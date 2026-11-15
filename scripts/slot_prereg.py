#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R869：**发布时段 × 浏览量** 的预注册判据（只读，先观测、不干预）。

★★★ 为什么是"先观测不干预"
  · 2026-11-15 的平台快照里，UTC 00–02 的 6 帖中位 **44** 浏览，UTC 02–05 的 5 帖中位 **0**
    （已按同时龄 6–10h 分层）。差距大到不像噪声。
  · 但这是 **n=11、单日**，而且**时段是在看完这一天之后才挑出来的**（数据驱动的假设）
    ⇒ 只能在**新数据**上确认，绝不能拿同一天自证。
  · 观测不动排程 ⇒ 不改发布集合 ⇒ 现在就能开始，不必等 ⑩ 窗口。

★★ 口径（与 `profit_report` 同源，不另立第二套）
  · 曝光时长 = `content_stats.ts(观测时刻) − metrics.ts(发帖时刻)`；
    ⚠️ 反例：把 `content_stats.ts` 当发帖时刻用过一次 ⇒ 凭空造出 4.5~9 倍的假时段效应
      （实际比的是三次导入批次，原始中位数两边都是 5）。见 HANDOVER 纪律第 19 条。
  · 指标 = `vpd`（views / 天）。
  · 只收**曝光 ∈ [6h, 72h]** 的帖：太新没跑开、太旧会被后续导出覆盖。

★★★ 预注册条款（**写死在下面，测完不许改**）
  · 主比较：`00-02` vs `02-05`（UTC，按**发帖时刻**分桶）；
  · 有效日 = 两个桶各 ≥3 帖、**两桶 en 占比差 ≤20pp**（防语种混淆，本轮真踩过）；
  · 统计单位 = **日**：逐日比"两桶 vpd 中位"，用**符号检验**（避免同日多帖自相关）；
  · **通过（证实时段效应）** = 有效日 ≥8 **且** 符号检验单侧 p<0.05 **且** 合并中位比 ≥1.5×；
    　（n=8 时 k≥7 才 p<0.05 ⇒ **允许 1 天反向**；n=5~7 要全胜，基本不可达 ⇒ 故意不设低门槛）
  · **反转** = ≥4/5 天方向相反 ⇒ 记录，**不改排程**；
  · **未通过** = 其余 ⇒ **不改排程**，线索标为「未被证实」；
  · 兜底：有效日 <8 ⇒ **数据不足，不下任何结论**（不许读成"没效果"）。

★ 阈值为什么是 3 帖 / 8 天（**说清依据，便于复核**）
  · 按 ≤11-15 的**非确认样本**统计可得性：现存每天两桶各约 2~6 帖 ⇒ 若要求每桶 ≥5，
    几乎每天都不合格、永远攒不够日子；**只按"能不能攒够"定，不按方向定**（方向在
    确认样本里完全不参与，那几天被 `R869_FROM_DATE` 整段排除）。
  · 8 天是为了让符号检验允许 1 天反向；5 天要全胜 = 假阴性风险太高。

