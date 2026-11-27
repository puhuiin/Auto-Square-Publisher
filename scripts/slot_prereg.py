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
import collections
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
    """把 profit_report 的联表结果整理成时段样本（只读）。

    ★ R871：额外记录 `fresh`（该帖是不是**当天该题材的第一条**）。
      为什么必须记：实测（11-15/11-16）00-02 桶的帖**几乎全是"首发"**（题材当天第 1 条），
      而 02-05 桶中位是第 2 条、05-08 是第 3~4 条、08-11 更高 ⇒
      **"时段"与"题材新鲜度"天然缠在一起**（越早的时段拿到越新的故事）。
      若不记录这个变量，就可能把"首发优势"读成"时段优势"，然后去改排程 —— 什么也拿不到 ✗。
    """
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
        toks = meta.get("tokens") or []
        out.append({
            "day": day, "hour": post.hour, "bucket": bucket_of(post.hour),
            "lang": meta.get("lang") or "?", "vpd": float(r.get("vpd") or 0.0),
            "views": int(r.get("views") or 0), "exposure_h": exp_h,
            "token": str(toks[0]) if toks else "",
            "impact": meta.get("impact_score"),
            "_t": post,
        })
    # 按时间顺序标「是否当天该题材的首发」
    seen = {}
    for r in sorted(out, key=lambda x: x["_t"]):
        key = (r["day"], r["token"])
        seen[key] = seen.get(key, 0) + 1
        r["ordinal"] = seen[key]
        r["fresh"] = seen[key] == 1
    for r in out:
        r.pop("_t", None)
    return out


def confound_text(rows) -> str:
    """★ R871：**混淆报告**（只读，不参与判定）。

    回答："00-02 的高浏览，是时段给的，还是因为它恰好拿到当天最新鲜的故事？"
      · 逐桶给「首发占比」与「首发帖的 vpd 中位 vs 非首发帖」；
      · 若**首发帖内部** 00-02 仍显著高于 02-05 ⇒ 时段效应站得住；
      · 若差距只在"首发 vs 非首发"上 ⇒ 时段只是代理，**改排程无用**，该去治"重复覆盖"。
    ⚠️ 本报告**不改变** R869 的通过/回滚条款（改条款 = 另开一轮预注册）。
    """
    if not rows:
        return "  （无样本）"
    lines = []
    lines.append("  桶      n  首发占比   首发 n/中位   非首发 n/中位   素材分中位")
    for b in [x[0] for x in BUCKETS]:
        g = [r for r in rows if r["bucket"] == b]
        if not g:
            continue
        fresh = [r["vpd"] for r in g if r.get("fresh")]
        old = [r["vpd"] for r in g if not r.get("fresh")]
        imps = [r["impact"] for r in g if isinstance(r["impact"], (int, float))]
        lines.append("  %-6s %3d  %4.0f%%      %2d /%7.1f    %2d /%7.1f      %s"
                     % (b, len(g), 100.0 * len(fresh) / len(g),
                        len(fresh), _median(fresh), len(old), _median(old),
                        ("%5.1f" % _median(imps)) if imps else "  —"))
    hi = [r for r in rows if r["bucket"] == R869_PRIMARY[0]]
    lo = [r for r in rows if r["bucket"] == R869_PRIMARY[1]]
    f_hi = [r["vpd"] for r in hi if r.get("fresh")]
    f_lo = [r["vpd"] for r in lo if r.get("fresh")]
    if f_hi and f_lo:
        lines.append("  ⇒ **只看首发帖**：%s n=%d 中位 %.1f ｜ %s n=%d 中位 %.1f（%.1f×）"
                     % (R869_PRIMARY[0], len(f_hi), _median(f_hi),
                        R869_PRIMARY[1], len(f_lo), _median(f_lo),
                        (_median(f_hi) / _median(f_lo)) if _median(f_lo) else float("inf")))
    else:
        lines.append("  ⇒ 只看首发帖：样本不足（%d / %d）⇒ 无法排除题材新鲜度混淆"
                     % (len(f_hi), len(f_lo)))
    return "\n".join(lines)


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
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


def robustness_reference(pre_rows, cut=R869_FROM_DATE):
    """★ 稳健性参考（**不参与判定**）：用**发现样本之前**的数据复算窗内/窗外。

    入参是 `collect()` 产出的**扁平行**（day/hour/bucket/vpd/lang），且已按
    `cut` 之前过滤好——`build_rows()` 自带 from_date 过滤，所以前置样本必须由
    调用方用更早的 from_date 单独构建（否则这里永远是空集）。
    绝不并入 `slot_verdict`：预注册口径只认确认样本。
    """
    import statistics as _st   # 本文件此前没导 statistics（st 在 render 里是局部变量）

    def _med(xs):
        return _st.median(xs) if xs else None

    def _split(rs):
        hi = [r["vpd"] for r in rs if r["bucket"] == R869_PRIMARY[0]]
        lo = [r["vpd"] for r in rs if r["bucket"] == R869_PRIMARY[1]]
        return hi, lo

    pre = [r for r in (pre_rows or []) if str(r.get("day", "")) < cut]
    if not pre:
        return None
    hi, lo = _split(pre)
    if not hi or not lo:
        return None
    zh = [r for r in pre if r.get("lang") in ("zh-CN", "zh-TW")]
    zhi, zlo = _split(zh)
    return {"n_hi": len(hi), "med_hi": _med(hi), "n_lo": len(lo), "med_lo": _med(lo),
            "n_hi_zh": len(zhi), "med_hi_zh": _med(zhi),
            "n_lo_zh": len(zlo), "med_lo_zh": _med(zlo)}


