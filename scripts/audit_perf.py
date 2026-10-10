#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发帖系统**巡检报告**（R719，2026-10-10）

★ 为什么做这个：R704~R718 期间，每轮优化都要**手工**回答同一组问题
（时段分布？币种 vs 浏览基线？源效率？限流档位？拒稿？配额达成？），
每次口径都可能不同 ⇒ 结论彼此冲突（R701/R704/R705 各栽过一次）。
   把口径固化进脚本 ⇒ 「同一份数据、同一套算法」，跨轮可比。

只读不改：脚本**不触碰**生产逻辑，纯离线分析（可安全反复跑）。

用法：
    python scripts/audit_perf.py            # 默认最近 24h
    python scripts/audit_perf.py --hours 48 # 自定义窗口
    python scripts/audit_perf.py --local   # 读本地 metrics.jsonl（不联网）
    python scripts/audit_perf.py --since "2026-10-10T06:00"  # 手动切点（对比上线前后）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

REPO = "puhuiin/Auto-Square-Publisher"
API = "https://api.github.com/repos/%s" % REPO
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CST = timezone(timedelta(hours=8))          # 北京时间（遥测 ts 是 UTC）


def _cst(ts: str) -> datetime:
    """UTC ISO → 北京时间（naive）。★ 遥测 ts 一律是 UTC，别再当成本地时间用
    （R704/R705/R708 连续三轮都栽在"把 UTC 当北京"上）。"""
    return datetime.fromisoformat(ts).replace(tzinfo=None) + timedelta(hours=8)


def load_rows(local: bool) -> list:
    if local:
        path = os.path.join(ROOT, "metrics.jsonl")
        raw = open(path, encoding="utf-8").read()
    else:
        import base64
        tok = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True).stdout.strip()
        if not tok:
            sys.exit("未取到 gh token：先 `gh auth login` 或改用 --local")
        h = {"Accept": "application/vnd.github+json", "User-Agent": "audit",
             "Authorization": "Bearer " + tok}

        def api(path):
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(API + path, headers=h), timeout=180).read().decode())

        # ⚠️ metrics.jsonl 常超 1MB ⇒ contents API 会返回空 content，
        #    必须走 blob（实测踩过）。
        sha = api("/contents/metrics.jsonl?ref=main")["sha"]
        d = api("/git/blobs/" + sha)
        raw = base64.b64decode(d["content"]).decode("utf-8", "replace")
    rows = []
    for line in raw.splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    rows.sort(key=lambda r: r.get("ts") or "")
    return rows


