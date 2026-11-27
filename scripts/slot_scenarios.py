#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排程方案测算（只读）：用**实测逐小时 vpd** 估几种"把量挪进 UTC 00–02"的收益。

为什么需要它（2026-11-27 实算）：
  同是"多塞 6 篇进窗口"，三种做法的预期差 5 倍——
      S1 cap=24 + 窗内一轮两篇   → +71%
      S2 窗口放宽到 00–04（1 篇/轮）→ +15%
      S3 只放宽两篇、cap 不变     → +33%（且**依赖配额有松量**，见下）
  没有这个表，很容易挑到收益最差的那个，还以为都一样。

★ 口径
  · vpd 来自 `content_stats × metrics`，只收曝光 ∈ [24,72]h（与 R883 判据同源）；
  · 逐小时取**中位**；样本 < 3 的小时视为不可估（不参与）；
  · 方案以「日均 zh / en 篇数」为约束，用 `Σ 篇数_h × vpd_h` 估算"日均浏览/天"。
    这是**量级估算**，不是保证——它假设每小时的新增浏览与观测期内一致。

★★ S3 的陷阱（写死在这里，防止再犯）
  配额是**滚动 24h**：某天窗口能发几篇，≈ 昨天窗口发了几篇（旧帖在同一时刻老化释放）。
  ⇒ 只放宽"一轮两篇"、不动 cap 时，窗口会被锁在昨天的水平（实测 6 篇/天），
    收益远低于上表的 33%——除非当天总量本身有松量（<60）。
  ⇒ **要真正搬家，cap 必须压到能腾出 6 个槽**（当前基线 zh 窗外 ≈30 篇/天 ⇒ cap≈24）。
