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
# ★ R818：浏览加权数据文件（相对仓库根读，便于测试注入）。它的 `as_of` 由创作者
#   后台**手工导出**决定——导出停更时文件内容一天天变旧，而读侧不看 as_of，
#   所以"数据是哪天的"必须由日报明确报出来（只报事实，不判阈值）。
_ENGAGEMENT_FILE = os.path.join(ROOT, "token_engagement.json")


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


_MR_MODULE = None


def _load_metrics_report():
    """R804：加载 metrics_report（daily_report 不能 import main）。

    R750 起 ⑥ 已用它做 quality_scan；R804 的故事级漏斗复用同一实例，
    避免两次 exec 326KB 脚本，也避免两条口径各加载一份。
    """
    global _MR_MODULE
    if _MR_MODULE is None:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "metrics_report_r750", os.path.join(ROOT, "scripts", "metrics_report.py"))
        _mr = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mr)
        _MR_MODULE = _mr
    return _MR_MODULE


def build_report(rows, hours: int) -> str:
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    cut = (now - dt.timedelta(hours=hours)).isoformat()
    pub = [r for r in rows if str(r.get("outcome", "")).startswith("binance_published")
           and (r.get("ts") or "") >= cut]
    sums = [r for r in rows if r.get("outcome") == "run_summary"
            and (r.get("ts") or "") >= cut]
    fails = [r for r in rows if r.get("outcome") == "publish_failed"
             and (r.get("ts") or "") >= cut]
    be = baseline()
    _fn = None

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

    # ★★ R818：**不能取 `sums[-1]`**——那一行可能是「间隔门早退」的**残行**：
    #   main.py 的 `append_run_summary(gap_early_exit=True, fetch_elapsed_sec=…,
    #   run_elapsed_sec=…)` 只带三个计时键，**没有** sent_24h / max_daily_posts /
    #   intel_* ⇒ 整条 ① 配额行与输入新鲜度行一起退化。
    #   生产实证（10-20 23:25Z 本地跑线上件）：① 从「未打满 56/60」变成
    #   「下一槽位 1 分钟后」、情报字段全变「未知」。间隔门早退 22/93 轮（24%）
    #   ⇒ 这不是罕见路径，是"约每四天错一次"。
    #   ⇒ 取**最后一个带该字段**的轮（找不到才回落 sums[-1]，且不静默）。
    def _last_with(key):
        for r in reversed(sums):
            if r.get(key) is not None:
                return r
        return sums[-1] if sums else {}

    latest = _last_with("sent_24h")
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
        # ★★ R815：`unprocessed` 是**残差**（candidates − 已发 − 各类跳过 − 异常），
        #   语义是"本轮没轮到评估的候选"，**不是"配额满"的证据**。
        #   生产实证 10-20 10:43Z：同一条日报里 ① 写「配额未打满 52/60」、
        #   ② 写「未处理(配额满) 79」——自己打自己。main.py R713 的注释也早写明
        #   `unprocessed == candidates`（入口早退）时**瓶颈是本门（间隔门），
        #   不是配额也不是供给**。⇒ 只报事实，不冒充成因。
        L.append("   候选 %.0f ｜ 未处理(本轮未评估) %.0f ｜ 无有效代币 %.1f ｜ 单币限流 %.1f"
                 % (avg("candidates", active), avg("unprocessed", active),
                    avg("skipped_no_token", active), avg("skipped_token_limit", active)))
        # ★ R815：接上 R714 的 `gap_early_exit` 遥测（此前**只写不读**——R714 的
        #   设计意图"让报表能区分 配额满 / 间隔挡 / 真没稿"从未闭环）。这些轮
        #   feeds_ok=0、不进 active，所以必须在 sums 上数。
        _gap_exits = [r for r in sums if r.get("gap_early_exit")]
        if _gap_exits:
            L.append("   ⏱️ 间隔门早退 %d/%d 轮（距上篇 < MIN_POST_GAP_MIN，未抓取）"
                     % (len(_gap_exits), len(sums)))
        oks = [r.get("feeds_ok") for r in active if isinstance(r.get("feeds_ok"), (int, float))]
        if oks:
            # ★★ R815：源健康的两个数必须**同轮同口径**。
            #   旧实现把「各轮 feeds_ok 的**中位**」与「末轮 per_feed_yield 的**键数**」
            #   并排 ⇒ 生产实证 10-20 10:43Z 打出「中位 24 个正常 / 共 22 源」：
            #   中位数比"总数"还大，读者会以为源集体掉线（实测 feeds_ok=23、
            #   failed=1、parked=1，而 per_feed_yield 只有 22 个键）。两个数不同源：
            #     · feeds_ok          = 该轮 HTTP 拉取成功的源数（跨轮取中位）
            #     · per_feed_yield 键 = 该轮**过滤后仍有留存条目**的源数（≤ feeds_ok）
            #   ⇒ 改为「跨轮中位正常数」+「末轮三元结构」，两段各自自洽。
            _last = active[-1]
            _ok_l = _last.get("feeds_ok")
            _parts = []
            if isinstance(_ok_l, (int, float)):
                _parts.append("本轮 %d 正常" % _ok_l)
                for _k, _lab in (("feeds_failed", "失败"), ("feeds_parked", "停放")):
                    _v = _last.get(_k)
                    if isinstance(_v, (int, float)) and _v:
                        _parts.append("%s %d" % (_lab, _v))
            L.append("   源健康：24h 中位 %d 个正常%s"
                     % (sorted(oks)[len(oks) // 2],
                        ("；" + " / ".join(_parts)) if _parts else ""))
            failed = active[-1].get("feeds_failed_sources")
            if failed:
                L.append("   ⚠️ 上轮失败源：%s" % failed)
            parked = active[-1].get("feeds_parked_sources")
            if parked:
                L.append("   ⏸️ 上轮停放源：%s" % parked)

    # ★★ R818：**导入型输入的新鲜度**（此前对运营完全不可见）。
    #   为什么必须显式渲染：`token_engagement.json` 由创作者后台**手工导出**，而读侧
    #   （main._load_token_engagement）只按 `min_n` 过滤、**完全不看 as_of** ⇒ 导出停更
    #   时陈旧触达数据一直静默驱动排序（实测 as_of 停在 10-16，已 4 天前），运行日志也
    #   只说"N 币有基线"。R811 只给**盈利脚本**加了新鲜度判断，日报侧一直是盲的
    #   （R612 家族：程序在用 ≠ 人在看）。
    #   ⚠️ **只报事实、不判阈值**——"该不该停用"是运营决策，报表不拍期限（R670 教训：
    #   拍出来的阈值第二版也错）。情报侧同理：读 run_summary 自带的
    #   `campaign_guidance_usable`/`intel_age_hours`（生产已落盘），**不复制 12h 那个阈值**。
    _fresh = []
    _asof = None
    try:
        import json as _json
        with open(_ENGAGEMENT_FILE, encoding="utf-8-sig") as _fh:
            _asof = (_json.load(_fh) or {}).get("as_of")
    except Exception:
        _asof = None
    if isinstance(_asof, str) and _asof.strip():
        _s = _asof.strip()
        _age = ""
        try:
            _d = dt.datetime.fromisoformat(_s.replace("Z", "+00:00"))
            # ⚠️ 带**非 UTC 偏移**的 as_of 必须先换算到 UTC 再摘 tzinfo——直接
            #    `replace(tzinfo=None)` 会静默错位（`+08:00` 的 13:39 会被当成
            #    UTC 13:39，年龄凭空多 8 小时）。真实文件是 `…Z`（UTC），
            #    但"读的人以为自己对"正是这类 bug 的温床。
            if _d.tzinfo is not None:
                _d = _d.astimezone(dt.timezone.utc)
            _d = _d.replace(tzinfo=None)
            _h = (now - _d).total_seconds() / 3600.0
            _age = "（%.1f 天前）" % (_h / 24.0) if _h >= 48 else "（%.1f 小时前）" % _h
        except Exception:
            _age = ""
        _fresh.append("浏览加权数据 as_of %s%s" % (_s[:16], _age))
    else:
        # 未知不是 0：文件缺失/无 as_of 时必须明说，别让读者以为"数据很新"
        _fresh.append("浏览加权数据 as_of 未知")
    if latest:
        _intel = _last_with("intel_age_hours")
        _gu = _intel.get("campaign_guidance_usable")
        _ia = _intel.get("intel_age_hours")
        _fresh.append("情报 guidance 可用 %s ｜ 情报 age %s h（取最近一次真正评估过情报的轮）" % (
            _gu, ("%.1f" % _ia) if isinstance(_ia, (int, float)) else "未知"))
    L.append("   📀 输入新鲜度：%s" % " ｜ ".join(_fresh))

    # ③ LLM 与尝试
    # ★★ R800：**尝试口径**。旧实现只把 `llm_failed` 当拒稿，漏掉了 transport 层
    #   failover 拒稿（llm_rejected）与 numbers/quality 门拒稿——生产实证 10-19
    #   窗口：59 篇发布 + 33 条失败尝试（transport 24 / numbers 7 / quality 2），
    #   日报却写"调用约 61 次（拒稿 2）"，少报 35%；其中 14 次「账户余额耗尽」
    #   完全不可见（R731 同族：报告与生产口径必须同源）。
    #   ⚠️ 情报路径（campaign_intel）是独立的辅助调用，不计入故事尝试。
    att = [r for r in rows
           if r.get("stage") != "campaign_intel"
           and str(r.get("outcome", "")) in ("llm_rejected", "llm_failed")
           and (r.get("ts") or "") >= cut]
    # ★ 账户级故障只认生产打的 `[credit 24h]` 标签（main._is_credit_exhausted 的
    #   唯一实现），报告不复制正则——两处分类必然漂移（R731 纪律）。
    cred = [r for r in rows
            if str(r.get("reason") or "").startswith("[credit 24h]")
            and (r.get("ts") or "") >= cut]
    if pub or att:
        rate = 100 * len(att) / max(1, len(pub) + len(att))
        L.append("")
        L.append("③ LLM：尝试 %d 次（成功 %d ｜ 失败 %d，失败率 %.0f%%）"
                 % (len(pub) + len(att), len(pub), len(att), rate))
        # ★★ R815：旧实现 `stage or "?"` 把**故事级中止**（llm_failed 无 stage）
        #   打成字面量 "?"——生产实证 10-20：132 条失败里有 36 条叫"?"（27%），
        #   读者根本看不懂，而它们其实全是「全部提供商处于冷却期，无可用通道」
        #   （由下一行的 reason 榜点名）。⇒ 按 record_scope 结构化命名，
        #   不在这里猜成因（成因由 reason 榜负责）。
        sc = Counter(r.get("stage") or ("整篇中止" if r.get("record_scope") == "story"
                                        else "未标注阶段") for r in att)
        if sc:
            L.append("   失败构成：%s"
                     % " ｜ ".join("%s %d" % (k, v) for k, v in sc.most_common(4)))
        rc = Counter((r.get("reason") or "")[:30] for r in att)
        for k, v in rc.most_common(3):
            L.append("   %2d  %s" % (v, k))
        # ★ 近 12 小时单独算一次：修 bug 前后的失败率会天差地别，
        #   混在 24h 窗口里会让人以为" bug 没修好"（R713 踩过同款）。
        recent_cut = (now - dt.timedelta(hours=12)).isoformat()
        a12 = [r for r in att if (r.get("ts") or "") >= recent_cut]
        p12 = [r for r in pub if (r.get("ts") or "") >= recent_cut]
        if p12 or a12:
            L.append("   近 12h：成功 %d / 失败 %d（失败率 %.0f%%）← 以此为准"
                     % (len(p12), len(a12), 100 * len(a12) / max(1, len(p12) + len(a12))))
        else:
            # ⚠️ 静默缺失会被读成"近 12h 没问题"，实际是**遥测没覆盖到窗口末端**
            #   （本地跑历史数据时必然如此）⇒ 必须明说。
            L.append("   近 12h：**遥测未覆盖到窗口末端**（本地跑历史数据时的正常现象），"
                     "上面的 24h 数字含更早时段，勿直接当作当前状态")
    # ★★ R804：**故事级**漏斗。③ 的尝试口径回答"调用了多少次"，但不回答
    #   "丢了几篇稿"——生产实证（10-19 24h）：尝试 68 篇 / 交付 58 篇，
    #   10 篇未交付（8 篇 openrouter 失控啰嗦：9,868 字符残句 / 耗 14k~16k
    #   token 吐空；2 篇 429 限额），而日报只写"失败率 38~59%"，读者会以为
    #   只是重试、没丢稿。故事口径复用 metrics_report.funnel（R119 单一实现），
    #   不在这里重算 key。
    try:
        _fn = _load_metrics_report().funnel(
            [r for r in rows if (r.get("ts") or "") >= cut])
    except Exception as _e:
        L.append("   故事级：⚠️ 漏斗不可用（%s: %s）——不静默跳过（R704）"
                 % (type(_e).__name__, _e))
    if _fn and _fn.get("attempted"):
        _lost = _fn.get("lost", _fn["attempted"] - _fn["delivered"])
        L.append("   故事级：交付 %d / 尝试 %d（成功率 %.0f%%；failover 救回 %d 篇；未交付 %d 篇）"
                 % (_fn["delivered"], _fn["attempted"],
                    100 * _fn["delivered"] / _fn["attempted"],
                    _fn.get("failover_rescued") or 0, _lost))
        _lc = _fn.get("lost_causes") or {}
        if _lost and _lc:
            L.append("   未交付主因：%s"
                     % " ｜ ".join("%s %d" % (k, v)
                                   for k, v in sorted(_lc.items(), key=lambda x: -x[1])[:3]))
    prov = Counter(r.get("provider") for r in pub if r.get("provider"))
    if prov:
        L.append("   通道：%s" % dict(prov.most_common(4)))
    # ★★ R800：传输层失败按通道点名。账户级故障被永久冷却后这些行会消失，
    #   但 `cred` 行仍在 24h 窗口里 ⇒ 面板不会因为"不再重试"而装作没发生
    #   （R617：熔断跳过与从未尝试在报表上不可区分）。
    tr_fail = [r for r in att if r.get("stage") == "transport"]
    if tr_fail:
        fc = Counter(r.get("provider") or "?" for r in tr_fail)
        L.append("   传输失败：%s"
                 % " ｜ ".join("%s ×%d" % (p, n) for p, n in fc.most_common(4)))
    if cred:
        cp = Counter(r.get("provider") or "?" for r in cred)
        first = min(cred, key=lambda r: r.get("ts") or "")
        L.append("   ⚠️ 账户级故障：%s 余额/额度耗尽（自 %s 起 %d 次）"
                 % ("、".join(cp), _cst(first["ts"]).strftime("%m-%d %H:%M"), len(cred)))
        _after = [r for r in pub if (r.get("ts") or "") >= (first.get("ts") or "")]
        _ap = Counter(r.get("provider") for r in _after if r.get("provider"))
        if _after and len(_ap) == 1:
            _p0, _n0 = next(iter(_ap.items()))
            L.append("   　⇒ 此后 %d 篇 100%% 由 %s 出稿（其余通道零成功）" % (_n0, _p0))

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

    # ⑥ 内容质量雷达（R750：把 R596~R747 建的收敛/质量指标接进日报）
    #   ★ 为什么：这些雷达此前只活在 metrics_report dashboard 里，而用户每天看的
    #     是这份钉钉日报 ⇒ 质量趋势对用户不可见（R600 教训的人工面复发）。
    #   ★ 口径**必须复用** metrics_report 的 quality_scan（单一实现）——日报自己
    #     重算一遍就是 R731「报告与生产口径分叉」的翻版。故这里 import 后直接调用，
    #     并由守卫锁「日报数值 == quality_scan 数值」。
    #   ⚠️ 只列**非零**行：日报要短，全零的雷达不占版面；全零时明说"未检出"。
    try:
        _mr = _load_metrics_report()
        _q = _mr.quality_scan(rows, window=20)
    except Exception as _e:
        _q = None
        L.append("")
        L.append("⑥ 内容质量：⚠️ 质量扫描不可用（%s: %s）"
                 "——不静默跳过，看不到差异会被读成'没差异'（R704）"
                 % (type(_e).__name__, _e))
    if _q:
        _scanned = _q.get("scanned") or 0
        _lines = []
        for _key, _label, _thr in (
                ("manip_frame", "🎭操纵归因", 40),
                ("vol_confirm", "📊放量确认", 40),
                ("stock_advice", "🧰实操套话", 40),
                ("watch_lead", "📋先看watch-lead", 40),
                ("hotlist_absent", "📻热搜缺席", 40),
                ("campaign_cta", "🎁活动CTA", 40)):
            _v = _q.get(_key) or 0
            if _v:
                _pct = 100 * _v / max(1, _scanned)
                _warn = " ⚠️" if _pct >= _thr else ""
                _lines.append("%s %d/%d=%d%%%s" % (_label, _v, _scanned, _pct, _warn))
        # R759：正文活动提及率（红线监护）——R758 提法菜单是条件句，若模型从此
        # 不在正文提活动（只靠注入标签保归因），互动驱动器就忘了（R741：KGST
        # 提及 1%→17% 靠正文提及）。同覆盖纪律：分母=有遥测帖数。
        # ⚠️ 本行**不得被任何分支丢弃**（首版把它 append 进 _lines 后，_six_clean
        #   分支直接渲染"未检出"把它丢了——指标建了不显示等于没建，R619）。
        _cmv = _q.get("campaign_mention") or 0
        _cmcov = _q.get("campaign_mention_cov") or 0
        if _cmcov > 0:
            _cmp = 100 * _cmv / _cmcov
            _lines.append("🎁正文活动提及 %d/%d=%d%%"
                          % (_cmv, _cmcov, _cmp))
            # ★★ R803：按语种拆分。生产实证（10-19 英文窗口）：近 20 篇
            #   构成 en 15 / zh-CN 2 / zh-TW 3，聚合提及率 12/20=60%，
            #   拆开是 zh-CN 2/2=100%、en 9/15=60%、zh-TW 1/3=33%。
            #   只看聚合会把"英文窗口拉低"误读成"中文也崩了"（R731 家族：
            #   混合口径让报告说错话）。≥2 个语种有样本才显示。
            _cml = _q.get("campaign_mention_by_lang") or {}
            _cml_parts = []
            for _lg, _d in sorted(_cml.items(),
                                  key=lambda kv: (-(kv[1].get("cov") or 0), kv[0])):
                _lcv = _d.get("cov") or 0
                if not _lcv:
                    continue
                _lhit = _d.get("hit") or 0
                _cml_parts.append("%s %d/%d=%d%%"
                                  % (_lg, _lhit, _lcv, 100 * _lhit / _lcv))
            # ★★ R816：**没样本的语种必须显式列名**，不能从这一行里静默消失。
            #   生产实证（10-20 13:40Z 日报）：⑥ 只打「zh-CN 12/14=85% /
            #   zh-TW 6/6=100%」，**en 整段不见**——因为日报跑在 13:40Z 前后，
            #   而英文窗口是**北京夜窗**（main.py LANG_DAY_START/END=06–22 ⇒
            #   UTC 14:00–22:00），"近20篇"里常常一篇 en 都没有。
            #   读者会把"没有这一行"读成"en 没问题"（R704：看不到差异会被读成
            #   没差异；R784 同族：覆盖 0 必须明说，绝不并进"未检出"）。
            #   ⚠️ 语种清单取自 ⑤ 的 **24h 实际发布语种**，**不硬编码语种名**
            #   （纪律 5：口径只能有一份实现；报表不复制生产的 LANG_* 常量）。
            _zero_parts = []
            for _lg in sorted(k for k in (langs or {}) if k):
                if _lg in _cml and ((_cml[_lg] or {}).get("cov") or 0):
                    continue
                _zero_parts.append("%s 0/0（本期无样本）" % _lg)
            if len(_cml_parts) >= 2 or _zero_parts:
                _lines.append("🎁提及按语种 %s" % " / ".join(_cml_parts + _zero_parts))
        # R758：活动CTA**结尾整句模板**收敛（用户第七批粘贴实测 93% 的那个模板）。
        # ★ 分母是**有全文遥测的帖数**（覆盖率）不是 scanned——混合窗口里旧帖
        #   无该字段，用 scanned 会稀释占比（R610：同一判据所有消费面一起落地）。
        _tpl_v = _q.get("campaign_cta_tpl") or 0
        _tpl_cov = _q.get("campaign_cta_tpl_cov") or 0
        _tpl_note = None
        if _tpl_cov > 0:
            _tpct = 100 * _tpl_v / _tpl_cov
            _twarn = " ⚠️" if _tpct >= 40 else ""
            _lines.append("🎁活动CTA结尾模板 %d/%d=%d%%%s"
                          % (_tpl_v, _tpl_cov, _tpct, _twarn))
        else:
            # ★ 覆盖 0 时**必须明说无遥测**，绝不并进"未检出"——那会把"没数据"
            #   读成"没问题"（R747：指标的量测口径要先自证）。
            _tpl_note = ("🎁活动CTA结尾模板暂无全文遥测"
                         "（R758上线后新帖才计数，不是0命中）")
        _six_clean = not _lines
        # ★★ R784：雷达扫描口径诚实化。quality_scan 现在优先扫 final_text
        #   （发布全文）——实测 711 篇发布帖 100% content_chars>200 ⇒ 旧
        #   preview 口径对每一篇都只覆盖开头 ~66%。R784 部署后新帖才有全文
        #   遥测 ⇒ 混合窗口里"命中数跳升"很可能只是**口径升级**（旧口径
        #   系统性漏掉段2~3/结尾的复读），不是真的恶化。把覆盖率写进日报，
        #   否则用户看到跳升会以为质量崩了（R736：报告说错话比不说话更糟）。
        _fcov = _q.get("full_cov") or 0
        _cov_note = None
        if _fcov == 0:
            _cov_note = "（雷达按200字preview口径扫描，段2~3/结尾不可见）"
        elif _fcov < _scanned:
            _cov_note = ("（全文遥测覆盖%d/%d篇，混合口径：与更早的日报"
                         "不可直接比较）" % (_fcov, _scanned))
        if _lines:
            if _tpl_note:
                _lines.append(_tpl_note)
            if _cov_note:
                _lines.append(_cov_note)
            L.append("")
            L.append("⑥ 内容质量（近20篇）：%s" % " ｜ ".join(_lines))
        elif _six_clean:
            L.append("")
            L.append("⑥ 内容质量（近20篇）：✅ 未检出收敛复读（另：%s）"
                     % "；".join(x for x in (_tpl_note, _cov_note) if x))
        else:
            L.append("")
            L.append("⑥ 内容质量（近20篇）：✅ 未检出收敛复读")

    # ⑦ 结论
    L.append("")
    # ★★ R800：账户级故障必须改变结论。旧逻辑只要配额打满就 ✅「运行正常…无需
    #   处理」——生产实证 10-19：stepfun 双通道 08:26Z 起余额耗尽、此后 8 篇
    #   100% 由 Preset-google 出稿，日报仍写"无需处理" ⇒ 用户不知道该充值
    #   （R798 同族：报告说了它没验证过的话）。
    #   已恢复出稿（末次成功晚于末次 credit）则不升级结论，避免误报。
    _cred_live = False
    if cred:
        _cp = set(r.get("provider") for r in cred if r.get("provider"))
        _last_cred = max((r.get("ts") or "") for r in cred)
        _cred_live = not any(
            r.get("provider") in _cp and (r.get("ts") or "") > _last_cred for r in pub)
    if fails:
        L.append("结论：⚠️ 有发布失败 %d 次，请查看上方明细" % len(fails))
    elif _cred_live:
        _cp = Counter(r.get("provider") or "?" for r in cred)
        _first = min(cred, key=lambda r: r.get("ts") or "")
        _after = [r for r in pub if (r.get("ts") or "") >= (_first.get("ts") or "")]
        _ap = Counter(r.get("provider") for r in _after if r.get("provider"))
        _tail = ""
        if _after and len(_ap) == 1:
            _p0, _n0 = next(iter(_ap.items()))
            _tail = "；此后 %d 篇 100%% 由 %s 出稿（其余通道零成功）" % (_n0, _p0)
        _lost_n = (_fn or {}).get("lost") or 0
        if _lost_n:
            _tail += "；近 24h 未交付 %d 篇" % _lost_n
        L.append("结论：⚠️ 通道账户余额/额度耗尽（%s，自 %s 起 %d 次）%s"
                 " ⇒ 请充值或更换 Key" % ("、".join(_cp),
                                          _cst(_first["ts"]).strftime("%m-%d %H:%M"),
                                          len(cred), _tail))
        if isinstance(sent24, int) and isinstance(cap, int) and cap > 0 and sent24 >= cap:
            L.append("       配额 %d/%d 已打满（正常行为）" % (sent24, cap))
    elif _fn and (_fn.get("lost") or 0) > 0:
        # ★★ R804：有丢稿就不能说"运行正常"（哪怕配额打满）。
        #   生产实证：10 篇未交付时，旧结论仍是"✅ 运行正常…无需处理"。
        _lc = _fn.get("lost_causes") or {}
        _top = "、".join("%s %d" % (k, v)
                         for k, v in sorted(_lc.items(), key=lambda x: -x[1])[:2])
        L.append("结论：⚠️ 近 24h 有 %d 篇故事未交付%s ⇒ 查看上方③故事级明细"
                 % (_fn["lost"], ("（主因：%s）" % _top) if _top else ""))
    elif isinstance(sent24, int) and isinstance(cap, int) and cap > 0 and sent24 >= cap:
        # ★ 配额打满是**预期结果**（配额就是上限），不是异常。
        #   此前写"⚠️ 窗口内 0 篇发布" ⇒ 在配额打满的日子**每天都误报警**。
        L.append("结论：✅ 运行正常。配额已打满 %d/%d，"
                 "配额门按设计拦截（省掉整轮抓取），无需处理" % (sent24, cap))
    elif not pub:
        # ★★ R796：结论要点名**实测的主导拦截原因**，不再是"可能命中 A/B/C"
        #   让人自己去翻。生产实证（10-18 的 11 个 0 发布轮）：候选 99~104 条
        #   全部死于「无有效代币 63~69 + 单币限流 35~36」——漏斗里写得清清楚楚，
        #   结论却只说"可能命中间隔门/无候选/风控拦截"，读者先去看三个错地方
        #   （R704：看不到差异会被读成没差异；这里是说错方向比不说更糟）。
        _dom = None
        if sums:
            _act = [r for r in sums if (r.get("feeds_ok") or 0) > 0] or sums
            _causes = (
                (sum(r.get("skipped_no_token") or 0 for r in _act), "无有效代币"),
                (sum(r.get("skipped_token_limit") or 0 for r in _act), "单币限流"),
                # ★ R815：同 ②——`unprocessed` 是"本轮未评估"的残差，不是"配额满"。
                #   本分支的前提就是「配额未满」，标成"配额满未处理"直接自相矛盾。
                (sum(r.get("unprocessed") or 0 for r in _act), "候选未评估"),
                (sum(r.get("skipped_risk_blocked") or 0 for r in _act), "风控拦截"),
                (sum(r.get("skipped_batch_dup") or 0 for r in _act), "批内重复"),
            )
            _n, _dom = max(_causes)
            if _n <= 0:
                _dom = None
        L.append("结论：⚠️ 窗口内 0 篇发布，且配额未满%s"
                 " ⇒ 请查看上方②漏斗明细" % (f"（主导拦截：{_dom}）" if _dom else ""))
    else:
        L.append("结论：✅ 运行正常，无发布失败")
    return "\n".join(L)


def send_dingtalk(url: str, text: str) -> str:
    """发送日报到钉钉机器人，返回响应体。

    ★★ R781：**必须校验 errcode**。生产实证：webhook token 失效
    （{"errcode":300005,"errmsg":"token is not exist"}）已连续 6 天发生，
    而旧实现把响应体 print 出来就 return——workflow 全绿、用户一条日报都没
    收到（"探针在跑、答案被丢弃"的运维侧变体：发送失败被当成发送成功）。
    DingTalk 约定 errcode==0 才是成功；非 0 抛异常让调用方走兜底/报警。
    """
    body = json.dumps({"msgtype": "text",
                       "text": {"content": text},
                       "at": {"isAtAll": False}}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=20)
    resp = r.read().decode("utf-8", "replace")
    try:
        payload = json.loads(resp)
    except ValueError:
        return resp          # 非 JSON 响应（代理页等）——原样返回，由调用方判断
    if isinstance(payload, dict) and payload.get("errcode") not in (0, None):
        raise RuntimeError("钉钉返回业务错误: %s" % resp[:200])
    return resp


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
    # ★★ R781：主通道（DINGTALK_WEBHOOK_URL）失败必须**兜底 + 响**。
    # 生产实证：钉钉 token 失效连续 6 天，旧实现 print  errcode 300005 但
    # exit 0 ⇒ workflow 全绿而用户零日报。现：主通道失败 → 试 WEBHOOK_URL
    # （同 payload，generic webhook 通道）→ 仍失败则 exit 1 让 workflow 红。
    try:
        print(send_dingtalk(a.url, text))
        return
    except Exception as e:
        print("主通道（DINGTALK_WEBHOOK_URL）发送失败: %s" % e)
    fallback = (os.environ.get("WEBHOOK_URL", "") or "").strip()
    # ★ R798：兜底是否**真的试过**要如实区分。旧收尾句无条件说
    #   "主通道与兜底通道均失败"——在 daily_report.yml 未透传 WEBHOOK_URL
    #   的生产里，兜底压根没跑，这句是**它没测过的结论**（报了个它没收集的
    #   数据；与项目里"探针在跑、答案被丢弃"同族，只是这次是"答案没采"）。
    tried_fallback = bool(fallback) and fallback != a.url
    if tried_fallback:
        try:
            print("兜底通道（WEBHOOK_URL）重试...")
            print(send_dingtalk(fallback, text))
            return
        except Exception as e2:
            print("兜底通道也失败: %s" % e2)
    if tried_fallback:
        sys.exit("日报发送失败（主通道与兜底通道均失败）——请检查 "
                 "DINGTALK_WEBHOOK_URL / WEBHOOK_URL 配置")
    if not fallback:
        sys.exit("日报发送失败（主通道失败；环境里没有 WEBHOOK_URL，"
                 "兜底通道根本未尝试）——请检查 DINGTALK_WEBHOOK_URL 配置，"
                 "或在仓库 secret 补一个独立的 WEBHOOK_URL 以获得兜底能力")
    sys.exit("日报发送失败（主通道失败；WEBHOOK_URL 与主通道是同一个地址，"
             "无法独立兜底）——请检查 DINGTALK_WEBHOOK_URL 配置")


if __name__ == "__main__":
    main()
