#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R808：盈利能力分析（content_stats × metrics，按发布天数归一）。

为什么需要它：
- 浏览数是**累计值**，老帖天然更高。直接比中位数会得出「越老越好」的假结论
  （实测：无 lang 的旧帖 views/天 7.2，新帖 1.3~3.1——差别主要来自时代，不是内容）。
- 只有把 views 除以「发布天数」，才能横向比较语种/时段/长短文/内容特征。
- 数据源：content_stats.jsonl（创作者中心导出，content_id/views/likes/comments）
  + metrics.jsonl（发帖遥测，content_id/lang/article/tokens/final_text）。

用法：
    python scripts/profit_report.py
    python scripts/profit_report.py --days 8 --stats content_stats.jsonl
"""
import argparse
import collections
import datetime
import io
import json
import re
import statistics as st
import sys

MIN_AGE_DAYS = 0.25  # 刚发布的帖至少按 6 小时计，避免除零/爆表


def load_jsonl(path):
    rows = []
    with io.open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def index_published(metrics_rows):
    """content_id -> 已发布 summarize 行（同一 id 取最后一条）。"""
    idx = {}
    for r in metrics_rows:
        if not isinstance(r, dict):
            continue
        if r.get("stage") != "summarize":
            continue
        if not str(r.get("outcome", "")).startswith("binance_published"):
            continue
        cid = str(r.get("content_id") or "")
        if cid:
            idx[cid] = r
    return idx


def views_per_day(views, age_days):
    age = max(MIN_AGE_DAYS, float(age_days or 0))
    return float(views or 0) / age


def join_stats(stats_rows, published_idx, export_time):
    """按 content_id 关联，返回带 views_per_day 的记录。"""
    out = []
    for s in stats_rows:
        if not isinstance(s, dict):
            continue
        m = published_idx.get(str(s.get("content_id") or ""))
        if not m:
            continue
        try:
            ts = datetime.datetime.fromisoformat(str(s.get("ts", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        age = (export_time - ts).total_seconds() / 86400.0
        out.append({
            "views": int(s.get("views") or 0),
            "likes": int(s.get("likes") or 0),
            "comments": int(s.get("comments") or 0),
            "age_days": max(MIN_AGE_DAYS, age),
            "vpd": views_per_day(s.get("views"), age),
            "meta": m,
        })
    return out


def summarize(records, keyfn, min_n=3):
    """按 key 分组，返回 (key, n, median_vpd, mean_vpd)，按中位数降序。"""
    g = collections.defaultdict(list)
    for r in records:
        g[keyfn(r)].append(r["vpd"])
    rows = []
    for k, v in g.items():
        if len(v) < min_n:
            continue
        rows.append((k, len(v), st.median(v), sum(v) / len(v)))
    rows.sort(key=lambda x: -x[2])
    return rows


BTC_ETH_TOKENS = {"BTC", "ETH"}
LARGE_CAP_TOKENS = {"SOL", "XRP", "BNB", "DOGE", "ADA", "SHIB", "TRX", "LINK"}


def token_category(token):
    """R809：币种大类（BTC/ETH 浏览显著更高，见报告）。"""
    t = str(token or "").upper()
    if t in BTC_ETH_TOKENS:
        return "BTC/ETH"
    if t in LARGE_CAP_TOKENS:
        return "大市值"
    return "其他"


def engagement_quality(records):
    """R811：互动（赞+评）是否可判读。

    生产实证（10-18 导出，378 行）：374 行 likes=0、377 行 comments=0——
    但这极可能是创作者中心导出**没有点赞/评论列**，`import_content_stats.py`
    缺列补 0 ⇒ **不可测**。若直接把 0 当结论，会做出完全错误的互动决策。
    判据：非零互动占比 <5% 视为不可测（真实账号不可能 95% 的帖子零赞零评）。
    """
    n = len(records)
    if n == 0:
        return 0, 0, False
    likes = sum(r.get("likes") or 0 for r in records)
    comments = sum(r.get("comments") or 0 for r in records)
    nonzero = sum(1 for r in records if (r.get("likes") or 0) or (r.get("comments") or 0))
    return likes, comments, (nonzero / n) >= 0.05


def first_line(rec):
    t = rec["meta"].get("final_text") or rec["meta"].get("final_preview") or ""
    return str(t).split("\n")[0]


def _fmt(rows):
    for k, n, md, mean in rows:
        print("  %-20s n=%-3d views/天 中位=%-6.1f 均值=%.1f" % (str(k)[:20], n, md, mean))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", default="content_stats.jsonl")
    ap.add_argument("--metrics", default="metrics.jsonl")
    ap.add_argument("--days", type=float, default=0,
                    help="只看最近 N 天发布的帖（0=全部）")
    ap.add_argument("--export-time", default="",
                    help="数据导出时间（ISO），默认取 stats 最大 ts + 2 天")
    args = ap.parse_args(argv)

    stats = load_jsonl(args.stats)
    metrics = load_jsonl(args.metrics)
    if not stats or not metrics:
        print("数据不足：stats=%d metrics=%d" % (len(stats), len(metrics)))
        return 1

    if args.export_time:
        export = datetime.datetime.fromisoformat(args.export_time.replace("Z", "+00:00"))
    else:
        latest = max(str(s.get("ts", "")) for s in stats)
        export = datetime.datetime.fromisoformat(latest.replace("Z", "+00:00")) + datetime.timedelta(days=2)

    joined = join_stats(stats, index_published(metrics), export)
    if args.days > 0:
        joined = [r for r in joined if r["age_days"] <= args.days]
    _youngest = min((r["age_days"] for r in joined), default=None)
    print("stats=%d metrics=%d joined=%d 导出时间=%s%s"
          % (len(stats), len(metrics), len(joined), export.date(),
             (" ｜ 最年轻 %.1f 天（偏旧的浏览数据别过度解读）" % _youngest) if _youngest is not None else ""))
    if not joined:
        return 0

    print("\n== 语种 ==")
    _fmt(summarize(joined, lambda r: r["meta"].get("lang") or "(无lang)"))
    print("\n== 长短文 ==")
    _fmt(summarize(joined, lambda r: "长文" if r["meta"].get("article") else "短讯"))
    print("\n== 小时(UTC) × 语种（中位 views/天）==")
    for h in sorted(set(str(r["meta"].get("ts", ""))[11:13] for r in joined)):
        cells = []
        for lg in ("zh-CN", "zh-TW", "en"):
            v = [r["vpd"] for r in joined
                 if str(r["meta"].get("ts", ""))[11:13] == h and r["meta"].get("lang") == lg]
            cells.append("%5.1f(n=%d)" % (st.median(v), len(v)) if v else "    -    ")
        print("  %s  %s  %s  %s" % (h, cells[0], cells[1], cells[2]))
    print("\n== 币种 ==")
    _fmt(summarize(joined, lambda r: (r["meta"].get("tokens") or ["?"])[0]))
    print("\n== 币种大类 ==")
    _fmt(summarize(joined, lambda r: token_category((r["meta"].get("tokens") or ["?"])[0])))
    print("\n== 配图 ==")
    _fmt(summarize(joined, lambda r: str(r["meta"].get("image_tier") or "none")))
    # ★★ R811：互动必须**先判可测性**再看数。
    likes, comments, measurable = engagement_quality(joined)
    if not measurable:
        print("\n== 互动 ==⚠️ 不可测：%s 篇里 %s 篇赞/评均为 0，疑似导出缺点赞/评论列"
              "（import_content_stats.py 缺列补 0）⇒ 不要基于互动率做任何内容决策。"
              % (len(joined), sum(1 for r in joined if not (r.get('likes') or 0) and not (r.get('comments') or 0))))
    else:
        print("\n== 互动 ==\n  总赞 %d ｜ 总评 %d ｜ (赞+评)/浏览 合计 %.4f"
              % (likes, comments, (likes + comments) / max(1, sum(r["views"] for r in joined))))
    print("\n== 首行特征（有/无 中位差）==")
    def feat(name, fn):
        a = [r["vpd"] for r in joined if fn(r)]
        b = [r["vpd"] for r in joined if not fn(r)]
        if len(a) < 3 or len(b) < 3:
            return
        base = st.median(b)
        print("  %-16s 有 n=%-3d 中位=%-5.1f | 无 n=%-3d 中位=%-5.1f | 差 %+.0f%%" % (
            name, len(a), st.median(a), len(b), base,
            100 * (st.median(a) / base - 1) if base else 0))
    feat("首行含数字", lambda r: bool(re.search(r"\d", first_line(r))))
    feat("首行含 $币种", lambda r: "$" in first_line(r))
    feat("首行含问号", lambda r: "?" in first_line(r) or "？" in first_line(r))
    feat("正文≥2个币种", lambda r: len(set(re.findall(
        r"\$([A-Za-z]{2,10})", str(r["meta"].get("final_text") or r["meta"].get("final_preview") or "")))) >= 2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