"""
import argparse
import io
import json
import os
import statistics as st
import sys

# ⚠️ 必须用 __file__ 推目录：被测试 import 时 `sys.path[0]` 是**测试目录**，
#    用它 insert 会找不到同目录的 profit_report（本轮实测直接 ModuleNotFoundError）。
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import profit_report as pr  # noqa: E402  ← 口径唯一实现

ZH_HOURS = list(range(0, 14)) + [22, 23]   # R710：北京 06–22 = UTC 22–14
EN_HOURS = list(range(14, 22))             # 北京 22–06 = UTC 14–22
WINDOW = (0, 2)
MAX_DAILY = 60          # 与生产 MAX_DAILY_POSTS 一致（60/24h 滚动）


def hourly_vpd(rows):
    out = {}
    for h in range(24):
        v = [r["vpd"] for r in rows if int(r["meta"]["ts"][11:13]) == h]
        if len(v) >= 3:
            out[h] = st.median(v)
    return out


def daily_count(metrics, lang_set, recent_days=5):
    """★ 用**最近 N 个完整日**的篇数中位（不含最后一天：它通常是半天）。

    为什么不用全窗口中位：全窗口含早期 en 较少的时段，会把 en 低估 ~15%，
    进而把"窗口还能塞几篇"算高（本轮实测 en 全窗 20.5 vs 近 5 日 24）。
    """
    per = {}
    for m in metrics:
        if not str(m.get("outcome", "")).startswith("binance_published"):
            continue
        lg = m.get("lang") or "zh-CN"
        if lg not in lang_set:
            continue
        d = str(m.get("ts", ""))[:10]
        per[d] = per.get(d, 0) + 1
    if not per:
        return None
    days = sorted(per)[:-1] if len(per) > recent_days else sorted(per)
    vals = [per[d] for d in days[-recent_days:]]
    return st.median(vals) if vals else None


def scenarios(zh_vpd, en_vpd, zh_per_day, en_per_day):
    zh_h = [h for h in ZH_HOURS if h in zh_vpd]
    en_h = [h for h in EN_HOURS if h in en_vpd]
    rest = [h for h in zh_h if h not in range(*WINDOW)]
    wid = WINDOW[1] - WINDOW[0]

    def total(counts, table):
        return sum(n * table.get(h, 0.0) for h, n in counts.items())

    out = {}
    # S0 现状
    c0 = {h: zh_per_day / len(zh_h) for h in zh_h}
    out["S0 现状（zh 均摊）"] = total(c0, zh_vpd) + total(
        {h: en_per_day / len(en_h) for h in en_h}, en_vpd)

    # S1：窗内一轮两篇。★ 容量必须由**配额恒等式**导出，不能只看 zh 日产量：
    #     zh_out + W + en ≤ MAX_DAILY  ⇒  cap = MAX - en - W
    #   （旧式 `cap = zh_per_day - W` 忽略了 en，把 cap 高估 3 篇、收益高估 ~6pp。）
    win_cap = 2 * (wid * 3)                          # 每槽 2 篇 × 窗内槽数
    win_posts = max(0.0, min(win_cap, MAX_DAILY - en_per_day, zh_per_day))
    # cap 同时受两个约束：① 配额恒等式（zh_out + W + en ≤ MAX）；② 实际日产量（不能凭空多写）
    zh_out = max(0.0, min(MAX_DAILY - en_per_day - win_posts, zh_per_day - win_posts))
    c1 = {h: win_posts / wid for h in range(*WINDOW)}
    c1.update({h: zh_out / len(rest) for h in rest})
    out["S1 cap=%d + 窗内两篇（W=%d）" % (int(zh_out), int(win_posts))] = (
        total(c1, zh_vpd) + total({h: en_per_day / len(en_h) for h in en_h}, en_vpd))

    # S2 窗口放宽到 00–04、仍 1 篇/轮
    wide = 4
    rest2 = [h for h in zh_h if h not in range(0, wide)]
    first = zh_per_day - (wide * 3) if zh_per_day > wide * 3 else 0
    c2 = {h: 3.0 for h in range(0, wide)}
    c2.update({h: max(0.0, first) / len(rest2) for h in rest2})
    out["S2 窗口放宽 00–04（1 篇/轮）"] = (
        total(c2, zh_vpd) + total({h: en_per_day / len(en_h) for h in en_h}, en_vpd))

    # S3 只放宽两篇、cap 不变（窗口被锁在昨日水平：按 6 篇估）
    locked = 6.0
    rest3 = [h for h in zh_h if h not in range(*WINDOW)]
    c3 = {h: locked / wid for h in range(*WINDOW)}
    c3.update({h: (zh_per_day - locked) / len(rest3) for h in rest3})
    out["S3 只放宽两篇、cap 不变"] = (
        total(c3, zh_vpd) + total({h: en_per_day / len(en_h) for h in en_h}, en_vpd))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="排程方案测算（只读）")
    ap.add_argument("--stats", default="content_stats.jsonl")
    ap.add_argument("--metrics", default="metrics.jsonl")
    args = ap.parse_args(argv)

    stats = pr.load_jsonl(args.stats)
    metrics = pr.load_jsonl(args.metrics)
    joined = [r for r in pr.join_stats(stats, pr.index_published(metrics))
              if 1.0 <= r["age_days"] <= 3.0]
    zh = [r for r in joined if r["meta"].get("lang") in ("zh-CN", "zh-TW")]
    en = [r for r in joined if r["meta"].get("lang") == "en"]
    zv, ev = hourly_vpd(zh), hourly_vpd(en)
    zd, ed = daily_count(metrics, {"zh-CN", "zh-TW"}), daily_count(metrics, {"en"})

    print("样本：zh n=%d ｜ en n=%d（曝光 24~72h）" % (len(zh), len(en)))
    print("日均发布（窗口内中位）：zh %s ｜ en %s" % (zd, ed))
    if not zv or not ev or not zd or not ed:
        print("数据不足，无法测算")
        return 1

    sc = scenarios(zv, ev, float(zd), float(ed))
    base = sc["S0 现状（zh 均摊）"]
    print("\n%-30s %10s %9s" % ("方案", "日均浏览", "相对现状"))
    for name, v in sc.items():
        print("%-30s %10.0f %8.0f%%" % (name, v, 100 * (v / base - 1)))

    print("\n[!] S3 依赖配额松量：滚动 24h 下「某天窗口能发几篇」≈「昨天窗口发了几篇」，"
          "不动 cap 时窗口会被锁住（按 6 篇估）。要真搬家，cap 得压到能腾出 6 个槽。")
    print("⚠️ 以上是量级估算，不含同槽抢量（2 篇/槽）风险——该风险只能实验内测"
          "（见 zh_window_prereg.py 的 density_breakdown）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