⚠️ 本文件**只读**：不参与发帖决策，不改任何生成/排程参数。
"""
import argparse
import datetime
import io
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "scripts"))

import profit_report as pr  # noqa: E402  ← 口径唯一实现

# ---- 预注册常量（先写死，再看数据）----
R869_FROM_DATE = "2026-11-16"          # 确认样本起点（11-15 是"发现样本"，不许混入）
R869_PRIMARY = ("00-02", "02-05")      # 主比较的两个桶
R869_MIN_POSTS_PER_BUCKET = 3          # 每个桶每天至少几帖（见 docstring 的可得性依据）
R869_MIN_TOTAL_PER_BUCKET = 20         # 合并后每桶总帖数下限（防中位数只由个别日决定）
R869_MIN_DAYS = 8                      # 至少几个有效日（n=8 才允许 1 天反向）
R869_MIN_RATIO = 1.5                   # 合并中位比至少几倍
R869_MAX_LANG_SHARE_DIFF = 0.20         # 两桶 en 占比差的上限（防语种混淆）
R869_EXPOSURE_HOURS = (6.0, 72.0)      # 只收这个曝光区间的帖
R869_ALPHA = 0.05

# 桶边界固定（不许事后改；改边界=改预注册=另开一轮）
BUCKETS = (("00-02", 0, 2), ("02-05", 2, 5), ("05-08", 5, 8), ("08-11", 8, 11),
           ("11-14", 11, 14), ("14-17", 14, 17), ("17-20", 17, 20), ("20-24", 20, 24))


def bucket_of(hour: int) -> str:
    """UTC 小时 → 桶名（边界固定，见 BUCKETS）。"""
    h = int(hour) % 24
    for name, lo, hi in BUCKETS:
        if lo <= h < hi:
            return name
    return "20-24"


def sign_test_p(k: int, n: int) -> float:
    """单侧符号检验：P(X ≥ k | n, p=0.5)。纯整数组合，无随机数 ⇒ 可复现。"""
    if n <= 0:
        return 1.0
    k = max(0, min(k, n))
    return min(1.0, sum(math.comb(n, i) for i in range(k, n + 1)) / float(2 ** n))


def _parse(s):
    return pr._parse_iso_utc(s)


def collect(joined, from_date: str = R869_FROM_DATE) -> list:
    """把 profit_report 的联表结果整理成时段样本（只读）。"""
    out = []
    lo, hi = R869_EXPOSURE_HOURS
    for r in joined or []:
        meta = r.get("meta") or {}
        post = _parse(meta.get("ts"))
        if post is None:
            continue
        exp_h = float(r.get("age_days") or 0) * 24.0
        if not (lo <= exp_h <= hi):
            continue
        day = post.strftime("%Y-%m-%d")
        if from_date and day < from_date:
            continue
        out.append({
            "day": day, "hour": post.hour, "bucket": bucket_of(post.hour),
            "lang": meta.get("lang") or "?", "vpd": float(r.get("vpd") or 0.0),
            "views": int(r.get("views") or 0), "exposure_h": exp_h,
        })
    return out


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return 0.0
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def day_pairs(rows, b_hi: str = R869_PRIMARY[0], b_lo: str = R869_PRIMARY[1]) -> list:
    """逐日取两桶的（样本数、中位 vpd、en 占比），并标记该日是否"有效"。"""
    days = sorted({r["day"] for r in rows})
    out = []
    for d in days:
        hi_rows = [r for r in rows if r["day"] == d and r["bucket"] == b_hi]
        lo_rows = [r for r in rows if r["day"] == d and r["bucket"] == b_lo]
        if not hi_rows or not lo_rows:
            continue
        n_hi, n_lo = len(hi_rows), len(lo_rows)
        share_hi = sum(1 for r in hi_rows if r["lang"] == "en") / float(n_hi)
        share_lo = sum(1 for r in lo_rows if r["lang"] == "en") / float(n_lo)
        med_hi = _median([r["vpd"] for r in hi_rows])
        med_lo = _median([r["vpd"] for r in lo_rows])
        ok = (n_hi >= R869_MIN_POSTS_PER_BUCKET and n_lo >= R869_MIN_POSTS_PER_BUCKET
              and abs(share_hi - share_lo) <= R869_MAX_LANG_SHARE_DIFF)
        out.append({"day": d, "n_hi": n_hi, "n_lo": n_lo,
                    "share_hi": share_hi, "share_lo": share_lo,
                    "med_hi": med_hi, "med_lo": med_lo, "valid": ok,
                    "hi_wins": med_hi > med_lo})
    return out


def slot_verdict(rows) -> tuple:
    """预注册判定（纯函数）：返回 (状态, 说明)。状态 ∈ 数据不足/通过/反转/未通过。"""
    pairs = [p for p in day_pairs(rows) if p["valid"]]
    n = len(pairs)
    tot_hi = sum(p["n_hi"] for p in pairs)
    tot_lo = sum(p["n_lo"] for p in pairs)
    if n < R869_MIN_DAYS or tot_hi < R869_MIN_TOTAL_PER_BUCKET or tot_lo < R869_MIN_TOTAL_PER_BUCKET:
        return ("数据不足", "有效日 %d < %d 或合并帖数 %d/%d < %d ⇒ 不下任何结论（不等于没效果）"
                % (n, R869_MIN_DAYS, tot_hi, tot_lo, R869_MIN_TOTAL_PER_BUCKET))
    k = sum(1 for p in pairs if p["hi_wins"])
    p_val = sign_test_p(k, n)
    pooled_ratio = 0.0
    med_hi = _median([p["med_hi"] for p in pairs])
    med_lo = _median([p["med_lo"] for p in pairs])
    if med_lo > 0:
        pooled_ratio = med_hi / med_lo
    detail = ("有效日 %d ｜ %s 更高的天 %d/%d ｜ 符号检验 p=%.4f ｜ 合并中位 %.2f vs %.2f（%.2f×）"
              % (n, R869_PRIMARY[0], k, n, p_val, med_hi, med_lo, pooled_ratio))
    if k < n - k and (n - k) >= 4:     # 多数日子方向相反 ⇒ 反转
        return ("反转", detail + " ⇒ 方向与假设相反，记录但不改排程")
    if p_val < R869_ALPHA and pooled_ratio >= R869_MIN_RATIO:
        return ("通过", detail + " ⇒ 时段效应成立（下一步才谈排程干预，且须重新预注册）")
    return ("未通过", detail + " ⇒ 时段差异**未被证实**，不改排程")


def build_rows(stats_path=None, metrics_path=None, from_date=R869_FROM_DATE):
    """读 content_stats + metrics（线上或本地）→ 时段样本。"""
    stats_path = stats_path or os.path.join(HERE, "content_stats.jsonl")
    metrics_path = metrics_path or os.path.join(HERE, "metrics.jsonl")
    stats = pr.load_jsonl(stats_path)
    idx = pr.index_published(pr.load_jsonl(metrics_path))
    joined = pr.join_stats(stats, idx)
    return collect(joined, from_date)


def render(rows) -> str:
    lines = []
    pairs = day_pairs(rows)
    lines.append("  日期        %s              %s              有效" % R869_PRIMARY)
    for p in pairs:
        lines.append("  %s  n=%2d 中位 %6.2f ｜ n=%2d 中位 %6.2f ｜ %s"
                     % (p["day"], p["n_hi"], p["med_hi"], p["n_lo"], p["med_lo"],
                        "✓" if p["valid"] else "—（样本/语种占比不合格）"))
    if not pairs:
        lines.append("  （没有同时含两个桶的日子）")
    else:
        lines.append("  合并：%s 共 %d 帖 ｜ %s 共 %d 帖（门槛：各 ≥%d 帖、有效日 ≥%d）"
                     % (R869_PRIMARY[0], sum(p["n_hi"] for p in pairs),
                        R869_PRIMARY[1], sum(p["n_lo"] for p in pairs),
                        R869_MIN_TOTAL_PER_BUCKET, R869_MIN_DAYS))
    st, why = slot_verdict(rows)
    lines.append("")
    lines.append("  ⇒ 判定：**%s** —— %s" % (st, why))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="R869 时段×浏览 预注册判据（只读）")
    ap.add_argument("--stats", default="")
    ap.add_argument("--metrics", default="")
    ap.add_argument("--from", dest="from_date", default=R869_FROM_DATE)
    ap.add_argument("--json", action="store_true", help="输出结构化结果")
    args = ap.parse_args(argv)

    rows = build_rows(args.stats or None, args.metrics or None, args.from_date)
    print("【R869 时段×浏览 · 预注册判据】只读；样本从 %s 起，曝光 ∈ [%.0f, %.0f]h"
          % (args.from_date, R869_EXPOSURE_HOURS[0], R869_EXPOSURE_HOURS[1]))
    print("  样本 %d 帖 ｜ 覆盖日 %d ｜ 语种 %s"
          % (len(rows), len({r["day"] for r in rows}),
             {l: sum(1 for r in rows if r["lang"] == l)
              for l in sorted({r["lang"] for r in rows})}))
    print(render(rows))
    if args.json:
        st, why = slot_verdict(rows)
        print(json.dumps({"status": st, "reason": why,
                          "days": day_pairs(rows)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
