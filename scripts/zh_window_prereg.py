#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R883：**中文帖窗口倾斜**（UTC 00–02）的预注册判据（只读；先定判据，后开闸）。

★★★ 为什么是「中文帖」而不是「所有帖」
  · 语种由时段决定（R710）：北京 06–22 中文、22–06 英文。
  · 本次实测（2026-11-22 导出，423 篇联表、**曝光 24~72h 匹配**）：
        zh 帖 vpd 中位：UTC 00–02 = **12.1**（n=19） vs 窗外 = **2.2**（n=121）⇒ 5.4×
        en 帖：vpd 中位 1.2（全在 14–22 UTC，与中文低时段相当）
    ⇒ 英文不背锅；要动的是**中文帖的时刻**，英文夜窗（R710 的受众设定）一个字不碰。

★★★ 为什么窗口是 UTC 00–02（北京 08–10），不是 R706 的「北京 09–12」
  逐小时（年龄匹配）看：00 点 28.3 / 01 点 7.4 / 02 点 **1.7** / 03 点 0.8 ⇒ 02 点就塌。
  「北京 09–12」= UTC 01–04，会把一半的量投进 UTC 02–04 的**次优段**。

★★★ 判据（写死在下面，**测完不许改**；改 = 另开一轮预注册）
  · 机制：处置期「窗内 zh 帖/天」中位 **≥10**（基线 5~6）——先证明闸门真把量吃进窗口。
  · 效应：**逐日**比「窗内 zh vpd 中位」vs「同日窗外 zh vpd 中位」，符号检验（日为单位）；
    有效日 ≥6 且单侧 p<0.05（n=6 全胜 p=0.0156）。
  · 量级：合并「窗内中位」≥「窗外中位」×**2.0**（实测 5.4×，打 3.7 折留样本外衰减）。
  · 护栏：处置期**总发布/天** 中位 ≥**50**（基线 59~60）；en 帖小时分布与基线 TVD ≤**0.25**。
  · **通过** = 四条同时成立；**回滚** = 总量 <50 或 窗内 zh <8/天 或 量级 <1.3×；
    **未通过** = 其余 ⇒ 关闸（设回 0），不改其他时段。

★ 与 R869 的关系（必须写清）
  R869 的确认样本**在 2026-11-22 定格为 3 个有效日、未裁**。本实验改变发布集合
  ⇒ R869 **不再累计**（否则两边互相污染）。R869 的读数只作为本次窗口选择的依据。

★ 口径（两类数字必须分开算，否则会假触发回滚）
  · **vpd**（浏览）用 `profit_report.join_stats`，且只收曝光 ∈ **[24,72]h**：
    比 R869 的 [6,72] 更严——本轮实测过"6–24h 的年轻帖 vpd 系统性偏高"
    （00–03 桶原始读数 8.7 → 年龄匹配后 4.8），不收紧会把窗口优势读虚。
  · **计数**（总量 / 窗内篇数 / en 分布）直接取 metrics 发布行，**不做曝光过滤**：
    用曝光过滤后的子集数"一天发了多少篇"，在单次导出下只会数到一两天 ⇒ 假回滚。
  · 两者不要混用（本轮初版就混了：总量中位从 59 被算成 33）。
