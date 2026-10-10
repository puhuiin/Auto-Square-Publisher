#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日项目状态报告 → 钉钉（R723）

★ 为什么单独一个脚本而不是复用 `Notifier`：
  `Notifier` 是**事件驱动**（发布成功/失败/熔断时才发，且错误有 12h 冷却），
  而用户要的是**每天固定一份状态快照**——哪怕一切正常也要报。
  ⇒ 两者语义不同（"有事通知" vs "每日汇报"），硬合并会让正常日的报告被冷却吞掉。

只读不改：仅读 metrics.jsonl 与本地基线，不触碰任何生产状态。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics as st
import sys
import urllib.request
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CST = dt.timezone(dt.timedelta(hours=8))


def _cst(ts: str) -> dt.datetime:
    """UTC ISO → 北京时间（naive）。★ 遥测 ts 一律是 UTC。"""
    return dt.datetime.fromisoformat(ts).replace(tzinfo=None) + dt.timedelta(hours=8)


def load_rows(path: str):
    rows = []
    for line in open(path, encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    rows.sort(key=lambda r: r.get("ts") or "")
    return rows


def baseline() -> dict:
    """R608 浏览基线（供币种档位参考）。缺失返回空 dict。"""
    try:
        d = json.load(open(os.path.join(ROOT, "token_engagement.json"),
                           encoding="utf-8-sig"))
    except Exception:
        return {}
    strong, weak = {}, {}
    min_n = d.get("min_n", 4) if isinstance(d.get("min_n"), int) else 4
    for t, v in (d.get("tokens") or {}).items():
        if not isinstance(v, dict) or not isinstance(v.get("median_views"), (int, float)):
            continue
        key = str(t).upper().replace("$", "")
        if isinstance(v.get("n"), int) and 2 <= v["n"] < min_n:
            weak[key] = float(v["median_views"])
        elif isinstance(v.get("n"), int) and v["n"] >= min_n:
            strong[key] = float(v["median_views"])
    return {"strong": strong, "weak": weak}


def build_report(rows, hours: int) -> str:
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    cut = (now - dt.timedelta(hours=hours)).isoformat()
    pub = [r for r in rows if str(r.get("outcome", "")).startswith("binance_published")
           and (r.get("ts") or "") >= cut]
    rej = [r for r in rows if str(r.get("outcome", "")).startswith("llm_failed")
           and (r.get("ts") or "") >= cut]
    sums = [r for r in rows if r.get("outcome") == "run_summary"
            and (r.get("ts") or "") >= cut]
    fails = [r for r in rows if r.get("outcome") == "publish_failed"
             and (r.get("ts") or "") >= cut]
    be = baseline()

    L = []
    L.append("【币安广场发帖机器人 · 每日状态】")
    L.append("统计窗口：%s ~ %s（%d 小时）"
             % (_cst(cut).strftime("%m-%d %H:%M"),
                _cst(now.isoformat()).strftime("%m-%d %H:%M"), hours))

    # ① 发帖量
    by_day = Counter(_cst(r["ts"]).strftime("%m-%d") for r in pub)
    L.append("① 发帖：%d 篇 %s" % (len(pub), dict(by_day) if by_day else ""))

    # ★ 配额状态：**必须看最近状态，不能用全窗口最小值**。
    #   首版用 `min(所有轮次的 next_slot_frees_min)` ⇒ 被窗口早期的
    #   "配额未满"轮次拉成 1 分钟 ⇒ 在**实际已打满 25/25** 时报"仍有余量"
    #   （实测踩到：线上 quota_blocked=true / 槽位 192 分钟，日报说 1 分钟）。
    #   ⇒ 改用 `sent_24h / max_daily_posts` 判定是否打满（中位数取最近 8 轮）。
    quota_note = "无数据"
    latest = sums[-1] if sums else {}
    sent24 = latest.get("sent_24h")
    cap = latest.get("max_daily_posts")
    nxt_recent = [r.get("next_slot_frees_min") for r in sums[-8:]
                  if isinstance(r.get("next_slot_frees_min"), (int, float))]
    if isinstance(sent24, int) and isinstance(cap, int) and cap > 0:
        if sent24 >= cap:
            near = st.median(sorted(nxt_recent)) if nxt_recent else None
            quota_note = "**已打满 %d/%d**" % (sent24, cap)
            if near is not None:
                quota_note += "（下一槽位约 %d 分钟后）⇒ 配额门拦截是**正常行为**" % near
        else:
            quota_note = "未打满 %d/%d" % (sent24, cap)
    elif nxt_recent:
        quota_note = "下一槽位 %d 分钟后" % min(nxt_recent)
    L.append("   配额：%s" % quota_note)

    # ② 候选与拦截漏斗
    def avg(k, sel=None):
        sel = sums if sel is None else sel
        v = [r.get(k) or 0 for r in sel]
        return sum(v) / len(v) if v else 0
    if sums:
        # ⚠️ 均值会被"配额满的早退轮"拉低（那些轮候选=0 且**根本没抓取**）。
        #   只统计**真正抓过**的轮次（feeds_ok > 0），否则与①的"配额打满"看似矛盾。
        active = [r for r in sums if (r.get("feeds_ok") or 0) > 0] or sums
        L.append("② 管线（%d/%d 轮真正抓取，均值/轮）" % (len(active), len(sums)))
        L.append("   候选 %.0f ｜ 未处理(配额满) %.0f ｜ 无有效代币 %.1f ｜ 单币限流 %.1f"
                 % (avg("candidates", active), avg("unprocessed", active),
                    avg("skipped_no_token", active), avg("skipped_token_limit", active)))
        oks = [r.get("feeds_ok") for r in active if isinstance(r.get("feeds_ok"), (int, float))]
        if oks:
            # ⚠️ 源总数取 **per_feed_yield 的键数**（当轮实际抓到的源），
            #    不是 `len(oks)`（那是**轮数**，会把"21 个源"显示成"51 个源"）。
            last_pf = active[-1].get("per_feed_yield") or {}
            n_src = len(last_pf) if last_pf else None
            L.append("   源健康：中位 %d 个正常%s"
                     % (sorted(oks)[len(oks) // 2],
                        (" / 共 %d 源" % n_src) if n_src else ""))
            failed = active[-1].get("feeds_failed_sources")
            if failed:
                L.append("   ⚠️ 上轮失败源：%s" % failed)
            parked = active[-1].get("feeds_parked_sources")
            if parked:
                L.append("   ⏸️ 上轮停放源：%s" % parked)

    # ③ LLM 与拒稿
    if pub or rej:
        rate = 100 * len(rej) / max(1, len(pub) + len(rej))
        L.append("")
        L.append("③ LLM：调用约 %d 次（成功 %d / 拒稿 %d，拒稿率 %.0f%%）"
                 % (len(pub) + len(rej), len(pub), len(rej), rate))
        rc = Counter((r.get("reason") or "")[:30] for r in rej)
        for k, v in rc.most_common(3):
            L.append("   %2d  %s" % (v, k))
        # ★ 近 12 小时单独算一次：修 bug 前后的拒稿率会天差地别，
        #   混在 24h 窗口里会让人以为" bug 没修好"（R713 踩过同款）。
        recent_cut = (now - dt.timedelta(hours=12)).isoformat()
        r12 = [r for r in rej if (r.get("ts") or "") >= recent_cut]
        p12 = [r for r in pub if (r.get("ts") or "") >= recent_cut]
        if p12 or r12:
            L.append("   近 12h：成功 %d / 拒稿 %d（拒稿率 %.0f%%）← 以此为准"
                     % (len(p12), len(r12), 100 * len(r12) / max(1, len(p12) + len(r12))))
        else:
            # ⚠️ 静默缺失会被读成"近 12h 没问题"，实际是**遥测没覆盖到窗口末端**
            #   （本地跑历史数据时必然如此）⇒ 必须明说。
            L.append("   近 12h：**遥测未覆盖到窗口末端**（本地跑历史数据时的正常现象），"
                     "上面的 24h 数字含更早时段，勿直接当作当前状态")
    prov = Counter(r.get("provider") for r in pub if r.get("provider"))
    if prov:
        L.append("   通道：%s" % dict(prov.most_common(4)))

    # ④ 发布失败（真实故障，要显眼）
    if fails:
        L.append("")
        L.append("④ ⚠️ 发布失败 %d 次（需关注）" % len(fails))
        for k, v in Counter((r.get("error") or "?")[:40] for r in fails).most_common(3):
            L.append("   %2d  %s" % (v, k))

    # ⑤ 语种与币种
    langs = Counter(r.get("lang") for r in pub if r.get("lang"))
    if langs:
        L.append("")
        L.append("⑤ 语种：%s" % dict(langs))
    toks = Counter()
    for r in pub:
        for t in (r.get("tokens") or []):
            toks[str(t).upper().replace("$", "")] += 1
    if toks:
        strong, weak = be.get("strong") or {}, be.get("weak") or {}
        top = ", ".join("%s×%d(基线%s)" % (
            t, n,
            ("%.0f" % strong[t]) if t in strong
            else ("%.0f弱" % weak[t]) if t in weak else "—")
            for t, n in toks.most_common(8))
        L.append("   币种：%s" % top)

    # ⑥ 结论
    L.append("")
    if fails:
        L.append("结论：⚠️ 有发布失败 %d 次，请查看上方明细" % len(fails))
    elif isinstance(sent24, int) and isinstance(cap, int) and cap > 0 and sent24 >= cap:
        # ★ 配额打满是**预期结果**（配额就是上限），不是异常。
        #   此前写"⚠️ 窗口内 0 篇发布" ⇒ 在配额打满的日子**每天都误报警**。
        L.append("结论：✅ 运行正常。配额已打满 %d/%d，"
                 "配额门按设计拦截（省掉整轮抓取），无需处理" % (sent24, cap))
    elif not pub:
        L.append("结论：⚠️ 窗口内 0 篇发布，且配额未满"
                 " ⇒ 可能命中间隔门/无候选/风控拦截，请查看上方明细")
    else:
        L.append("结论：✅ 运行正常，无发布失败")
    return "\n".join(L)


def send_dingtalk(url: str, text: str) -> str:
    body = json.dumps({"msgtype": "text",
                       "text": {"content": text},
                       "at": {"isAtAll": False}}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=20)
    return r.read().decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--metrics", default=os.path.join(ROOT, "metrics.jsonl"))
    ap.add_argument("--url", default=os.environ.get("DINGTALK_WEBHOOK_URL",
                                                     os.environ.get("WEBHOOK_URL", "")).strip())
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发送")
    a = ap.parse_args()

    if not os.path.exists(a.metrics):
        sys.exit("找不到 %s" % a.metrics)
    text = build_report(load_rows(a.metrics), a.hours)
    print(text)
    if a.dry_run:
        return
    if not a.url:
        sys.exit("未配置 webhook（--url 或环境变量 DINGTALK_WEBHOOK_URL / WEBHOOK_URL）")
    print("\n--- 发送 ---")
    print(send_dingtalk(a.url, text))


if __name__ == "__main__":
    main()