def weekly_ratio_detail(rows):
    """★ R897：按周算比值，并**同时给两个曝光口径**（[6,72] 与 [24,72]）。

    为什么必须给两个：`vpd = views/age`，6–24h 的年轻帖系统性偏高，
    各周年轻帖占比不同就会伪造出"比值在收窄"。实测：
        口径 [6,72]h  ：10.8× → 6.4×（看着收窄 41%）
        口径 [24,72]h ：9.2×  → 9.4×（稳定）
    ⇒ 单看前者会误判"窗口效应变弱"。预注册口径不改，这里只做敏感性展示。
    """
    import datetime as _dt
    import statistics as _st

    def _week(day):
        d = _dt.date.fromisoformat(day)
        return str(d - _dt.timedelta(days=d.weekday()))

    def _calc(rs):
        g = collections.defaultdict(lambda: {"hi": [], "lo": []})
        for r in rs:
            key = "hi" if r.get("bucket") == R869_PRIMARY[0] else (
                "lo" if r.get("bucket") == R869_PRIMARY[1] else None)
            if not key:
                continue
            try:
                w = _week(str(r.get("day", "")))
            except ValueError:
                continue
            g[w][key].append(r["vpd"])
        out = []
        for w in sorted(g):
            hi, lo = g[w]["hi"], g[w]["lo"]
            if len(hi) < 3 or len(lo) < 3:
                continue
            mh, ml = _st.median(hi), _st.median(lo)
            out.append((w, len(hi), mh, len(lo), ml, (mh / ml) if ml else None))
        return out

    loose = _calc(rows or [])
    tight = _calc([r for r in (rows or []) if float(r.get("exposure_h") or 0) >= 24.0])
    return loose, tight


def weekly_ratio(rows):
    """★ 分期比值：按自然周（周一起）看「窗内中位 / 窗外中位」。

    用途：区分两种"整体下滑"——
      · **比值稳定、绝对水平降** ⇒ 市场层面热度衰减，判据（窗内外对比）不受影响；
      · **比值也在缩** ⇒ 衰减对窗口更不利，得重新掂量结论。
    两侧各 <3 帖的周跳过。
    """
    import datetime as _dt

    def _week(day):
        d = _dt.date.fromisoformat(day)
        return str(d - _dt.timedelta(days=d.weekday()))

    g = collections.defaultdict(lambda: {"hi": [], "lo": []})
    for r in rows or []:
        key = "hi" if r.get("bucket") == R869_PRIMARY[0] else (
            "lo" if r.get("bucket") == R869_PRIMARY[1] else None)
        if not key:
            continue
        try:
            w = _week(str(r.get("day", "")))
        except ValueError:
            continue
        g[w][key].append(r["vpd"])
    out = []
    for w in sorted(g):
        hi, lo = g[w]["hi"], g[w]["lo"]
        if len(hi) < 3 or len(lo) < 3:
            continue
        import statistics as _st   # 本文件模块级没有 statistics（st 在 render 里是局部变量）
        mh, ml = _st.median(hi), _st.median(lo)
        out.append((w, len(hi), mh, len(lo), ml, (mh / ml) if ml else None))
    return out