"""
import argparse
import collections
import json
import math
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "scripts"))

import profit_report as pr  # noqa: E402  ← 口径唯一实现

# ---- 预注册常量（先写死，再看数据）----
ZW_FROM_DATE = "2026-11-23"          # 处置期起点（开闸日；改它 = 另开预注册）
ZW_WINDOW_UTC = (0, 2)               # 目标窗（UTC，左闭右开）= 北京 08–10
ZW_LANGS = ("zh-CN", "zh-TW")
ZW_BASELINE_FROM = "2026-11-17"      # 基线臂（处置前最后一批导出覆盖）
ZW_BASELINE_TO = "2026-11-22"
ZW_MIN_VALID_DAYS = 6
ZW_MIN_RATIO = 2.0
ZW_MIN_WINDOW_POSTS = 10.0           # 窗内 zh 帖/天 中位
ZW_MAX_EN_TVD = 0.25
ZW_ROLLBACK_TOTAL_BELOW = 50
ZW_ROLLBACK_WINDOW_BELOW = 8
ZW_ROLLBACK_RATIO_BELOW = 1.3
ZW_ALPHA = 0.05
ZW_EXPOSURE_HOURS = (24.0, 72.0)


def sign_test_p(k, n):
    """单侧符号检验 P(X ≥ k | n, p=0.5)。纯整数组合，可复现。"""
    if n <= 0:
        return 1.0
    k = max(0, min(int(k), int(n)))
    return min(1.0, sum(math.comb(n, i) for i in range(k, n + 1)) / float(2 ** n))


def _hour(ts):
    return int(str(ts)[11:13])


def _in_window(ts):
    lo, hi = ZW_WINDOW_UTC
    return lo <= _hour(ts) < hi


def _is_zh(lang):
    return (lang or "zh-CN") in ZW_LANGS


def hour_profile_tvd(rows_a, rows_b):
    """两组发布行的 3 小时桶分布 TVD（确认 en 夜窗没被带跑）。"""
    def prof(rows):
        c = collections.Counter(_hour(r.get("ts")) // 3 for r in rows)
        tot = sum(c.values())
        return {k: v / float(tot) for k, v in c.items()} if tot else {}
    p, q = prof(rows_a), prof(rows_b)
    keys = set(p) | set(q)
    return 0.5 * sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in keys)


def _med(vals):
    return st.median(vals) if vals else None


def collect(joined, pub_rows):
    """按日聚：vpd 侧（曝光过滤）+ 计数侧（全量发布行）。"""
    days = collections.defaultdict(lambda: {
        "win": [], "out": [], "en": [], "pub": [], "win_pub": []})
    for r in joined:
        d = str(r["meta"].get("ts", ""))[:10]
        if not d:
            continue
        lg = r["meta"].get("lang")
        if _is_zh(lg):
            days[d]["win" if _in_window(r["meta"]["ts"]) else "out"].append(r)
        elif lg == "en":
            days[d]["en"].append(r)
    for m in pub_rows:
        # ⚠️ 只认**发布行**：metrics 里还有 run_summary/provider_probe（无 lang），
        #    不过滤会把它们按"未知语种→zh-CN"数进窗内（初版实测：总量被算成 251/天）
        if not str(m.get("outcome", "")).startswith("binance_published"):
            continue
        d = str(m.get("ts", ""))[:10]
        if not d:
            continue
        days[d]["pub"].append(m)
        if _is_zh(m.get("lang")) and _in_window(m.get("ts")):
            days[d]["win_pub"].append(m)
    return days


def evaluate(joined, pub_rows):
    days = collect(joined, pub_rows)
    all_days = sorted(days)
    base_days = [d for d in all_days if ZW_BASELINE_FROM <= d <= ZW_BASELINE_TO]
    treat_days = [d for d in all_days if d >= ZW_FROM_DATE]

    base_win_posts = _med([len(days[d]["win_pub"]) for d in base_days]) if base_days else None
    base_total = _med([len(days[d]["pub"]) for d in base_days]) if base_days else None

    rows, wins_better, valid = [], 0, 0
    win_vpds, out_vpds = [], []
    for d in treat_days:
        w = [r["vpd"] for r in days[d]["win"]]
        o = [r["vpd"] for r in days[d]["out"]]
        ok = len(w) >= 3 and len(o) >= 3
        better = ok and _med(w) > _med(o)
        if ok:
            valid += 1
            wins_better += 1 if better else 0
            win_vpds.extend(w)
            out_vpds.extend(o)
        rows.append({"day": d, "win_n": len(w), "out_n": len(o),
                     "win_pub": len(days[d]["win_pub"]),
                     "win_med": _med(w), "out_med": _med(o),
                     "valid": ok, "better": bool(better)})

    win_posts_med = _med([len(days[d]["win_pub"]) for d in treat_days]) if treat_days else None
    total_med = _med([len(days[d]["pub"]) for d in treat_days]) if treat_days else None
    win_med_pool, out_med_pool = _med(win_vpds), _med(out_vpds)
    ratio = (win_med_pool / out_med_pool) if (win_med_pool is not None
                                             and out_med_pool) else None
    p = sign_test_p(wins_better, valid)

    base_en = [m for d in base_days for m in days[d]["pub"] if m.get("lang") == "en"]
    treat_en = [m for d in treat_days for m in days[d]["pub"] if m.get("lang") == "en"]
    en_tvd = hour_profile_tvd(base_en, treat_en) if (base_en and treat_en) else None

    checks = {
        "机制·窗内zh帖/天≥%.0f" % ZW_MIN_WINDOW_POSTS:
            (win_posts_med is not None and win_posts_med >= ZW_MIN_WINDOW_POSTS),
        "效应·符号检验": (valid >= ZW_MIN_VALID_DAYS and p < ZW_ALPHA),
        "量级·≥%.1f×" % ZW_MIN_RATIO: (ratio is not None and ratio >= ZW_MIN_RATIO),
        "护栏·总量≥%d" % ZW_ROLLBACK_TOTAL_BELOW:
            (total_med is not None and total_med >= ZW_ROLLBACK_TOTAL_BELOW),
        "护栏·en分布≤%.2f" % ZW_MAX_EN_TVD:
            (en_tvd is not None and en_tvd <= ZW_MAX_EN_TVD),
    }
    rollback = ((total_med is not None and total_med < ZW_ROLLBACK_TOTAL_BELOW)
                or (win_posts_med is not None and win_posts_med < ZW_ROLLBACK_WINDOW_BELOW)
                or (ratio is not None and ratio < ZW_ROLLBACK_RATIO_BELOW))
    if not treat_days:
        verdict = "未开闸 / 无处置样本"
    elif rollback:
        verdict = "回滚（关闸，设回 0）"
    elif all(checks.values()):
        verdict = "通过"
    elif valid < ZW_MIN_VALID_DAYS:
        verdict = "数据不足（有效日 %d < %d，不下任何结论）" % (valid, ZW_MIN_VALID_DAYS)
    else:
        verdict = "未通过（关闸，不改其他时段）"

    return {
        "baseline_days": base_days, "treatment_days": treat_days,
        "base_win_posts_med": base_win_posts, "base_total_med": base_total,
        "rows": rows, "valid": valid, "wins_better": wins_better, "p": p,
        "win_posts_med": win_posts_med, "total_med": total_med,
        "win_med_pool": win_med_pool, "out_med_pool": out_med_pool,
        "ratio": ratio, "en_tvd": en_tvd, "checks": checks,
        "rollback": rollback, "verdict": verdict,
    }


def render(res):
    def f(v, fmt="%.2f"):
        return (fmt % v) if v is not None else "-"

    L = ["【R883 中文帖窗口倾斜 · 预注册判据】只读；窗口 UTC %02d–%02d（北京 %02d–%02d），"
         "vpd 曝光 ∈ [%g,%g]h"
         % (ZW_WINDOW_UTC[0], ZW_WINDOW_UTC[1], (ZW_WINDOW_UTC[0] + 8) % 24,
            (ZW_WINDOW_UTC[1] + 8) % 24, ZW_EXPOSURE_HOURS[0], ZW_EXPOSURE_HOURS[1])]
    L.append("  基线臂 %s~%s（%d 天）：窗内 zh 帖/天 中位 %s（全量口径）｜ 总发布/天 中位 %s"
             % (ZW_BASELINE_FROM, ZW_BASELINE_TO, len(res["baseline_days"]),
                res["base_win_posts_med"], res["base_total_med"]))
    if not res["rows"]:
        L.append("  处置期（≥%s）**还没有样本** ⇒ 闸门打开后（`ZH_OFFWIN_CAP=32`）"
                 "每 2 天导出一次再来跑。" % ZW_FROM_DATE)
        return "\n".join(L)
    L.append("  日期         窗内(vpd)           窗外(vpd)        窗内篇数  有效  窗好于外")
    for r in res["rows"]:
        L.append("  %s  n=%-2d 中位 %-6s  n=%-2d 中位 %-6s  %-6d    %s     %s"
                 % (r["day"], r["win_n"], f(r["win_med"], "%.1f"),
                    r["out_n"], f(r["out_med"], "%.1f"), r["win_pub"],
                    "✓" if r["valid"] else "✗", "是" if r["better"] else "否"))
    L.append("")
    L.append("  有效日 %d/%d ｜ 窗好于外 %d/%d ｜ 符号检验 p=%.4f（<%.2f 才算过）"
             % (res["valid"], ZW_MIN_VALID_DAYS, res["wins_better"], res["valid"],
                res["p"], ZW_ALPHA))
    L.append("  合并：窗内 %s ｜ 窗外 %s ｜ 比值 %s（门槛 ≥%.1f×）"
             % (f(res["win_med_pool"]), f(res["out_med_pool"]),
                (f(res["ratio"]) + "×") if res["ratio"] else "-", ZW_MIN_RATIO))
    L.append("  机制：窗内 zh 帖/天 中位 %s（门槛 ≥%.0f）｜ 总量/天 中位 %s（护栏 ≥%d）"
             % (res["win_posts_med"], ZW_MIN_WINDOW_POSTS, res["total_med"],
                ZW_ROLLBACK_TOTAL_BELOW))
    L.append("  en 小时分布漂移 TVD %s（上限 %.2f）" % (f(res["en_tvd"]), ZW_MAX_EN_TVD))
    L.append("  判据逐条：" + " ｜ ".join("%s %s" % (k, "✓" if v else "✗")
                                    for k, v in res["checks"].items()))
    L.append("")
    L.append("  ⇒ 判定：**%s**" % res["verdict"])
    if res["verdict"].startswith("数据不足"):
        L.append("     （有效日不足 ≠ 没效果；别据此关闸、也别改窗口）")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="R883 中文帖窗口倾斜 · 预注册判据（只读）")
    ap.add_argument("--stats", default="content_stats.jsonl")
    ap.add_argument("--metrics", default="metrics.jsonl")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    stats = pr.load_jsonl(args.stats)
    metrics = pr.load_jsonl(args.metrics)
    joined = pr.join_stats(stats, pr.index_published(metrics))
    lo_h, hi_h = ZW_EXPOSURE_HOURS
    # ⚠️ `age_days` 单位是**天**：不换算会把样本全滤光，脚本还"正常"打印没有样本
    joined = [r for r in joined if lo_h <= r["age_days"] * 24.0 <= hi_h]

    res = evaluate(joined, metrics)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, default=str))
    else:
        print(render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
