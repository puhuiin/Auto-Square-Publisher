#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开闸后即时体检（只读）：不依赖浏览导出，只看**遥测**就能判断处置臂有没有跑歪。

为什么需要（2026-11-27）：
  S1（`ZH_OFFWIN_CAP=27` + `ZH_WINDOW_DOUBLE_PER_RUN=1`）预期 +62%，
  但它有两条**当场**就能看出来的失效形态：
    ① **填不满**：窗内 zh 帖没涨到 ~12/天（容量或候选不足）；
    ② **砍过头**：总量掉破护栏（<50/天，预注册回滚线）。
  这两条等 6 天判定再发现，等于白跑一轮、还白掉产量。
  ⇒ 开闸后每几小时跑一次本脚本；红了就立刻回滚（两个变量设 0）。

★ 口径
  · 全部指标取自 `metrics.jsonl` 的 **published 行**（不依赖人工导出）；
  · 「窗内」= UTC 00–02（北京 08–10），「中文」= zh-CN / zh-TW（缺 lang 按 zh-CN，同 R708）；
  · 基线取「开闸日之前」的最近 N 天（默认 7），处置期取「开闸日之后」的全部日子；
  · **不做显著性判断**——那是预注册工具的活；这里只报"有没有跑歪"。
"""
import argparse
import collections
import datetime as dt
import io
import json
import statistics as st
import sys

WINDOW = (0, 2)
ZH = {"zh-CN", "zh-TW"}
EN_HOURS = range(14, 22)          # R710：北京 22–06
SLOT_MIN = 20                     # 与 cron 同源的"一轮"
TOTAL_FLOOR = 50                  # 预注册回滚线：总量中位 < 50/天


def load(path):
    rows = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def published(rows):
    return [r for r in rows if str(r.get("outcome", "")).startswith("binance_published")]


def _lang(r):
    lg = r.get("lang") or "zh-CN"
    return lg if lg in ("zh-CN", "zh-TW", "en") else "zh-CN"


def _hour(r):
    return int(str(r.get("ts", ""))[11:13] or 0)


def summarize(pubs, since=None):
    """按日聚：窗内 zh 篇数、总篇数、每槽篇数分布、en 小时分布。"""
    days = collections.defaultdict(lambda: {"win_zh": 0, "total": 0, "slots": collections.Counter()})
    en_prof = collections.Counter()
    for r in pubs:
        d = str(r.get("ts", ""))[:10]
        if not d or (since and d < since):
            continue
        days[d]["total"] += 1
        lg, h = _lang(r), _hour(r)
        if lg in ZH and WINDOW[0] <= h < WINDOW[1]:
            days[d]["win_zh"] += 1
        if lg in ZH:
            mm = int(str(r.get("ts", ""))[14:16] or 0) // SLOT_MIN * SLOT_MIN
            # ⚠️ 槽键 = **小时 + 20 分钟分桶**：
            #   · 初版漏了小时 ⇒ 所有 :00/:20/:40 跨小时并成一个键，把"每槽篇数"算成 8~13；
            #   · 第二版又直接把 HH:MM 当键 ⇒ 同槽（00:05 与 00:12）被拆成两个槽，
            #     "一轮两篇有没有发生"永远读不到。两个坑都实测踩过。
            _mm = int(str(r.get("ts", ""))[14:16] or 0) // SLOT_MIN * SLOT_MIN
            days[d]["slots"]["%s:%02d" % (str(r.get("ts", ""))[11:13], _mm)] += 1
        if lg == "en" and h in EN_HOURS:
            en_prof[h // 3] += 1
    return days, en_prof


def _med(v):
    return st.median(v) if v else None


def report(rows, since=None, base_days=7):
    pubs = published(rows)
    if not pubs:
        return ["没有 published 行，无法体检"]
    cut = since or str(pubs[-1].get("ts", ""))[:10]
    # 基线窗口：开闸日前 N 个自然日
    cut_d = dt.date.fromisoformat(cut[:10])
    b_from = str(cut_d - dt.timedelta(days=base_days))
    base, _ = summarize(pubs, b_from)
    base = {d: v for d, v in base.items() if d < cut[:10]}
    treat, en_t = summarize(pubs, cut[:10])

    L = ["【开闸后即时体检】只读；窗 = UTC %02d–%02d ｜ 开闸日 %s ｜ 基线 %s~（%d 天）"
         % (WINDOW[0], WINDOW[1], cut[:10], b_from, len(base))]

    if not treat:
        L.append("  处置期还没有完整日数据（最早一天可能未走完）⇒ 稍后再看。")
        return L

    b_win = _med([v["win_zh"] for v in base.values()]) if base else None
    b_tot = _med([v["total"] for v in base.values()]) if base else None
    t_win = _med([v["win_zh"] for v in treat.values()])
    t_tot = _med([v["total"] for v in treat.values()])

    L.append("  窗内 zh 帖/天：基线 %s → 处置 %s" % (b_win, t_win))
    L.append("  总发布/天：   基线 %s → 处置 %s（护栏 ≥%d）" % (b_tot, t_tot, TOTAL_FLOOR))

    # 每槽篇数分布（看"一轮两篇"到底有没有发生）
    slot_cnt = collections.Counter()
    for v in treat.values():
        slot_cnt.update(v["slots"].values())
    L.append("  每 20 分钟槽的篇数分布（处置期）：%s"
             % dict(sorted(slot_cnt.items())))

    # en 小时分布漂移
    base_en, _ = summarize(pubs, b_from)
    en_b = collections.Counter()
    for r in pubs:
        d = str(r.get("ts", ""))[:10]
        if d and b_from <= d < cut[:10] and _lang(r) == "en" and _hour(r) in EN_HOURS:
            en_b[_hour(r) // 3] += 1
    def _share(c):
        t = sum(c.values())
        return {k: v / t for k, v in c.items()} if t else {}
    pb, pt = _share(en_b), _share(en_t)
    tvd = 0.5 * sum(abs(pb.get(k, 0.0) - pt.get(k, 0.0)) for k in set(pb) | set(pt))
    L.append("  en 小时分布漂移 TVD = %.2f（≤0.25 为正常）" % tvd)

    # 拦截计数（闸门是否在工作）
    blocked = sum(1 for r in rows if r.get("zh_window_blocked") is True)
    caps = [r.get("zh_window_cap") for r in rows if r.get("zh_window_cap") is not None]
    L.append("  闸门：zh_window_blocked 行 %d ｜ 最近 cap = %s"
             % (blocked, caps[-1] if caps else "未设"))

    # 判读（只报"有没有跑歪"，不做显著性）
    L.append("")
    verdicts = []
    if t_tot is not None and t_tot < TOTAL_FLOOR:
        verdicts.append("🔴 总量跌破护栏（%s < %d）⇒ **建议回滚**（两个变量设 0）" % (t_tot, TOTAL_FLOOR))
    if b_win is not None and t_win is not None and t_win <= b_win:
        verdicts.append("🟡 窗内 zh 没有增加（%s → %s）⇒ 闸门/双篇可能没生效，先查 cap 与间隔门"
                        % (b_win, t_win))
    if tvd > 0.25:
        verdicts.append("🟡 en 分布漂移过大（TVD %.2f）⇒ 英文夜窗被影响，检查时段判定" % tvd)
    if not verdicts:
        verdicts.append("🟢 未见跑歪：窗内在涨、总量与 en 分布正常（显著性与否看预注册工具）")
    L.extend("  " + v for v in verdicts)
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="开闸后即时体检（只读）")
    ap.add_argument("--metrics", default="metrics.jsonl")
    ap.add_argument("--since", default="", help="开闸日 YYYY-MM-DD（默认取最后一条的日期）")
    ap.add_argument("--base-days", type=int, default=7)
    args = ap.parse_args(argv)
    for line in report(load(args.metrics), args.since or None, args.base_days):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