def render(rows, pre_rows=None) -> str:
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

    # ★ R896：分期比值——区分"市场热度衰减"与"窗口效应变弱"
    _wr = weekly_ratio(rows)
    if len(_wr) >= 2:
        lines.append("")
        lines.append("  【分期比值 · 只用于排除「整体触达衰减」这一解释】")
        lines.append("     周(周一)      窗内 n/中位    窗外 n/中位    比值")
        for _w, _nh, _mh, _nl2, _ml, _r in _wr:
            lines.append("     %s    %3d / %5.1f    %3d / %5.1f    %s"
                         % (_w, _nh, _mh, _nl2, _ml, ("%.1f×" % _r) if _r else "-"))
        # ★ R897：口径敏感性——[6,72]h 与 [24,72]h 是否给同一结论
        _artefact = False
        _loose, _tight = weekly_ratio_detail(rows)
        if len(_loose) >= 2 and len(_tight) >= 2:
            def _trend(seq):
                xs = [x[5] for x in seq if x[5]]
                return (xs[-1] / xs[0]) if len(xs) >= 2 and xs[0] else None
            _tl, _tt = _trend(_loose), _trend(_tight)
            def _fmt(seq, t):
                if len(seq) < 2:
                    return "-"
                return "%.2f×→%.2f×（%+.0f%%）" % (seq[0][5], seq[-1][5], 100 * ((t or 1) - 1))
            lines.append("     ↳ 口径敏感性：[6,72]h %s ｜ [24,72]h %s"
                         % (_fmt(_loose, _tl), _fmt(_tight, _tt)))
            if _tl is not None and _tt is not None and _tl < 0.8 and _tt >= 0.9:
                _artefact = True
                lines.append("        ⚠️ 两个口径结论相反 ⇒ 收窄是**年轻帖占比**造成的假象，"
                             "别据此说「窗口效应在变弱」（本轮实测即此）")

        _rs = [x[5] for x in _wr if x[5]]
        if _artefact:
            pass          # 已由上面的口径敏感性解释：收窄是年轻帖占比造成的，不再重复告警
        elif len(_rs) >= 2:
            _chg = _rs[-1] / _rs[0]
            if _chg >= 0.8:
                lines.append("     ↳ 比值基本稳定（%.2f× → %.2f×）⇒ 绝对水平下落属**市场热度**，"
                             "不影响窗内外对比" % (_rs[0], _rs[-1]))
            else:
                lines.append("     ↳ ⚠️ 比值收窄了 %.0f%%（%.2f× → %.2f×）：仍高于门槛 %.1f×，"
                             "但需单独排查「衰减是否对窗口更不利」，别只读合并比值"
                             % (100 * (1 - _chg), _rs[0], _rs[-1], R869_MIN_RATIO))

    # ★ R892：稳健性参考（**不参与判定**）——用发现样本之前的数据复算一次
    rb = robustness_reference(pre_rows)
    if rb:
        def _r(a, b):
            return ("%.1f×" % (a / b)) if (a is not None and b) else "-"
        lines.append("")
        lines.append("  【稳健性参考 · **不参与判定**】发现样本（%s）之前的数据："
                     % R869_FROM_DATE)
        lines.append("     全部：00–02 桶 n=%d 中位 %s ｜ 02–05 桶 n=%d 中位 %s ⇒ %s"
                     % (rb["n_hi"], ("%.1f" % rb["med_hi"]) if rb["med_hi"] is not None else "-",
                        rb["n_lo"], ("%.1f" % rb["med_lo"]) if rb["med_lo"] is not None else "-",
                        _r(rb["med_hi"], rb["med_lo"])))
        lines.append("     仅 zh：00–02 桶 n=%d 中位 %s ｜ 02–05 桶 n=%d 中位 %s ⇒ %s"
                     % (rb["n_hi_zh"],
                        ("%.1f" % rb["med_hi_zh"]) if rb["med_hi_zh"] is not None else "-",
                        rb["n_lo_zh"],
                        ("%.1f" % rb["med_lo_zh"]) if rb["med_lo_zh"] is not None else "-",
                        _r(rb["med_hi_zh"], rb["med_lo_zh"])))
        _small = min(rb["n_hi"], rb["n_lo"]) < 8
        lines.append("     用途：回答「这是不是偶然」；**不得**并入上面的判定，"
                     "也不得据此提前改排程。")
        if _small:
            lines.append("     ⚠️ 前置样本很小（n=%d/%d）⇒ 只看方向与量级，别当独立证据。"
                         % (rb["n_hi"], rb["n_lo"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="R869 时段×浏览 预注册判据（只读）")
    ap.add_argument("--stats", default="")
    ap.add_argument("--metrics", default="")
    ap.add_argument("--from", dest="from_date", default=R869_FROM_DATE)
    ap.add_argument("--json", action="store_true", help="输出结构化结果")
    args = ap.parse_args(argv)

    rows = build_rows(args.stats or None, args.metrics or None, args.from_date)
    # ★ R892：前置样本要单独构建（build_rows 会按 from_date 过滤，同一个 rows 里没有前置数据）
    pre_rows = build_rows(args.stats or None, args.metrics or None, "2000-01-01")
    print("【R869 时段×浏览 · 预注册判据】只读；样本从 %s 起，曝光 ∈ [%.0f, %.0f]h"
          % (args.from_date, R869_EXPOSURE_HOURS[0], R869_EXPOSURE_HOURS[1]))
    print("  样本 %d 帖 ｜ 覆盖日 %d ｜ 语种 %s"
          % (len(rows), len({r["day"] for r in rows}),
             {l: sum(1 for r in rows if r["lang"] == l)
              for l in sorted({r["lang"] for r in rows})}))
    print(render(rows, pre_rows))
    print("\n【混淆报告（R871，只读、不参与判定）】")
    print(confound_text(rows))
    if args.json:
        st, why = slot_verdict(rows)
        print(json.dumps({"status": st, "reason": why,
                          "days": day_pairs(rows)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
