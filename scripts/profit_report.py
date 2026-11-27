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


def _parse_iso_utc(s):
    """ISO 字符串 → aware datetime（UTC）；不可解析返回 None（未知不是 0，纪律 12）。"""
    try:
        d = datetime.datetime.fromisoformat(str(s or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def join_stats(stats_rows, published_idx, export_time=None):
    """按 content_id 关联，返回带 views_per_day 的记录。

    ★★★ R853：**曝光时长的口径修正**（旧版量错了东西）。
      · 旧版：`age = export_time − content_stats.ts`。而 `content_stats.ts` 是
        **该浏览量被观测/导入到的时刻**（快照时刻），**不是发帖时刻**
        ⇒ 旧口径量的是「这份数据有多旧」，与帖子曝光时长无关：
          同一篇帖，观测越晚（越接近导出）读数越高；而老批次（10-14~10-21 导入）
          会被系统性算低 **~20×** —— 实测那句「近 2 天 6.72 vs 更早 0.27」的假象
          就来自这里（详见 HANDOVER 的浏览量侧：`vpd_true2.py`）。
      · 新口径：`age = content_stats.ts(观测) − metrics.ts(发帖)` —— **两端都取自数据本身**，
        与 `--export-time` 无关（该参数降级为"仅表头展示/向后兼容"，age 不再使用它）。
      · 观测早于发帖（时钟错位/字段串行）⇒ `age <= 0` ⇒ **跳过**，不硬算（宁少算不伪造）。
    """
    out = []
    for s in stats_rows:
        if not isinstance(s, dict):
            continue
        m = published_idx.get(str(s.get("content_id") or ""))
        if not m:
            continue
        obs = _parse_iso_utc(s.get("ts"))
        post = _parse_iso_utc(m.get("ts"))
        if obs is None or post is None:
            continue
        age = (obs - post).total_seconds() / 86400.0
        if age <= 0:
            continue
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


def hour_bucket(ts):
    """metrics.ts(UTC) → 3 小时桶序号 0..7（只用于分布比较，不是判据阈值）。"""
    try:
        h = int(str(ts)[11:13])
    except (TypeError, ValueError):
        return None
    return h // 3


def hour_profile(rows):
    """类别内的小时分布（占比），None 桶丢弃。返回 {bucket: share}。"""
    c = collections.Counter()
    for r in rows:
        b = hour_bucket(r["meta"].get("ts"))
        if b is not None:
            c[b] += 1
    tot = sum(c.values())
    return {b: v / float(tot) for b, v in c.items()} if tot else {}


def profile_tvd(p, q):
    """两个分布在 8 个桶上的总变差距离（0=同分布，1=完全错开）。"""
    keys = set(p) | set(q)
    return 0.5 * sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in keys)


COLLINEAR_TVD = 0.35   # 启发式：仅用于"提示读者别直接比"，不参与任何取舍


def collinearity(joined, keyfn):
    """每个类别相对**全样本**的小时分布偏斜。返回 [(类别, n, tvd, 主桶, 主桶占比)]。"""
    overall = hour_profile(joined)
    out = []
    for cat in sorted({keyfn(r) for r in joined}, key=str):
        rs = [r for r in joined if keyfn(r) == cat]
        if len(rs) < 8:          # 小样本的分布没有解释力，别拿它发警报
            continue
        prof = hour_profile(rs)
        top = max(prof.items(), key=lambda kv: kv[1]) if prof else (None, 0.0)
        out.append((cat, len(rs), profile_tvd(prof, overall), top[0], top[1]))
    return out


def within_bucket(joined, keyfn, cat_a, cat_b, min_n=3):
    """两个类别的**桶内**对照：只返回两边都 ≥min_n 的桶。
    返回 [(桶, a 的 n, a 中位, b 的 n, b 中位)]。
    """
    rows = []
    for b in range(8):
        a = [r["vpd"] for r in joined
             if hour_bucket(r["meta"].get("ts")) == b and keyfn(r) == cat_a]
        c = [r["vpd"] for r in joined
             if hour_bucket(r["meta"].get("ts")) == b and keyfn(r) == cat_b]
        if len(a) >= min_n and len(c) >= min_n:
            rows.append((b, len(a), st.median(a), len(c), st.median(c)))
    return rows


def collinearity_lines(joined, keyfn, title):
    """渲染共线告警（只提示，不改任何结论）。"""
    lines = []
    for cat, n, tvd, top, share in collinearity(joined, keyfn):
        if tvd < COLLINEAR_TVD:
            continue
        lines.append(
            "  ⚠️ **%s 「%s」与时段共线**：n=%d，%d–%d 点 UTC 占 %.0f%%（TVD=%.2f）"
            "⇒ 别拿它跟别的%s直接比中位，先看桶内对照。"
            % (title, cat, n, (top or 0) * 3, (top or 0) * 3 + 3, share * 100, tvd, title))
    return lines


def _fmt(rows):
    for k, n, md, mean in rows:
        print("  %-20s n=%-3d views/天 中位=%-6.1f 均值=%.1f" % (str(k)[:20], n, md, mean))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", default="content_stats.jsonl")
    ap.add_argument("--metrics", default="metrics.jsonl")
    ap.add_argument("--days", type=float, default=0,
                    help="只看曝光 ≤N 天的帖（0=全部）")
    ap.add_argument("--export-time", default="",
                    help="⚠️ R853 起**不再参与 age 计算**（曝光改为 观测−发帖）；仅向后兼容")
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
    print("stats=%d metrics=%d joined=%d 曝光口径=R853（观测−发帖）%s"
          % (len(stats), len(metrics), len(joined),
             (" ｜ 最短曝光 %.2f 天" % _youngest) if _youngest is not None else ""))
    if not joined:
        return 0

    print("\n== 语种 ==")
    _fmt(summarize(joined, lambda r: r["meta"].get("lang") or "(无lang)"))
    # ★ R881：语种 × 时段共线护栏（en 只在夜窗发时，跨语种中位差会伪造出"英文差"）
    for _ln in collinearity_lines(joined, lambda r: r["meta"].get("lang") or "(无lang)", "语种"):
        print(_ln)
    print("\n== 长短文 ==")
    _fmt(summarize(joined, lambda r: "长文" if r["meta"].get("article") else "短讯"))
    # ★ R881：体裁 × 时段共线护栏（长文常集中在少数时段，直接比会把它读成"长文更好"）
    for _ln in collinearity_lines(joined, lambda r: "长文" if r["meta"].get("article") else "短讯",
                                  "体裁"):
        print(_ln)
    _wb = within_bucket(joined, lambda r: "长文" if r["meta"].get("article") else "短讯",
                        "长文", "短讯")
    if _wb:
        print("  ↳ 桶内对照（两边各 ≥3 帖才列；用于剥离时段）：")
        for _b, _na, _ma, _nc, _mc in _wb:
            print("     %02d–%02d UTC  长文 n=%-2d 中位 %-6.1f ｜ 短讯 n=%-3d 中位 %-6.1f ｜ 比 %.2f×"
                  % (_b * 3, _b * 3 + 3, _na, _ma, _nc, _mc, (_ma / _mc) if _mc else 0))
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
    # ★ R893：热度分档 × 浏览（2026-11-27 深挖后固化的观测面板）
    #   实测（曝光 24~72h、zh 短讯）：分 ≥60 的帖 vpd 中位只有 1.0~1.3，
    #   而 0–35 档在 1.6~3.1 ⇒ 超大事件帖反而低。
    #   ⚠️ **不能据此改排序**：低分档是"被限流/配额筛过后的残余集"（选择偏差），
    #   真要验证得做受控实验。这里只负责让它**持续可见**，别再靠一次性脚本重挖。
    def _score_band(r):
        v = r["meta"].get("impact_score")
        if not isinstance(v, (int, float)):
            return "无分"
        v = int(v)
        for lo, hi in ((0, 15), (15, 25), (25, 35), (35, 60), (60, 10 ** 9)):
            if lo <= v < hi:
                return "%d-%d" % (lo, min(hi, 999))
        return "无分"

    print("\n== 热度分档 × 浏览（低分档=被筛后的残余集，别当因果）==")
    _fmt(summarize(joined, _score_band))
    # ★ 2026-11-27 已验伪：原始读数看着"分越高浏览越低"（2.2→0.9 单调），
    #   但**按小时居中后残差只有 ±0.6** ⇒ 那条单调主要是**时段构成**造成的。
    #   ⇒ 别据此改排序（真要验得做受控实验）。这行旁注就是防止下一个人重犯。
    print("  ↳ 已验伪：按小时居中后各档残差仅 ±0.6（原始单调来自时段构成）；"
          "低档还是被筛残余集 ⇒ **本面板只做监控，不得用于改排序**")
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