def engagement_baseline() -> dict:
    """R608 的浏览基线（币 → 中位浏览）。缺失返回空 dict。"""
    p = os.path.join(ROOT, "token_engagement.json")
    try:
        d = json.load(open(p, encoding="utf-8-sig"))
    except Exception:
        return {}
    out = {}
    for t, v in (d.get("tokens") or {}).items():
        if isinstance(v, dict) and isinstance(v.get("median_views"), (int, float)):
            out[t.upper().replace("$", "")] = float(v["median_views"])
    return out


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return 0.0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def report(rows, hours, since_iso=None, base_limit=25):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cut = (now - timedelta(hours=hours)).isoformat()
    pub = [r for r in rows if str(r.get("outcome", "")).startswith("binance_published")
           and (r.get("ts") or "") >= cut]
    sums = [r for r in rows if r.get("outcome") == "run_summary"
            and (r.get("ts") or "") >= cut]
    rej = [r for r in rows if str(r.get("outcome", "")).startswith("llm_failed")
           and (r.get("ts") or "") >= cut]
    be = engagement_baseline()

    print("=" * 72)
    print("巡检报告｜窗口 %s ~ %s（%d 小时）" % (_cst(cut).strftime("%m-%d %H:%M"),
                                              _cst(now.isoformat()).strftime("%m-%d %H:%M"), hours))
    print("=" * 72)

    # ① 配额达成
    by_day = Counter(_cst(r["ts"]).strftime("%m-%d") for r in pub)
    n_min = by_day[max(by_day)] if by_day else 0
    print("\n① 配额：%d 篇 | 按天 %s | 单日峰值 %d（配额上限 %d）"
          % (len(pub), dict(by_day), n_min, base_limit))
    nxt = [r.get("next_slot_frees_min") for r in sums
           if isinstance(r.get("next_slot_frees_min"), (int, float))]
    if nxt:
        print("   下一槽位：最短 %s 分钟（中位 %s）⇒ %s"
              % (min(nxt), _median(nxt),
                 "**已打满**" if min(nxt) > 30 else "仍有余量"))

    # ② 时段 × 浏览
    print("\n② 时段 × 浏览（中位）")
    buck = defaultdict(list)
    for r in pub:
        buck[_cst(r["ts"]).hour // 3 * 3].append(
            1)  # 先占位；views 不在回执里，见 ⑤
    used = Counter()
    for r in pub:
        used[_cst(r["ts"]).hour // 3 * 3] += 1
    for h in sorted(used):
        bar = "█" * used[h]
        print("   %02d-%02d时 %2d 篇 %s" % (h, h + 3, used[h], bar))

    # ③ 币种 vs 浏览基线
    print("\n③ 币种分布 vs 浏览基线（R608）")
    c = Counter()
    for r in pub:
        for t in (r.get("tokens") or []):
            c[t.upper().replace("$", "")] += 1
    tot = sum(c.values()) or 1
    for t, n in c.most_common(12):
        b = be.get(t)
        # R718 的档位（与 main.py 同口径：中位×1.3 / ×0.7）
        lim = "—"
        if b and be:
            med = _median(be.values())
            if med > 0:
                mult = 2 if b >= med * 1.3 else (0 if b <= med * 0.7 else 1)
                lim = "%d" % (base_limit * mult if mult else max(1, base_limit - 1))
        print("   %-9s %2d 篇 %5.1f%%  基线 %-6s 限流档 %s"
              % (t, n, 100 * n / tot, "%.0f" % b if b else "—", lim))

    # ④ 源效率：入选 → 发布
    print("\n④ 源效率（入选 → 最终发布）")
    fin = Counter()
    for r in sums:
        for name, v in (r.get("per_feed_yield") or {}).items():
            fin[name.split(" (")[0]] += v.get("kept") or 0
    pubsrc = Counter((r.get("source") or "?").split(" (")[0] for r in pub)
    for name, kept in fin.most_common(10):
        print("   %-26s 入选 %4d  发布 %3d  转化 %4.1f%%"
              % (name[:26], kept, pubsrc.get(name, 0),
                 100 * pubsrc.get(name, 0) / max(1, kept)))
    if fin:
        print("   —— 累计：入选 %d → 发布 %d（%.1f%%），源数 %d"
              % (sum(fin.values()), sum(pubsrc.values()),
                 100 * sum(pubsrc.values()) / max(1, sum(fin.values())), len(fin)))

    # ⑤ 拦截漏斗
    print("\n⑤ 拦截漏斗（run_summary 累加）")
    for k, label in (("skipped_no_token", "无有效代币"), ("skipped_token_limit", "单币限流"),
                     ("skipped_batch_dup", "批内重复"), ("skipped_parked", "源停放"),
                     ("skipped_risk_blocked", "风控拦截"), ("unprocessed", "未处理(配额满)")):
        v = sum(r.get(k) or 0 for r in sums)
        print("   %-16s %5d  (%.1f/轮)" % (label, v, v / max(1, len(sums))))
    print("   %-16s %5d  (拒稿率 %.0f%%)"
          % ("LLM 拒稿", len(rej), 100 * len(rej) / max(1, len(pub) + len(rej))))
    rc = Counter((r.get("reason") or "")[:34] for r in rej)
    for k, v in rc.most_common(4):
        print("      %2d  %s" % (v, k))

    # ⑥ 语种（R712）
    langs = Counter(r.get("lang") for r in pub if r.get("lang"))
    if langs:
        print("\n⑥ 输出语种（R710 时段策略 / R712 遥测）")
        for k, v in langs.most_common():
            print("   %-7s %2d 篇" % (k, v))

    # ⑦ 可选：上线前后对比
    if since_iso:
        cst_cut = datetime.fromisoformat(since_iso)
        print("\n⑦ 上线前后对比（切点 %s）" % since_iso)

        def agg(sel):
            return (len(sel),
                    sum(r.get("published") or 0 for r in sums if _cst(r["ts"]) >= cst_cut) if sel else 0)
        pre_pub = [r for r in pub if _cst(r["ts"]) < cst_cut]
        post_pub = [r for r in pub if _cst(r["ts"]) >= cst_cut]
        pre_s = [r for r in sums if _cst(r["ts"]) < cst_cut]
        post_s = [r for r in sums if _cst(r["ts"]) >= cst_cut]

        def rate(ss, k):
            return (sum(r.get(k) or 0 for r in ss) / len(ss)) if ss else 0
        print("   %-12s %8s %8s" % ("指标", "前", "后"))
        for k, label in (("skipped_token_limit", "限流跳过/轮"), ("skipped_no_token", "无代币/轮")):
            print("   %-12s %8.1f %8.1f" % (label, rate(pre_s, k), rate(post_s, k)))
        print("   %-12s %8d %8d" % ("发布篇数", len(pre_pub), len(post_pub)))
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--quota", type=int, default=25,
                    help="24h 配额上限（与 main.py MAX_DAILY_POSTS 默认值一致）")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--since", default=None, help="上线切点，北京时间 'YYYY-MM-DDTHH:MM'")
    a = ap.parse_args()
    report(load_rows(a.local), a.hours, a.since, a.quota)
