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
        sc = Counter(r.get("stage") or "?" for r in att)
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
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "metrics_report_r750",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "scripts", "metrics_report.py"))
        _mr = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mr)
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
            if len(_cml_parts) >= 2:
                _lines.append("🎁提及按语种 %s" % " / ".join(_cml_parts))
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
        L.append("结论：⚠️ 通道账户余额/额度耗尽（%s，自 %s 起 %d 次）%s"
                 " ⇒ 请充值或更换 Key" % ("、".join(_cp),
                                          _cst(_first["ts"]).strftime("%m-%d %H:%M"),
                                          len(cred), _tail))
        if isinstance(sent24, int) and isinstance(cap, int) and cap > 0 and sent24 >= cap:
            L.append("       配额 %d/%d 已打满（正常行为）" % (sent24, cap))
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
                (sum(r.get("unprocessed") or 0 for r in _act), "配额满未处理"),
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
