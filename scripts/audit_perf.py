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
import time
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

        def api(path, tries=4):
            """⚠️ 大 blob（metrics.jsonl 已 >4MB）**偶发 IncompleteRead**：
            网络抖动会读到一半就断。必须重试，否则巡检脚本本身变成不稳定源。"""
            import time as _t
            last = None
            for i in range(tries):
                try:
                    return json.loads(urllib.request.urlopen(
                        urllib.request.Request(API + path, headers=h),
                        timeout=240).read().decode())
                except Exception as e:
                    last = e
                    _t.sleep(3 * (i + 1))
            raise last

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
    """R608 的浏览基线（币 → 中位浏览）。缺失返回空 dict。

    ⚠️ **必须复刻生产的 `min_n` 过滤**（R608 的加载器只收样本量达标的币）。
       首版这里不过滤 ⇒ 把 n=1 的冷门币也算进中位 ⇒ **算出的档位与生产不一致**，
       报告会说"ETH 是高档 6 篇"而生产其实按 3 篇跑。
       ⇒ 口径要与 `MultiLLMEngine._load_token_engagement` 逐字一致。
    """
    p = os.path.join(ROOT, "token_engagement.json")
    try:
        d = json.load(open(p, encoding="utf-8-sig"))
    except Exception:
        return {}
    min_n = d.get("min_n", 4) if isinstance(d.get("min_n"), int) else 4
    out = {}
    for t, v in (d.get("tokens") or {}).items():
        if not isinstance(v, dict):
            continue
        n = v.get("n")
        if not (isinstance(n, int) and n >= min_n):
            continue                       # 样本不足 ⇒ 生产也不参与加权
        if isinstance(v.get("median_views"), (int, float)):
            out[t.upper().replace("$", "")] = float(v["median_views"])
    return out


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return 0.0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def production():
    """★★ R731：导入生产模块，**不再复刻任何生产口径**。

    ⚠️ 为什么必须这样：R719 为了"验证一致性"在脚本里重算了限流档位，
       结果**连错三处**（全部实测，报告从 R719 上线起一直是错的）：
       ① `base_limit * mult` 里的 `base_limit` 传的是**日配额**（25/40），
          而档位该乘的是 `TOKEN_DAILY_LIMIT`(3) ⇒ 报告印出"限流档 50/25/24"，
          真实档位是 **6/3/2**（量级差一个数量级，R622「量纲必须对齐」同型）。
       ② 档位基线用脚本的 `engagement_baseline()`（min_n=4 ⇒ 8 币、中位 88.5），
          而生产 `_token_daily_limit` 自 R722 起用 **n>=2**（13 币、中位 83.0）
          ⇒ 分档边界本就不同（R719 刚因同一类口径分叉修过一次）。
       ③ `--quota` 默认值写死 25，而 R724 已把生产默认提到 **40**。
    ⇒ 三处同根：**脚本自己算了一遍生产逻辑**。口径只能有一份实现
      （R722 纪律）⇒ 这里直接调生产函数，让它成为唯一事实源。
    ⚠️ 导入失败时**必须喊出来**（R718 教训：静默降级会让缺陷完全隐身），
       并把受影响的列显示成 `?` ——**绝不退回自己算一个数**，
       因为"算错的报告比没有报告更糟"（R719 的立项理由）。
    """
    import importlib.util
    try:
        path = os.path.join(ROOT, "main.py")
        spec = importlib.util.spec_from_file_location("_prod_main_r731", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_prod_main_r731"] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:                       # pragma: no cover - 环境异常
        print("⚠️⚠️ 无法导入生产模块 main.py（%s: %s）\n"
              "   ⇒ 配额上限与限流档位**无法按生产口径显示**（列显示 ?）。\n"
              "   ⇒ 不退回脚本自算：R719 的三处错档位正是自算造成的。"
              % (type(e).__name__, e))
        return None


def report(rows, hours, since_iso=None, quota=None, prod=None):
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
    #   ★ R731：配额上限取**生产默认值**（R724 已 25→40），不再写死在脚本里。
    by_day = Counter(_cst(r["ts"]).strftime("%m-%d") for r in pub)
    n_min = by_day[max(by_day)] if by_day else 0
    if quota is None:
        quota = getattr(prod, "MAX_DAILY_POSTS", None) if prod else None
    print("\n① 配额：%d 篇 | 按天 %s | 单日峰值 %d（配额上限 %s）"
          % (len(pub), dict(by_day), n_min,
             quota if isinstance(quota, int) else "?"))
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
    #   ★ R731：基线分「强样本」(n>=min_n，R608 全权) / 「弱样本」(2<=n<min_n，
    #     R722 半权)。此前只显示强样本 ⇒ HYPE(236)/LINK(193) 这两枚**浏览最高**
    #     的币显示成"基线 —"，恰好把 R722 的效果藏了起来（它们正是 R722 的受益币）。
    print("\n③ 币种分布 vs 浏览基线（强=R608 全权 / 弱=R722 半权）｜限流档取生产口径")
    weak = {}
    if prod:
        try:
            weak = dict(prod.NewsFetcher._load_weak_views())
        except Exception as e:
            print("   ⚠️ 弱样本基线读取失败（%s）⇒ 弱样本币会显示成 —" % e)
    c = Counter()
    for r in pub:
        for t in (r.get("tokens") or []):
            c[t.upper().replace("$", "")] += 1
    tot = sum(c.values()) or 1
    for t, n in c.most_common(12):
        b, tag = be.get(t), "强"
        if b is None:
            b, tag = weak.get(t), "弱"
        # ★★ 限流档**直接问生产**，不在脚本里重算（R719 自算连错三处，见 production()）
        lim = "?"
        if prod:
            try:
                lim = "%d" % prod._token_daily_limit(t)
            except Exception:
                lim = "?"
        print("   %-9s %2d 篇 %5.1f%%  基线 %-9s 限流档 %s"
              % (t, n, 100 * n / tot,
                 ("%.0f(%s)" % (b, tag)) if b else "—", lim))

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

    # ⑤½ 逐币限流拦截分布（R728 落的字段，R731 把它接进报告）
    #   ★ 为什么必须进报告：R728 的立项问题是「档位是否与候选供给匹配」，
    #     而答案只在这个字段里。上一轮是**手工**翻 metrics 才读出来的
    #     ——手工读出来的结论下一轮还得再手工读一遍（R719 的立项理由）。
    #   ⚠️ 只有「评估数够多」的轮次才有判读价值：配额/间隔早退轮只评估 1~2 条，
    #     拦截自然是 0（R729 实测踩过这个小样本假象）⇒ 必须同时报评估基数。
    blk = Counter()
    rounds_with = 0
    for r in sums:
        d = r.get("token_limit_blocked_by")
        if isinstance(d, dict) and d:
            rounds_with += 1
            for k, v in d.items():
                blk[str(k).upper().replace("$", "")] += v or 0
    print("\n⑤½ 逐币限流拦截（R728）｜有拦截记录的轮次 %d/%d" % (rounds_with, len(sums)))
    if blk:
        tb = sum(blk.values()) or 1
        for t, v in blk.most_common(10):
            lim = "?"
            if prod:
                try:
                    lim = "%d" % prod._token_daily_limit(t)
                except Exception:
                    lim = "?"
            print("   %-9s 被拦 %4d 次 %5.1f%%  （该币限流档 %s）"
                  % (t, v, 100 * v / tb, lim))
        print("   ★ 判读：占比极高的币 = 候选供给远超其档位 ⇒ 档位与供给错配；"
              "分布均匀 ⇒ 供给维度不是关键")
    else:
        print("   ⚠️ 窗口内无逐币拦截记录 ⇒ **无法归因**（该字段 R728 上线于 "
              "10-10 19:30；且只在真正评估到候选的轮次才会产生）")

    # ⑥ 语种（R712）
    langs = Counter(r.get("lang") for r in pub if r.get("lang"))
    if langs:
        print("\n⑥ 输出语种（R710 时段策略 / R712 遥测）")
        for k, v in langs.most_common():
            print("   %-7s %2d 篇" % (k, v))

    # ⑥½ 按语种看浏览效果（★ 验证 R710 时段语种策略的关键维度）
    #   数据源：content_stats.jsonl（创作者中心导出，按 content_id join）
    #   ⚠️ 没有浏览数据时**明确说明**，不静默跳过——否则"看不到差异"
    #     会被误读成"三种语言效果一样"（R704/R705 栽过的坑）。
    lang_views = defaultdict(list)
    stats_path = os.path.join(ROOT, "content_stats.jsonl")
    if os.path.exists(stats_path):
        cid2v = {}
        for line in open(stats_path, encoding="utf-8"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            v = d.get("views")
            if isinstance(v, (int, float)):
                cid2v[str(d.get("content_id"))] = v
        hit = 0
        for r in pub:
            lg = r.get("lang")
            v = cid2v.get(str(r.get("content_id")))
            if lg and v is not None:
                lang_views[lg].append(v)
                hit += 1
        print("\n⑥½ 按语种 × 浏览（join content_stats，命中 %d/%d 篇）" % (hit, len(pub)))
        if lang_views:
            print("   %-8s %6s %10s %10s" % ("语种", "样本", "中位浏览", "均值"))
            for lg, vs in sorted(lang_views.items()):
                print("   %-8s %6d %10.0f %10.0f" % (lg, len(vs), _median(vs), sum(vs) / len(vs)))
            print("   ★ 样本 <5 时**不可据此下结论**（R704 的教训）")
        else:
            print("   ⚠️ 遥测里没有 lang 字段（R712 上线于 10-10 10:59），"
                  "或 content_id 未命中 ⇒ 本轮无法按语种归因")
    else:
        print("\n⑥½ 按语种 × 浏览：**缺 content_stats.jsonl** ⇒ 无法归因"
              "（需从创作者中心导出后重跑）")

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
    # ★ R731：默认 None ⇒ 取**生产** MAX_DAILY_POSTS（R724 已 25→40）。
    #   写死默认值的代价是实测过的：R719 写 25，R724 提量后报告一直显示旧上限。
    ap.add_argument("--quota", type=int, default=None,
                    help="24h 配额上限；默认读生产 main.MAX_DAILY_POSTS")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--since", default=None, help="上线切点，北京时间 'YYYY-MM-DDTHH:MM'")
    a = ap.parse_args()
    report(load_rows(a.local), a.hours, a.since, a.quota, production())
