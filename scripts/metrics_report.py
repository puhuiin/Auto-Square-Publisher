#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
遥测分析器：把 metrics.jsonl 聚合成可读的数据驱动调优简报。

设计约束（血泪版）：
- schema 是开放演进的（先后出现过 platforms/stage/reason/model/tokens_used/
  llm_latency_sec/error 等字段），本脚本只认"有则用、无则跳"，未知 outcome
  按规则归类，绝不因缺字段崩溃；
- 坏行只计数不中断（JSONL 追加写 + git 合并都可能留下半行）；
- 纯标准库、无任何副作用（只读），方便任何时间点重跑。

用法：
  python scripts/metrics_report.py [metrics.jsonl] [--json]
"""
import collections
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "metrics.jsonl")
TOP_N = 8

# R105：内容合规巡检模式（与 main.py 同步——脚本独立运行不 import 主模块）。
# prompt 级禁令是软约束，模型可能不遵守——合规度此前零度量，全靠人工读帖。
# 注意：final_preview 截断长度经历过 120→200（R106），巡检覆盖的是回执实际
# 存储的钩子区文本（算法首屏所在），非全文。
# R10：第三分支此前是 `(?:贪婪|恐惧|情绪)[^。！？\n]{0,8}\d{2}`——间隙允许逗号与拉丁字母，
# 于是"市场情绪偏谨慎，BTC 24 小时涨了 3%"这类**正常行情句**也会命中（实测确认），
# 后果是：误武装情绪锚点禁令 → 盘面情绪行被剥离、prompt 注入"严禁提及"，
# 报表还把合规内容记成违规。收紧为"间隙只允许中文/空格，且数字紧跟"，实测真阳性全保留。
_FNG_ANCHOR_RE = re.compile(
    r"(贪婪|恐惧|情绪)指数|贪婪区|恐惧区|(?:贪婪|恐惧|情绪)[^\s。！？，、；：\nA-Za-z0-9]{0,4}\s?\d{2}")
_OVERUSED_DEVICES = ("先泼盆冷水",)  # main._OVERUSED_OPENING_DEVICES
# R286：长文标题禁用领词表——与 main._GENERIC_LEADINS 同步（防漂移测试见
# TestQualityPatternSync）。标题是信息流里决定点不点开的第一触点：生产实录
# 11 篇长文标题里 "刚出炉：Fed升息落地…"命中 R282 刚晋升进静态表的"刚出"族。
# R296（2026-09-21）已把该表写进长文 TITLE 指令做预防；R329 修本注释与告警
# 文案——旧文案"守卫只覆盖正文开场"在 R296 后为假，会把历史残留读成开放缺口。
_TITLE_LEADINS = ("刚刚", "突发", "重磅", "快讯", "注意", "刚出", "几分", "最新", "刚爆")
# R343：weekday_bj（0=周一）自 append_metrics base 起写在每行，与 hour_bj 同源，
# 渲染按自然周序（非频次序）读，缺勤日一眼可见
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# R296 落地日（UTC）：命中日全早于该日 = 历史残留；含当日及之后 = 预防侧需复查
_TITLE_BAN_DEPLOYED = "2026-09-21"
_AI_FLAVOR_HARD = (  # main.MultiLLMEngine._AI_FLAVOR_HARD
    "拭目以待", "未来可期", "保驾护航", "谱写", "新篇章", "扬帆起航",
    "值得注意的是", "值得一提的是", "综上所述", "总而言之", "让我们一起",
    "毋庸置疑", "不言而喻", "共同见证",
    # R580 新增：正式书面转折/总结词（口语交易员不会敲），命中即废
    "由此可见", "众所周知", "简而言之", "简言之", "换言之",
    "一言以蔽之", "备受瞩目",
)
QUALITY_SCAN_WINDOW = 20  # 最近 N 篇发布帖做合规扫描
# R603：操纵归因（万能阴谋论）扫描词——R596 用轮换 hedge 词+可观察驱动指导压
# 「我猜这波是主力出货」的腔调，但实发验证（R596 后首 3 帖）显示：逐字「我猜」确实
# 消失、hedge 已多样化，可**语义上的「利好不涨=有人出货/烟雾弹/送流动性」操纵叙事
# 仍在 2/3 帖出现**——它靠 R596 多样化后的词汇绕过了前缀指纹雷达（我猜/现在那套
# 按前缀聚簇的探测看不见语义框架）。这里按操纵归因标记词计数，让这个「叙事指纹」
# 像 FNG/AI 腔一样可跨帖追踪：若 R596 真起效，占比会随新帖下行；若长期高位，才是
# 加固 R596 的信号（避免在 n=3 上过拟合重复打补丁）。信息性，非门禁。
_MANIPULATION_FRAME = ("出货", "洗盘", "烟雾弹", "送流动性", "派筹", "诱多", "压盘")

# R610：FORCE_STRIP 清单的第二份事实源（与 main.SquarePublisher.
# FORCE_STRIP_CASHTAGS 同步，防漂移测试见 TestQualityPatternSync）。
# 用途：把「稳定币-only 的零挂件帖」从R123 生命线告警里摘出来——那是 R316
# 「稳定币不做挂件」契约下的合规行为，不是保底机制被绕过。
_FORCE_STRIP_CASHTAGS = frozenset({
    "ETF", "SEC", "FED", "CEO", "NFT", "AI", "USD", "USDT", "USDC",
    "CEX", "DEX", "API", "CAGR", "APR", "APY", "ATH", "BAPI", "NEWS", "MEME",
})


def _is_stablecoin_only(tokens):
    """该帖识别到的标的是否**全部**属于 FORCE_STRIP（稳定币/非交易词）。

    True = 零挂件是 R316 契约的必然结果（合规），不该算 Write2Earn 生命线失守。
    口径从严：tokens 缺失/为空/含任一非 FORCE_STRIP 词 → 一律判False（进告警
    分母）。宁可多报不可漏报——这条告警的唯一价值就是「真失守时能响」，
    漏判会让它重新变成哑炮。混合新闻（USDC + 真实山寨币）返回 False，正确报警。
    """
    if not isinstance(tokens, (list, tuple)) or not tokens:
        return False
    return all(isinstance(t, str) and t.strip().upper() in _FORCE_STRIP_CASHTAGS
               for t in tokens)

# R607：「利好不涨」描述复读——读近期全文发现，加密新闻最常见的场景（消息出来、
# 24h 价格没怎么动）被模型收敛到一小撮固定描述句：「连个像样的反弹都没有」「盘面
# 不买账」「连个水花都没溅起来」。全史 8% 但近 30 篇升到 33%（倒数60~30=20%→近30=33%）。
# 它藏在句中、不是段首，R600 的前缀指纹雷达抓不到（续 R603「语义/短语框架要单建指标」
# 的教训）。只追踪不急改 prompt：33% 的上升可能是「近期新闻恰好多为利好不涨、描述本就
# 该多」的话题假象，而非文风退化——贸然在已很密的 prompt 里禁这些生动短语会误伤恰当
# 描述。指标跨更多帖确认是「风格收敛」而非「话题驱动」后，再决定是否在 prompt 里
# 给「换着说法描述『消息出来价格没动』」的技法指导。信息性，非门禁。
_FLAT_DESC_RE = re.compile(
    r"连个像样的.{0,4}(?:反弹|脉冲|涨幅|阳线).{0,3}都没"
    r"|连个水花.{0,4}(?:都没|没溅)"
    r"|盘面.{0,3}(?:不买账|没动|不跟涨|不领情|没反应)"
    r"|淡得(?:抠脚|离谱)")

# R605：句长 burstiness（节奏方差）——整合自全网最新研究（textpulse 2026 对 6 万+
# 文本的实证）：AI 文本最稳的「机器味」信号之一是**句长过于均匀**（标准差小），人类
# 写作句长起伏大。该研究量化：人类句长变异系数 CV≈0.449、AI≈0.376，79% 的 AI 改写
# 比人类原文更「平」。我们 prompt 早有「长短句交错/别每句一个节奏」的指令，但从无
# 度量——此指标把它变成可观测：按帖算句长 CV，跨帖看中位 + 偏平尾。研究同时警告
# burstiness 是「群体信号、个体判决不可靠」（阈值抓 62% AI 也误伤 39% 人类），故**只
# 追踪不设门禁**（承 R603 纪律），偏平阈值 0.35（低于 AI 均值）仅作尾部计数。
def _sentence_cv(text):
    """正文句长变异系数 CV=σ/μ（句=以。！？及换行切分，长度按去空白字符数）。
    句数 <2 返 None（短帖不足以判节奏）。标签行先剥除。"""
    import re as _re
    import statistics as _st
    t = _re.sub(r"#\S+", "", text or "")
    segs = [_re.sub(r"\s", "", s) for s in _re.split(r"[。！？!?\n]+", t)]
    lens = [len(s) for s in segs if s]
    if len(lens) < 2:
        return None
    mean = _st.mean(lens)
    if mean <= 0:
        return None
    return _st.pstdev(lens) / mean


def quality_scan(rows, window=QUALITY_SCAN_WINDOW):
    """对最近 N 篇发布帖的 final_preview 做禁令合规扫描（信息性，非门禁）。
    返回 {"scanned", "fng_anchor", "banned_device", "ai_flavor", "offenders": {...},
          "fng_ban_armed", "fng_violation", "fng_avoided"}。
    R101 前的历史帖命中 FNG 属预期（防线尚未上线），解读时对照时间线。
    R162：fng_ban_active 直录后，"禁令武装 → 新帖避开"的咬合从推断变事实
    （violation>0 即模型无视禁令，是执法升级的实证依据）。"""
    previews = []
    for r in rows:
        if not isinstance(r, dict) or r.get("dry_run") is True:
            continue
        if not _is_delivery_outcome(r.get("outcome")):
            continue
        pv = (r.get("final_preview") or "").strip()
        if pv:
            previews.append((pv, r.get("fng_ban_active"), r.get("ts")))
    previews = previews[-window:]
    out = {"scanned": len(previews), "fng_anchor": 0, "banned_device": 0,
           "ai_flavor": 0, "offenders": collections.Counter(),
           "fng_ban_armed": 0, "fng_violation": 0, "fng_avoided": 0,
           "manip_frame": 0, "burstiness_cvs": [], "flat_desc": 0,
           # R610：追踪指标的「修复生效」判据。固定窗口有个致命的观测时滞：
           # 一次 prompt 加固（R604）刚上线时，窗口里 15/20 篇仍是加固前的旧稿，
           # 于是面板继续报 50% 命中——看起来像修复无效，实则新帖 0 命中
           # （生产实录：R604 commit 后 5 篇操纵词全 0，仅部署延迟那篇例外）。
           # 这种假阴性会诱导下一轮「再去加固一遍已经修好的东西」，正是本项目
           # 最忌的无数据支撑边际改动。故把同一窗口切成近/远两半分别计数：
           # 近半是修复后的新帖（真实当期水位），远半是修复前（对照基线）。
           # 近半显著低于远半 = 修复生效，两者都高 = 修复无效，判据无歧义。
           "manip_frame_recent": 0, "manip_frame_older": 0,
           "flat_desc_recent": 0, "flat_desc_older": 0,
           # R610：近半的时间跨度（最早/最晚 ts）——判读近半的前提。发布速率约
           # 4~6 篇/天，一次加固上线 1 天后近半仍可能含 4 篇加固前旧稿，此时
           # 近半 40% 并不代表"修复无效"。把这个跨度显性化，读者才能自己判断
           # 近半是否已完全落在修复之后，而不是被一个无信息的百分比误导。
           "recent_span": (None, None)}
    # 近/远切分点：窗口后一半为「近」（时间上更近=修复后），前一半为「远」（对照）。
    # 奇数窗时近半多 1 篇；窗口 <2 时不切分（两半都留 0，由渲染层不显示对照）。
    _split = (len(previews) + 1) // 2
    for _idx, (pv, ban_active, _ts) in enumerate(previews):
        _is_recent = _idx >= _split
        if _is_recent and _ts:
            # _lo 取首次出现、_hi 每次覆盖为最新（previews 已按时间升序）。
            # 两者都写成「None 才赋值」会让 _hi 永远停在第一篇，跨度退化成单点。
            _lo, _hi = out["recent_span"]
            _s = str(_ts)[:16].replace("T", " ")
            out["recent_span"] = (_s if _lo is None else _lo, _s)
        m_fng = _FNG_ANCHOR_RE.search(pv)
        if m_fng:
            out["fng_anchor"] += 1
            # R122：命中明细必须进 offenders——此前 FNG 只计数不落明细，
            # 报表 breakdown 里"装置/AI腔"可见而 FNG 隐身（生产实录：巡检报
            # 16 处命中但明细只有冷水×3，6 个 FNG 锚点命中无处可查）
            out["offenders"][f"FNG锚:{m_fng.group(0)[:12]}"] += 1
        # R162：禁令咬合度量——只统计显式记录了状态的帖子（None=历史帖无字段，不进分母）
        if ban_active is True:
            out["fng_ban_armed"] += 1
            if m_fng:
                out["fng_violation"] += 1
            else:
                out["fng_avoided"] += 1
        for dev in _OVERUSED_DEVICES:
            if dev in pv:
                out["banned_device"] += 1
                out["offenders"][f"装置:{dev}"] += 1
        for w in _AI_FLAVOR_HARD:
            if w in pv:
                out["ai_flavor"] += 1
                out["offenders"][f"AI腔:{w}"] += 1
        # R603：操纵归因叙事——每帖最多计一次（按帖占比，不按词频）。不进 offenders
        # （那是「N 处命中」的硬合规口径），只走独立的 🎭 趋势行，避免两个口径互相污染。
        if any(w in pv for w in _MANIPULATION_FRAME):
            out["manip_frame"] += 1
            out["manip_frame_recent" if _is_recent else "manip_frame_older"] += 1
        # R605：句长 burstiness（节奏方差）——句数≥2 才计入，短帖跳过
        _cv = _sentence_cv(pv)
        if _cv is not None:
            out["burstiness_cvs"].append(_cv)
        # R607：「利好不涨」描述复读——句中短语，前缀雷达抓不到，单独按帖计一次
        if _FLAT_DESC_RE.search(pv):
            out["flat_desc"] += 1
            out["flat_desc_recent" if _is_recent else "flat_desc_older"] += 1
    out["offenders"] = dict(out["offenders"])
    return out


# R124：开场指纹雷达——把 R104（"先泼盆冷水"三犯）与 R121（"刚刚"10 帖 3 次）
# 的人工发现过程产品化：自动扫描近期开场句的共享前缀，新指纹成形前预警。
# 只产预警不做禁令（自动禁令有误杀风险，禁令仍走 main.py 的窗口/永久机制）。
_FINGERPRINT_WINDOW = 10
_FINGERPRINT_MIN_HITS = 3
_ARTICLE_HEADER_RE = re.compile(r"^[一二三四五六七八九十]、")

# R302：永久失败拒因标记 → 人类可读原因。标记文本是 main.py failover 循环里
# _breaker_record_permanent 两个调用点写死的 fail_reason 前缀（[credit 24h] =
# 账户余额/额度耗尽、[permanent 24h] = 模型下架/404），test_core 的 R300/R301
# 用例以字面量锁了 main 侧；这里是报表侧的独立副本，故意只认这两类**真·24h 永久
# 冷却**——[router 404] 走指数退避（会自愈、非永久）、[rate-limit] 是节奏问题，
# 都不算永久失败，不进本面。main 若改标记文本，其自身 R300/R301 用例先红。
_PERMANENT_FAIL_TAGS = (
    ("[credit 24h]", "余额/额度耗尽"),
    ("[permanent 24h]", "模型下架/404"),
)


def _extract_opener(preview):
    """R293：从回执预览取开场句——长文分节头（"一、发生了什么"）不是开场句，
    跳过取首个正文段（与 main.py _recent_openers 的 R121 修复同语义）。
    opener_fingerprint 与首段钩子普查（R293）共用，保口径不漂移。"""
    pv = (preview or "").strip()
    if not pv:
        return ""
    for seg in (s.strip() for s in re.split(r"[。\n]", pv)):
        if not seg:
            continue
        if _ARTICLE_HEADER_RE.match(seg):
            continue
        return seg
    return ""


def _extract_body_opener(preview):
    """R600：取开场句之后第二个正文段的首句——「我猜这波是主力…」这类分析段
    开场是开场雷达（_extract_opener 只看第一段）的盲区：R596 实录「我猜」从 0%
    猛升到最近 30 篇 47%、几乎全在第 2 段开头，dashboard 全程没报、靠人工读
    final_preview 才发现。把分析段开场也纳入指纹扫描，补上这块盲区。
    长文分节头（"一、发生了什么"）不是正文段，跳过。"""
    pv = (preview or "").strip()
    if not pv:
        return ""
    segs = []
    for seg in (s.strip() for s in re.split(r"[。\n]", pv)):
        if not seg or _ARTICLE_HEADER_RE.match(seg):
            continue
        segs.append(seg)
        if len(segs) >= 2:
            break
    return segs[1] if len(segs) >= 2 else ""


def _cluster_openers(openers, min_hits):
    """共享前缀聚簇（opener_fingerprint 与 body_fingerprint 共用，保口径不漂移）。
    4 字簇优先，2 字簇仅在其不是任何 4 字簇前缀时才报（去重：同簇只报最长）。
    R131：2 字簇要求词边界——"Bitwise/BitGo"共享的"Bi"只是词的前半，不是指纹；
    "刚刚,$SHIB"/"刚刚 Solana"的"刚刚"后接标点/空格才是完整领词。边界=第 3 字符
    非 ASCII 字母数字（CJK 跟随算边界："刚刚看涨"就是"刚刚"领句）。
    R601：$挂件/币代码前缀不是文风指纹——「$XRP 现在报…」「$XRP Ledger 销毁…」
    「$XRP现价…」三条开场各不相同，只是热门币 $XRP 连续领头（内容集中度，不是
    套路复读）；而账号本就「追踪热门币种」会让 BTC/XRP/SHIB 反复打头，不排除就会
    让币名噪音淹没真·文风指纹（我猜/多数人/全网）。前缀去掉可选 $ 后若纯 ASCII 字母
    （币代码/实体名 XRP/BTC/Bitwise）即视为实体名、不报，与 R131 实体名不算指纹同理。"""
    def _lead_word_boundary(opener: str) -> bool:
        nxt = opener[2:3]
        return nxt == "" or not (nxt.isascii() and nxt.isalnum())

    def _is_entity_prefix(prefix: str) -> bool:
        core = prefix[1:] if prefix.startswith("$") else prefix
        return len(core) >= 2 and core.isascii() and core.isalpha()

    from collections import Counter
    alerts = {}
    clusters4 = {p: c for p, c in
                 Counter(o[:4] for o in openers if len(o) >= 4).items()
                 if c >= min_hits and not _is_entity_prefix(p)}
    alerts.update(clusters4)
    for p, c in Counter(o[:2] for o in openers
                        if len(o) >= 2 and _lead_word_boundary(o)).items():
        if c >= min_hits and not _is_entity_prefix(p) and not any(p4.startswith(p) for p4 in clusters4):
            alerts[p] = c
    return dict(sorted(alerts.items(), key=lambda kv: -kv[1]))


def _collect_segments(rows, extractor, window):
    """按投递口径从近及远收集 extractor 产出的非空段（dry_run/非投递跳过）。"""
    out = []
    for r in reversed(rows if isinstance(rows, list) else []):
        if not isinstance(r, dict) or r.get("dry_run") is True:
            continue
        if not _is_delivery_outcome(r.get("outcome")):
            continue
        seg = extractor(r.get("final_preview"))
        if seg:
            out.append(seg)
        if len(out) >= window:
            break
    return out


def opener_fingerprint(rows, window=_FINGERPRINT_WINDOW, min_hits=_FINGERPRINT_MIN_HITS):
    """抽最近 N 帖开场句的首 2/4 字前缀，同一前缀 ≥min_hits 次即报预警。
    长文分节头（"一、发生了什么"）不是开场句，跳过取正文段（与 main.py
    _recent_openers 的 R121 修复同语义）。返回 {"scanned", "alerts": {前缀: 次数}}。"""
    openers = _collect_segments(rows, _extract_opener, window)
    return {"scanned": len(openers), "alerts": _cluster_openers(openers, min_hits)}


def body_fingerprint(rows, window=_FINGERPRINT_WINDOW, min_hits=_FINGERPRINT_MIN_HITS):
    """R600：第二正文段（分析/观点段）开场的共享前缀预警——与 opener_fingerprint
    同口径同阈值，只是扫 _extract_body_opener。补开场雷达只看第一段的盲区
    （R596「我猜这波是主力」47% 复读就藏在这里）。"""
    bodies = _collect_segments(rows, _extract_body_opener, window)
    return {"scanned": len(bodies), "alerts": _cluster_openers(bodies, min_hits)}



def _num(v):
    """宽容数字：int/float/数字字符串 -> float，否则 None。
    NaN/inf 一律拒收（JSON 的 NaN 非标准但能解析，放进来会毒化整组平均数）。"""
    if isinstance(v, bool):
        return None
    f = None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except (ValueError, AttributeError):
            return None
    if f is None or not math.isfinite(f):
        return None
    return f


def load_content_stats(path=None):
    """R285：读 content_stats.jsonl（import_content_stats.py 的产物）→
    {content_id: {"views":int,"likes":int,"comments":int}}。文件缺失/损坏返回
    空 dict——没有互动数据时报表整块不渲染（零噪音，同"停放的源"惯例）。"""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "content_stats.jsonl")
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                cid = r.get("content_id")
                if not cid:
                    continue
                rec = {}
                for k in ("views", "likes", "comments"):
                    v = r.get(k)
                    if isinstance(v, int) and v >= 0:
                        rec[k] = v
                if rec:
                    out[str(cid)] = rec
    except OSError:
        return {}
    return out


_STATS_CACHE = {"loaded": False, "data": {}}


def _stats_lookup(cid):
    """R285：content_stats 进程内懒加载缓存（一次读盘，后续 join 零 IO）。"""
    if not _STATS_CACHE["loaded"]:
        _STATS_CACHE["data"] = load_content_stats()
        _STATS_CACHE["loaded"] = True
    if not cid:
        return None
    return _STATS_CACHE["data"].get(str(cid))


def load_token_engagement(path=None):
    """R612：读 token_engagement.json（R608 浏览加权表）→ {"min_n":int, "tokens":
    {TOK: {"n":int,"median_views":int}}}。缺失/损坏返回空 dict——报表整块不渲染。

    这张表此前**零消费面**：main.py 用它加权，但没有任何报表告诉运营者
    「哪些币在表内（会被 ±5）、哪些被 min_n挡在表外」。于是R608 最关键的一个
    后果完全不可见——见R612 反馈回路分析。
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "token_engagement.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    toks = data.get("tokens")
    if not isinstance(toks, dict):
        return {}
    out = {}
    for t, rec in toks.items():
        if not isinstance(rec, dict):
            continue
        n = rec.get("n")
        mv = rec.get("median_views")
        if isinstance(n, int) and isinstance(mv, (int, float)) and mv >= 0:
            out[str(t).upper().replace("$", "")] = {"n": n, "median_views": int(mv)}
    mn = data.get("min_n")
    return {"min_n": mn if isinstance(mn, int) else 4, "tokens": out}


def _bucket_line(buckets, top=None):
    """R285：{桶名: [浏览样本]} → "名 均值×n" 串（按均值降序，样本 <3 标注小样本）。"""
    if not buckets:
        return ""
    items = sorted(buckets.items(), key=lambda kv: -(sum(kv[1]) / len(kv[1])))
    if top:
        items = items[:top]
    parts = []
    for name, vals in items:
        avg = sum(vals) / len(vals)
        mark = "（小样本）" if len(vals) < 3 else ""
        parts.append(f"{name} {avg:.0f}×{len(vals)}{mark}")
    return " · ".join(parts)


def _hour_bucket(h):
    """北京小时 → 时段桶（内容受众以中文用户为主，按本地作息分四档）。"""
    if not isinstance(h, int) or not (0 <= h <= 23):
        return None
    if h < 6:
        return "凌晨0-6"
    if h < 12:
        return "上午6-12"
    if h < 18:
        return "下午12-18"
    return "晚间18-24"


def load_rows(path):
    """返回 (rows, bad_lines)：坏行跳过计数。
    utf-8-sig：Windows 下编辑器手碰过的文件常带 BOM，不吃掉它首行必被判坏。"""
    rows, bad = [], 0
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(obj, dict):
                rows.append(obj)
            else:
                bad += 1
    return rows, bad


def _is_delivered(row):
    plats = row.get("platforms")
    return isinstance(plats, list) and len(plats) > 0


def _is_delivery_outcome(outcome) -> bool:
    """与 cost_analysis.is_delivery_outcome / main._is_delivery_outcome 同语义（R211/R572）：
    binance_published*、video_published 与副平台 *_delivered* 都算投递成功；
    already_delivered 是幂等跳过标记，排除。"""
    o = str(outcome or "")
    if o.startswith("binance_published") or o == "video_published":
        return True
    if o == "already_delivered":
        return False
    return o.endswith("_delivered") or o.endswith("_delivered_cache_failed")


# "真正进入 LLM 尝试"的 outcome 集合。R6 补入 publish_failed：该行是
# "LLM 生成成功、但投递失败"的留痕，platforms 为空时既不算投递也不算拒稿，
# 旧口径会把整条故事从分母里漏掉 → 发布全挂也显示 100% 成功率。
ATTEMPT_OUTCOMES = ("llm_rejected", "llm_failed", "llm_success", "publish_failed")


def summarize(rows):
    """聚合成嵌套计数，调用方只读不写"""
    s = {
        "total": len(rows),
        "by_outcome": collections.Counter(),
        "by_hour": collections.Counter(),
        # R343：投递篇分周（0=周一）——weekday_bj 与 by_hour 同源同粒度，此前
        # 只有 hour_bj 有出口（分时 + 时段均浏览），周维静默；周末/工作日节奏与
        # 缺勤日不可查。写侧 base 每行都写（1542/1542），零消费=写侧无出口。
        "by_weekday": collections.Counter(),
        "by_source": collections.Counter(),
        "by_provider": collections.Counter(),
        "by_token": collections.Counter(),
        "images": 0,
        "image_tiers": collections.Counter(),
        "zero_widget_posts": 0,
        # R610：稳定币-only 零挂件（合规，不告警）——与上面失守桶分开计数，
        # 报表显性列出，避免"告警消失"被误读成观测被关掉。
        "zero_widget_stablecoin_only": 0,
        "zero_tag_posts": 0,
        # R284：活动标签返佣归因覆盖（保底双标签之外的创作激励活动标签）
        "campaign_tag_evaluated": 0,
        "campaign_tag_covered": 0,
        "campaign_tag_zero_fresh": 0,
        # R291：显式活动标签直方图（注入原文，回答"实际在参加哪个活动"）
        "campaign_tags": collections.Counter(),
        # R612：浏览加权表覆盖审计——表内/表外币的首标的发布量与浏览基线。
        # 动机见 render_text：该表按 min_n 过滤，而样本量 n 本身由"我们发了多少篇"
        # 决定，于是高浏览币（发得少→n 小）被挡在表外、低浏览币（发得多→n 大）
        # 留在表内被罚——一个自我强化的回路，此前零可见性。
        "eng_boost": None,
        # R285：浏览/互动 join（content_id × content_stats.jsonl）与三维归因样本
        "stats_posts": 0,
        # R628：分母= 带 content_id 的投递行（与 stats_posts 分子同源，见累加处）
        "delivered_joinable_posts": 0,
        "stats_views_total": 0,
        # R286：长文标题眼钩分析——article_title 自 R125 起逐帖落盘（注释原话
        # "事后做标题质量/眼钩分析"），129 条回执零消费面。标题是信息流第一触点。
        "article_titles": [],
        "title_hooks": collections.Counter(),
        "title_leadin_hits": [],
        "stats_views": [],
        "stats_likes": [],
        "stats_comments": [],
        "stats_by_hourbucket": {},   # 时段桶 -> [浏览样本]
        "stats_by_genre": {},        # 长文/短讯 -> [浏览样本]
        "stats_by_source": {},       # 来源 -> [浏览样本]
        # R602：文风维度 × 浏览归因——R521/R288/R130/R592 分别轮换开场钩子/人设/
        # 结尾套路/实操角度来破单调，但「哪种钩子/人设/结尾/角度真能带来浏览」此前
        # 无出口：浏览只按时段/体裁/来源分桶，恰好漏掉我在优化的那几个旋钮。等互动
        # CSV 一到，这四个桶就把 R592-R601 的文风投入变成可度量的 engagement 结论。
        "stats_by_hook": {},         # 开场钩子 -> [浏览样本]
        "stats_by_persona": {},      # 人设 -> [浏览样本]
        "stats_by_ending": {},       # 结尾套路 -> [浏览样本]
        "stats_by_cta": {},          # 实操角度 -> [浏览样本]
        "by_ending": collections.Counter(),
        "by_trade_cta_style": collections.Counter(),
        # R288：人设分布与近期集中度（R287 修的是生成端，报表端监测其效果）
        "by_persona": collections.Counter(),
        "persona_seq": [],
        # R288：热度分分布——TOKEN_LIMIT_BYPASS_IMPACT(30)/ARTICLE_MIN_IMPACT(20)
        # 等门槛的校准基线，此前只能即席探针
        "impact_scores": [],
        # R292：篇幅遥测（短讯/长文分桶）——prompt"140~200 字"条款的度量面（R294 对齐后）
        "chars_by_genre": {},
        "cjk_by_genre": {},
        # R293：首段钩子普查——prompt 最强调的条款"【首两行定生死】第一段必须放
        # 钩子（反差结论/具体数字/悬念）"此前零门零度量；与 R286 标题眼钩同口径
        "opener_hooks": collections.Counter(),
        "opener_evaluated": 0,
        # R222：发布内容新鲜度样本（age_hours 发布行全量携带，此前只能手工统计）
        "pub_ages": [],
        # R173：过期情报注入计数——R171 写侧已直录，报表端同轮补齐（R92 纪律）
        "intel_degraded_posts": 0,
        "intel_fresh_posts": 0,
        # R289：FNG 三件套的最后两个字段（R162 起落盘、R284 对账清单最后一项）
        # hook_count=近窗锚点引入次数（滞回驱动量）；market_stripped=R101 互补
        # 剥离是否真生效。armed_not_stripped 是跨字段一致性不变量。
        "fng_hook_hist": collections.Counter(),
        "fng_evaluated": 0,
        "fng_armed_not_stripped": 0,
        # R181：情报陈旧小时样本（有 last_updated 的帖才进）
        "intel_age_hours": [],
        # R199：最近一次情报成功刷新时刻（campaign_intel llm_success）
        "last_intel_refresh_ts": None,
        # R333：stale_date_refs 写侧（main analyze_with_ai）自 R127 起逐次落盘
        # 「guidance 残留过期日期」计数，但报表零消费——有数据无出口（R276 同族）。
        # 三元组：命中刷新次数 / 引用总数 / 单次最大。
        "stale_date_refs_refreshes": 0,
        "stale_date_refs_hits": 0,
        "stale_date_refs_max": 0,
        # R175：情报刷新被失败退避跳过的轮次（区分「配额早退没刷」vs「想刷被退避挡」）
        "intel_cooldown_skips": 0,
        "reject_by_stage": collections.Counter(),
        "reject_by_provider": collections.Counter(),
        # R620：provider 质量产出 {短名: {ok, rej, fail}}——通过率的分子分母来源。
        # 动机：通道位次行只给"发N/拒M"，读者需心算；而生产实测通道间通过率
        # 差7 倍（openrouter 8% vs stepfun-flash 55%），**按调用量排序会得出
        # 完全相反的结论**。这是"排名指标选错"的典型：量大的通道未必贡献多。
        "provider_quality": {},
        # R623：provider **尝试计数** {短名: 次数}——「池内哪几条从未被尝试过」
        # 的分母来源。
        #
        # 为什么 provider_quality 不够：它只记 ok/rej/fail 三类，**全被拒或全被
        # 熔断跳过的通道在遥测里一行都不落**，于是"从未尝试"与"尝试了但全拒"在
        # 面板上同为空。生产实测这个差别是决定性的——近 30 天池内 12 站有
        # **8 站（67%）零遥测**，而唯一被验证过的一簇是 stepfun / stepfun-flash，
        # **两者同base_url 同 api_key**（R337 注：这是同一家网关的两个模型，
        # 不是两个容灾池）。该网关整体不可用时，链上剩 10 条**从未被验证过**的
        # 通道，正确性从未被任何生产数据检验。
        #
        # 判据（R618 纪律的延伸）：**全史 0 次不是安全信号，是危险信号**——
        # 它意味着这条通道的正确性从未被生产数据验证。
        "provider_attempts": {},
        "reject_reasons": collections.Counter(),
        # R302：永久失败（24h 冷却）按 提供商×原因 单列——push 侧 R301 报警只在跃迁沿
        # 响一次，报表是 pull 侧的常驻视图，但此前把 [credit 24h]/[permanent 24h] 标记
        # 埋在 60 字截断的高频原因 top5 里，"哪个通道当前永久死、为什么"看不清（生产
        # b.ai 余额耗尽 8 条只在 top5 占一行、极易漏读）。键 (provider, 原因标签)。
        "permanent_failures": collections.Counter(),
        # R304：每 (provider,原因) 的最近命中时刻——全史报表里 09-08 已退役的 minimax
        # 永久失败与今天 b.ai 余额耗尽同框而无时间线索，运营分不清"当前该处理"vs
        # "两周前的历史簇"（minimax R261 早已撤），加末次日期让 💀 行真正可行动。
        "permanent_failures_last": {},
        # R616：每 provider 的**末次成功投递**时刻。R304 补了"末次永久失败"日期，
        # 但单看日期仍无法行动——必须与"该provider 此后再没成功过"对比才能分清
        # 两种性质完全相反的状态：
        #   失败早于最后成功 → 已自愈（当时那个模型被换掉了，如09-08 的 minimax
        #     随默认名改为 openrouter/free 退役，而该通道 09-29 还在成功出稿）
        #     → 标 ℹ️ 退役簇，**不该催人工处置**（催了会让人去改一个早已修好的配置）
        #   失败晚于最后成功 → 仍死（如 b.ai 余额 09-29 耗尽，此后没再出一篇）
        #     → 标 ⚠️ 真需处置
        # 生产实锤：两项都是"💀 永久失败"，但一项已自愈、一项仍需充值，
        # 修复前同貌（都只印一个"需人工处置"）。
        "provider_last_success": {},
        # R163：质量门拒稿正文快照（最近几条）——短回/拒答型故障只报长度无法归因
        "reject_previews": [],
        "latency_by_provider": {},
        "tokens_by_provider": {},
        "errors": collections.Counter(),
        # R624：每条错误串的 {末次时刻, 窗口内命中数} —— 新鲜度维度。
        #
        # 为什么必须加：errors 是**全史累计 Counter，零新鲜度**，而遥测只 25 天
        # 历史、报表默认窗口"全史"——于是「近30 天 404 有 20 次」这种说法技术
        # 上成立，描述的却是全史。生产实测四类全在陈迹：
        #   404 僵尸名 全史 20 / 近7 天 0（末次 09-08，24 天前）
        #   超时     全史 23 / 近7 天 0（末次 09-21，11 天前）
        #   空内容   全史 24 / 近7 天 0（末次 09-22，10 天前）
        #   b.ai 余额 全史 11 / 近7 天 1（末次 09-29，3.2 天前，仍活）
        # **同一行的数字看着像"当前有多少问题"，实际绝大多数早已绝迹。**
        # R614 已为 alert_dropped_no_channel 立过同一判据（全史累计告警必须带
        # 新鲜度），errors 这条是同一类缺陷的最后一块。
        #
        # 只存末次时刻就够：渲染层拿它与 s["ts_max"]（数据集自身最新时刻）比，
        # 不另设"近期计数"——**参照点必须是数据集而非 now()**，否则回看历史
        # 数据时所有行都会被算成陈迹（全史 vs 当前，两个问题混成���个）。
        "error_last": {},
        "dry_skipped": 0,
        "runs": {},
        "ts_min": None,
        "ts_max": None,
    }
    lat_tmp, tok_tmp = collections.defaultdict(list), collections.defaultdict(list)
    runs_tmp = {
        "n": 0, "quota_blocked": 0, "active_hours_blocked": 0, "zero_candidates": 0,
        "candidates": 0, "published": 0, "unprocessed": 0,
        "skips": collections.Counter(), "last_trending": "",
        "token_limit_bypass": 0,  # R215：限流高影响放行计数（拦截的另一半）
        # R216：门槛校准两端顶分（窗口内最大，None=窗口内没有该类候选）
        "token_limit_capped_top": None, "token_limit_bypass_top": None,
        # R220：每源入选率——源名首词 → [扫描, 入选]（源治理数据面）
        # R621：桶扩为 [扫描, 入选, 旧闻, 重复推送, 跨源同题]；另置
        # feed_yield_attr 记录窗口内是否**出现过**归因字段（区别于"归因累加为 0"
        # = 观测到确实没丢，见累加处注释）
        "feed_yield": {},
        "feed_yield_attr": False,
        # R334：R273/R274 注入截断（injection_hits/injection_feeds）自写侧起
        # 只有日志 + Step Summary 两个易失出口；R275 明言「人工第一眼巡检的页面
        # 完全静默…最后缺口」但只补了 write_github_step_summary。metrics_report
        # （持久巡检面）零消费——R276 同族「有数据无出口」。命中才显形。
        "injection_hits": 0,
        "injection_feeds": collections.Counter(),
        # R335：R276 写侧的源健康细化（feeds_empty_sources / fetch_timeout_sources）
        # 此前只进日志 + Step Summary，metrics_report 零消费——生产 09-22 BlockTempo
        # 空 feed ×3 有数据无出口。按源名累加轮次，命中才显形。
        "feeds_empty_sources": collections.Counter(),
        "fetch_timeout_sources": collections.Counter(),
        # R613：三类源健康告警的**最后发生时刻**。这三类都是全史累计计数，
        # 此前渲染时完全不带时间维度——于是「4 天前已自愈的空 feed」与
        # 「正在发生的空 feed」在面板上长得一模一样，运维只能每次人工翻
        # metrics.jsonl 判断是陈迹还是活警。生产实测：注入截断最后发生距今
        # 49h、空 feed 67h、硬故障 42h——面板却与事发当日完全同貌。
        # 加"最后发生"让告警自带新鲜度，陈迹降级为ℹ️、不再占用⚠️ 视觉预算。
        "source_alarm_last": {},
        # R617：provider 默认模型名探针（scripts/probe_provider_models.py）的消费面。
        # 动机是 R612 原则的极端形态——R615 把「默认名是否还是僵尸名」自动化了，
        # 但结论只print 到 Actions 日志，而 R614 已确认通知渠道 0 个：日志不会
        # 到达任何人。**探针在跑，答案被丢弃**，比没有探针更危险（它看起来
        # 在防R263）。这里消费 outcome=provider_probe 行，取**最新一轮**结论
        # （行按时间序追加，后写覆盖先写 = 最新值，同 R113 next_slot_frees 口径）。
        # 关键：sites_ok 与 sites_total 必须**同时**渲染。只显存活数会让
        # 「核实 3/12」被读成「全绿」——未核实站（无 key / 目录不可达）是
        # **未知**，不是通过（原则：未知 ≠ 已解决，同 R613方向）。
        "provider_probe": None,
        # R342：R276 写侧扫描漏斗（fetched/stale/cached/near_dup）自写侧起只有
        # 扫描日志 + Step Summary「管线吞吐」两个易失出口，metrics_report（持久
        # 巡检面）零消费——R276 注释明言 durable 趋势可查（near_dup 抬升=去重过
        # 紧吞事件 / cached 跳涨=缓存失效 / stale 峰值=源新鲜度劣化）却无报表
        # 出口。累加总量+单轮峰值：峰值抓单轮异常尖刺，总量给基线。{字段:[总,峰]}
        "fetch_funnel": {"fetched": [0, 0], "stale": [0, 0],
                         "cached": [0, 0], "near_dup": [0, 0]},
        # R344：源硬故障/停放频率（feeds_failed/feeds_parked 计数自 R90 起写
        # run_summary，R278 补了源名但只在新故障时落；生产三个实证轮 09-17×2/
        # 09-18×1 的 feeds_failed=1 只有计数、无源名，metrics_report 零消费=写侧
        # 无出口）。空feed/超时/注入各有源名出口，硬故障（网络/HTTP≠200/畸形XML）
        # 与停放是仅剩的静默源健康信号。轮次分母+源次总量+单轮峰值；全零不渲染。
        "feed_fail_runs": 0, "feed_fail_total": 0, "feed_fail_peak": 0,
        "feed_park_runs": 0, "feed_park_total": 0, "feed_park_peak": 0,
        # R625：feeds_ok（健康源数）——三态里唯一无出口的一态，见累加处注释。
        # **min 初值不能是 0**：真出现"0 个源健康"时（全集故障/风控）必须能被
        # min 捕捉到，用 0 起算会把这个最值吃的，只剩 max=9 一档。
        "feed_ok_runs": 0, "feed_ok_total": 0, "feed_ok_min": 999, "feed_ok_max": 0,
        "trend_freq": collections.Counter(),
        "last_hot_topics": "",  # R190：全网实时热点钩子供给（HN 等）
        "hot_topic_hits": 0,    # 出现过 hot_topics 的发帖轮数
        # R193：四路信号加权命中数（供给≠命中，此前不可见）
        "boost_hits": {"campaign": 0, "trend": 0, "hot": 0,
                       "eng_up": 0, "eng_down": 0},  # R608：浏览加权命中(+/-)
        "boost_runs": 0,
        # R611：**每路信号各自**有数据的轮数。boost_runs 是"任一路有命中"的轮数，
        # 四路信号上线时间不同（R608 浏览加权 10-02 才上线），拿它当浏览加权的
        # 分母会得出"208 轮里只命中 6 次"的假结论——实测那 +2-4 全部来自**唯一
        # 1 轮**有该字段的run_summary。分母必须是"该信号自己有多少轮数据"，
        # 否则新上线的信号永远显示成"几乎不命中"，正好掩盖它其实刚跑通。
        "boost_runs_by_signal": {"campaign": 0, "trend": 0, "hot": 0,
                                 "eng": 0},
        # R201：off-pool 活动币快照（最近一轮）
        "last_campaign_off_pool": "",
        "elapsed": [],  # R126：单轮耗时样本（秒），聚平均/最长
        "sleep_elapsed": [],  # R177：拟人 pacing 累计——解释 ~370s 总耗时
        "llm_elapsed": [],
        "intel_elapsed": [],  # R183：饱和轮也付情报时间（R182 前移后）
        "quota_wait_elapsed": [],  # R184：配额边界追赶等待（R154）单列
        # R280：抓取/配图/发布分段——R177 起就随 run_summary 落盘，但读侧
        # 从未消费（全史 run_summary 三分段零读取面）：总耗时逼近回调节奏时
        # "哪一段在吃钟"没有任何报表出口。抓取段直接对 R9 deadline（默认
        # 300s）负责；配图段含转码+S3 上传（R110：视频转码以分钟计）；
        # 发布段是币安侧延迟的代理。
        "fetch_elapsed": [],
        "image_elapsed": [],
        "publish_elapsed": [],
        # R196：饱和轮情报陈旧度（R195 写侧）——80 轮/天的 quota_blocked 可见
        "quota_intel_ages": [],
        "quota_intel_degraded": 0,
    }
    lat_tmp, tok_tmp = collections.defaultdict(list), collections.defaultdict(list)
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("dry_run") is True:
            # DRY 试运行行只计数不聚合：沙盒延迟/成功率不得毒化生产调优（打标见 append_metrics）
            s["dry_skipped"] += 1
            continue
        outcome = str(r.get("outcome", "unknown"))
        s["by_outcome"][outcome] += 1
        ts = r.get("ts")
        if isinstance(ts, str) and ts:
            if s["ts_min"] is None or ts < s["ts_min"]:
                s["ts_min"] = ts
            if s["ts_max"] is None or ts > s["ts_max"]:
                s["ts_max"] = ts
        err = r.get("error") or r.get("error_code")
        prov = r.get("provider") or "unknown"
        model = r.get("model")
        who = f"{prov}/{model}" if model else str(prov)
        if _is_delivered(r):
            s["by_provider"][who] += 1
            # R616：末次成功投递时刻。**键是 provider（不含 model）**——判定"永久
            # 失败是否已自愈"必须用provider 粒度，不能用 provider/model：
            # 生产实锤 `Preset-openrouter/minimax/minimax-m3:free` 09-08 因模型
            # 下架永久失败，但同一 provider 后来换成默认名 openrouter/free，
            # 09-29 仍在成功出稿——按 model 粒度会误判成"仍死"（该模型确实
            # 再没成功过），而运营真正要决定的是"这条通道还要不要管"。
            # 换个模型就救活一条通道 = 已自愈。
            if isinstance(ts, str) and ts:
                _pp = str(prov)
                _prev_ok = s["provider_last_success"].get(_pp)
                if _prev_ok is None or ts > _prev_ok:
                    s["provider_last_success"][_pp] = ts
            hour = r.get("hour_bj", "unknown")
            try:
                hour = int(hour)
            except (TypeError, ValueError):
                hour = "unknown"
            s["by_hour"][hour] += 1
            # R343：投递篇分周——weekday_bj 恒为 int 0~6（append_metrics 写
            # bj_now.weekday()）；越界/缺字段/历史行不进桶
            _wd = r.get("weekday_bj")
            if isinstance(_wd, int) and 0 <= _wd <= 6:
                s["by_weekday"][_wd] += 1
            if r.get("source"):
                s["by_source"][str(r["source"])] += 1
            toks = r.get("tokens")
            if isinstance(toks, list) and toks:
                s["by_token"][str(toks[0])] += 1
            # R222：内容新鲜度样本——中位数一旦明显抬升（生产基线 ~1.9h），
            # 大概率是某源日期格式变化让 parse_entry_age_hours 返 None
            # （时效过滤 fail-open），旧闻纯拼内容分入选
            _age = _num(r.get("age_hours"))
            if _age is not None:
                s["pub_ages"].append(_age)
            if r.get("image"):
                s["images"] += 1
            tier = r.get("image_tier")
            if tier:
                s["image_tiers"][str(tier)] += 1
            # R123：Write2Earn 生命线度量——全文零有效挂件的帖子数（保底机制
            # 失守的直接信号；预览区无 $ 只可能是截断伪影，不看全文计数会误报）
            # R610：稳定币-only 的零挂件是**契约合规**不是失守——R316 明确
            # 「USDC/USDT 等稳定币不做挂件」，R586更进一步在选稿阶段就跳过
            # 全稳定币的帖子（skip_counts["no_token"]）。生产实录：09-30 02:04
            # USDC-only 帖 widget_count=0，那正是 R586 修复前的合规帖，不是
            # 保底被绕过。原先把它计成失守，会让告警长期钉在 1/288 假阳性上，
            # 真正的失守反而被淹没（R123 告警的第一次失效就是"狼来了"式失效）。
            # 故按tokens 判定：全为 FORCE_STRIP 词 → 归入合规桶，不进告警分母。
            wc = r.get("widget_count")
            if wc == 0:
                if _is_stablecoin_only(r.get("tokens")):
                    s["zero_widget_stablecoin_only"] += 1
                else:
                    s["zero_widget_posts"] += 1
            # R125：返佣归因标签覆盖率——零标签帖 = #Write2Earn 归因丢失
            tc = r.get("tag_count")
            if tc == 0:
                s["zero_tag_posts"] += 1
            # R284：活动标签（返佣归因第 3 席）覆盖——tag_count>0 只证明保底双
            # 标签在，活动标签静默丢失（intel 无 active_tags / _inject_campaign_tag
            # 回归）时零可见。零覆盖且 intel_degraded≠True = 情报新鲜却没活动标签
            # 可注入 = 疑似注入回归；intel 降级时的零覆盖是合法语境（无供给）。
            # R291：显式字段（注入的活动标签原文）优先于 proxy 计数——旧 proxy 把
            # 模型自写的核心代币名也算"有活动标签"，injector 被 few-shot 挤成死
            # 代码时遥测显示 107/107 假全覆盖；legacy 行无该键，回退 proxy。
            if "campaign_tag" in r:
                s["campaign_tag_evaluated"] += 1
                _ct = r.get("campaign_tag")
                if _ct:
                    s["campaign_tag_covered"] += 1
                    s["campaign_tags"][str(_ct)] += 1
                elif r.get("intel_degraded") is not True:
                    s["campaign_tag_zero_fresh"] += 1
            else:
                ctc = r.get("campaign_tag_count")
                if ctc is not None:
                    s["campaign_tag_evaluated"] += 1
                    if ctc > 0:
                        s["campaign_tag_covered"] += 1
                    elif r.get("intel_degraded") is not True:
                        s["campaign_tag_zero_fresh"] += 1
            # R285：浏览/互动 join——content_id 是 R125 起就落盘的 join 键，
            # 直到本轮才第一次有消费面。三维归因样本按发布行的既有字段分桶，
            # 回答"哪类帖有流量"（时段/体裁/来源），无 stats 的行不进任何分母。
            # R628：分母（delivered_joinable_posts）——**只数"能被 join 的投递
            # 行"**，即带 content_id 的那批。口径必须与 stats_posts 的分子同源：
            # 分子是「投递行 ∩ 内容库」，若分母取全部投递行（含没有 content_id
            # 的历史格式回执），覆盖率会被系统性低估。
            #
            # 缩进纪律：本块处于 activity 分支的 else 层（12 空格），随活动标签
            # 统计同在 `if/else` 内——**任何整块重排都可能悄悄改执行条件**。
            # R628 首版就是把这段从 12 空格挪到 8 空格，块被外移一级，16 例既有用例
            # 行为随之改变（"夹具缺字段"与"块被外移"两种现象在测试里长得一样：
            # 都是静默不输出）。改此类块时只加行、不动缩进。
            if r.get("content_id") and _is_delivery_outcome(str(r.get("outcome") or "")):
                s["delivered_joinable_posts"] += 1
            st = _stats_lookup(r.get("content_id"))
            if st and isinstance(st.get("views"), int):
                s["stats_posts"] += 1
                s["stats_views_total"] += st["views"]
                s["stats_views"].append(st["views"])
                if isinstance(st.get("likes"), int):
                    s["stats_likes"].append(st["likes"])
                if isinstance(st.get("comments"), int):
                    s["stats_comments"].append(st["comments"])
                _hb = _hour_bucket(r.get("hour_bj"))
                if _hb:
                    s["stats_by_hourbucket"].setdefault(_hb, []).append(st["views"])
                _genre = "长文" if r.get("article") else "短讯"
                s["stats_by_genre"].setdefault(_genre, []).append(st["views"])
                _src = str(r.get("source") or "")
                if _src:
                    s["stats_by_source"].setdefault(_src, []).append(st["views"])
                # R602：文风维度 × 浏览——按已落盘的轮换标签分桶（空值不进分母）
                for _field, _bucket in (("opening_hook", "stats_by_hook"),
                                        ("persona", "stats_by_persona"),
                                        ("ending_style", "stats_by_ending"),
                                        ("trade_cta_style", "stats_by_cta")):
                    _lab = r.get(_field)
                    if isinstance(_lab, str) and _lab.strip():
                        s[_bucket].setdefault(_lab.strip(), []).append(st["views"])
            # R286：长文标题眼钩普查（article_title 仅长文帖非空）——标题是信息流
            # 第一触点，数字/$挂件/疑问三类眼钩元素的覆盖率要有基线可查
            _at = r.get("article_title")
            if isinstance(_at, str) and _at.strip():
                _at = _at.strip()
                s["article_titles"].append(_at)
                if any(c.isdigit() for c in _at):
                    s["title_hooks"]["数字钩子"] += 1
                if "$" in _at:
                    s["title_hooks"]["$挂件"] += 1
                if "？" in _at or "?" in _at:
                    s["title_hooks"]["疑问钩子"] += 1
                # R329：startswith 必须对 strip 后的串——" 刚出炉：…"/全角空格前缀
                # 会漏检；命中带日期，供告警区分 R296 前历史残留 vs 新命中
                if any(_at.startswith(w) for w in _TITLE_LEADINS):
                    s["title_leadin_hits"].append(
                        (str(r.get("ts") or "")[:10], _at))
            # R130：结尾套路分布——验证 ShuffleBag 生产轮换均匀性
            if r.get("ending_style"):
                s["by_ending"][str(r["ending_style"])] += 1
            # R592：实操建议角度分布——与开场钩子/结尾套路同为 prompt 轮换槽，监测跨帖建议
            # 是否又收敛到「回踩/现货拿稳/杠杆降到最低」固定套话；legacy 行无字段跳过。
            if r.get("trade_cta_style"):
                s["by_trade_cta_style"][str(r["trade_cta_style"])] += 1
            # R288：人设分布 + 时序（集中度告警要按发布顺序取近窗）
            if r.get("persona"):
                s["by_persona"][str(r["persona"])] += 1
                s["persona_seq"].append(str(r["persona"]))
            _imp = _num(r.get("impact_score"))
            if _imp is not None:
                s["impact_scores"].append(int(_imp))
            # R292：篇幅分体裁收集（短讯目标 140~200 / 长文目标 500~800）
            _cc = _num(r.get("content_chars"))
            _cj = _num(r.get("content_cjk"))
            if _cc is not None or _cj is not None:
                _g = "长文" if r.get("article") else "短讯"
                if _cc is not None:
                    s["chars_by_genre"].setdefault(_g, []).append(int(_cc))
                if _cj is not None:
                    s["cjk_by_genre"].setdefault(_g, []).append(int(_cj))
            # R293：首段钩子三要素（与 R286 标题眼钩同口径：数字/$挂件/疑问）
            _op = _extract_opener(r.get("final_preview"))
            if _op:
                s["opener_evaluated"] += 1
                if any(c.isdigit() for c in _op):
                    s["opener_hooks"]["数字"] += 1
                if "$" in _op:
                    s["opener_hooks"]["$挂件"] += 1
                if "？" in _op or "?" in _op:
                    s["opener_hooks"]["疑问"] += 1
            # R173：情报降级注入——None=历史行无字段，不进分母
            if r.get("intel_degraded") is True:
                s["intel_degraded_posts"] += 1
            elif r.get("intel_degraded") is False:
                s["intel_fresh_posts"] += 1
            # R181：情报陈旧小时数
            _iah = _num(r.get("intel_age_hours"))
            if _iah is not None:
                s["intel_age_hours"].append(_iah)
            # R289：FNG 滞回驱动量 + 互补剥离一致性（三件套里最后两个未消费字段）
            _fhc = r.get("fng_hook_count")
            if isinstance(_fhc, int):
                s["fng_evaluated"] += 1
                s["fng_hook_hist"][_fhc] += 1
                if r.get("fng_ban_active") is True and \
                        r.get("fng_market_stripped") is False:
                    s["fng_armed_not_stripped"] += 1
        if outcome == "intel_cooldown_skip":
            # R175：想刷新但被 2h 失败退避挡下——与配额早退（根本没走到这里）区分
            s["intel_cooldown_skips"] += 1
        elif outcome == "llm_success" and r.get("stage") == "campaign_intel":
            # R199：最近成功刷新时刻——判断下一次 12h 过期、以及饱和轮是否在喂陈旧情报
            if isinstance(ts, str) and ts:
                if s["last_intel_refresh_ts"] is None or ts > s["last_intel_refresh_ts"]:
                    s["last_intel_refresh_ts"] = ts
            # R333：consume stale_date_refs（R127 写侧，报表此前零出口）
            _sdr = _num(r.get("stale_date_refs"))
            if _sdr is not None and _sdr > 0:
                s["stale_date_refs_refreshes"] += 1
                s["stale_date_refs_hits"] += int(_sdr)
                if int(_sdr) > s["stale_date_refs_max"]:
                    s["stale_date_refs_max"] = int(_sdr)
        elif outcome == "llm_rejected":
            s["reject_by_stage"][str(r.get("stage", "unknown"))] += 1
            s["reject_by_provider"][who] += 1
            if r.get("reason"):
                reason_str = str(r["reason"])
                s["reject_reasons"][reason_str[:60]] += 1
                # R302：真·24h 永久失败按 提供商×原因 单列（前缀匹配部署的 fail_reason 标记）
                for tag, label in _PERMANENT_FAIL_TAGS:
                    if reason_str.startswith(tag):
                        key = (who, label)
                        s["permanent_failures"][key] += 1
                        # R304：记末次命中时刻（字符串 ISO ts 可字典序比较）
                        if isinstance(ts, str) and ts:
                            prev = s["permanent_failures_last"].get(key)
                            if prev is None or ts > prev:
                                s["permanent_failures_last"][key] = ts
                        break
            # R163：短回/质量拒稿的原文快照（有则收，窗口内只留最近 5 条）
            pv = r.get("content_preview")
            if isinstance(pv, str) and pv:
                s["reject_previews"].append({
                    "ts": r.get("ts"),
                    "stage": r.get("stage"),
                    "provider": who,
                    "reason": (r.get("reason") or "")[:40],
                    "finish_reason": r.get("finish_reason"),
                    "preview": pv[:80],
                })
                if len(s["reject_previews"]) > 5:
                    s["reject_previews"] = s["reject_previews"][-5:]
        # 失败行（llm_failed / 无 stage 的异常行）的 reason 是错误诊断的唯一载体——
        # 生产遥测里 404/超时/空回全写在 reason，error 字段几乎恒空。没写 stage 的
        # reject 行同样兜进 errors，避免"错误面板空但原因字段一大堆"的失真报表。
        if outcome == "llm_failed" and not err:
            err = r.get("reason")
        if err:
            _ek = str(err)[:60]
            s["errors"][_ek] += 1
            # R624：末次时刻（后写覆盖先写=取最新）。缺 ts 的历史行**不写**——
            # 时间戳缺失时按"新鲜度未知"处理，不能当成陈迹（判据：未知≠已解决）。
            _ets = r.get("ts")
            if _ets:
                _prev = s["error_last"].get(_ek)
                if _prev is None or str(_ets) > str(_prev):
                    s["error_last"][_ek] = str(_ets)
        lat = _num(r.get("llm_latency_sec"))
        if lat is not None:
            lat_tmp[str(prov)].append(lat)
        tok = _num(r.get("tokens_used"))
        if tok is not None:
            tok_tmp[str(prov)].append(tok)
        # R620：provider **质量产出**统计（通过率 + 净产出）。
        #
        # 为什么必须单独一栏：已有的「通道位次」行只显示"发N/拒M"，读者要心算
        # 才知道通道好坏。生产实测差距是 7 倍——Preset-openrouter 通过率 8%
        # vs Preset-stepfun-flash 55%。而按调用量排序会得出完全相反的结论
        #（openrouter 109 次调用看着"很常用"，实际每次都在烧额度却几乎不产出）。
        #
        # 口径（**分子是 binance_published，不是 llm_success**——实测全部46 条
        # llm_success 的 stage都是 campaign_intel，那是情报刷新不是发帖产出；
        # 发帖成功记在投递行上）。分母 = 投递成功 + 拒稿 + 失败，三者同粒度
        # 折叠到 provider 短名（同_provider_dispatch_order）。
        #
        # R623：尝试计数在此**单点累加**——口径须与 provider_quality 同源
        # （同一短名折叠、同一非真实通道过滤），否则两张表会数出不同的通道数，
        # "池内 12 站 / 有遥测 4 条" 这类对比就失去意义。
        #
        # 排除 provider_probe：那是 R617 探针**每轮一次的目录列举**，不是 LLM
        # 尝试。若计入，会让「每轮都被探针看见」的通道显示成"被尝试过N 次"，
        # 恰好把本条要抓的盲区（模型可用性从未被验证）伪装成已覆盖。
        if outcome != "provider_probe" and prov and str(prov) not in ("-", "unknown"):
            s["provider_attempts"][str(prov).split("/", 1)[0]] = \
                s["provider_attempts"].get(str(prov).split("/", 1)[0], 0) + 1
        if _is_delivered(r):
            _qp = str(prov).split("/", 1)[0]
            if _qp and _qp not in ("-", "unknown"):
                s["provider_quality"].setdefault(
                    _qp, {"ok": 0, "rej": 0, "fail": 0})["ok"] += 1
        elif outcome in ("llm_rejected", "llm_failed"):
            _qp = str(prov).split("/", 1)[0]
            if _qp and _qp not in ("-", "unknown"):
                _q = s["provider_quality"].setdefault(
                    _qp, {"ok": 0, "rej": 0, "fail": 0})
                if outcome == "llm_rejected":
                    _q["rej"] += 1
                else:
                    _q["fail"] += 1
        # R617：provider 默认名探针结论。每轮一行，取最新一轮（后写覆盖先写）。
        # 三态严格区分，这是本条存在的全部意义：
        #   probe_ok=True  且 sites_ok == sites_total → 全部核实通过
        #   probe_ok=True  但 sites_ok <  sites_total → **部分未核实**（无 key /
        #       目录不可达）。绝不能只显存活数：那会让「核实 3/12」读成「全绿」，
        #       而未核实站的默认名随时可能已经是僵尸名（R263 正是这么发生的）。
        #   probe_ok=False → 探针自身没跑成，结论不可信，按活警渲染。
        if outcome == "provider_probe":
            runs_tmp["provider_probe"] = {
                "ts": str(r.get("ts") or ""),
                "ok": r.get("probe_ok") is True,
                "total": _num(r.get("sites_total")),
                "sites_ok": _num(r.get("sites_ok")),
                "unknown": _num(r.get("sites_unknown")),
                "unknown_sites": str(r.get("unknown_sites") or ""),
                # R618：僵尸名单独成字段。必须与 unknown 分开——僵尸名是**已确证
                # 的事实**（默认名确实不在目录里），未核实只是覆盖缺口。生产
                # 实锤：aihubmix 的 coding-glm-5.3-flash-free 已从 417 模型目录
                # 消失，而同轮有 8 站因缺 key 未核实——若用单一 probe_ok 判据，
                # 这个真僵尸名会被"整轮不可信"吞掉。
                "zombies": _num(r.get("zombie_count")),
                "zombie_sites": str(r.get("zombie_sites") or ""),
                "error": str(r.get("probe_error") or ""),
                "elapsed": _num(r.get("probe_elapsed_sec")),
            }
        # R92：run_summary 行聚合——饱和/零候选/跳过分布的报表端消费，
        # 不再需要手写临时脚本回答"配额是否该调"（R90/R91 分析实录）
        if outcome == "run_summary":
            runs_tmp["n"] += 1
            _el = _num(r.get("run_elapsed_sec"))
            if _el is not None:
                runs_tmp["elapsed"].append(_el)
            quota_blocked = r.get("quota_blocked") is True
            hours_blocked = r.get("active_hours_blocked") is True
            if quota_blocked:
                runs_tmp["quota_blocked"] += 1
            # R113/R6：配额释放估算取"最近一条带该字段的 run_summary"（行按时间序
            # 追加，后写覆盖先写 = 最新值）。**不能只在 quota_blocked 行里找**：
            # R129 之后正常发帖轮也会写该字段，只在配额行里找会让报表长期显示
            # 一个早已过期的旧估算（正是 R129 要修的问题，读侧当时漏改）。
            if r.get("next_slot_frees"):
                runs_tmp["next_slot_frees"] = r["next_slot_frees"]
            if r.get("next_slot_frees_min") is not None:
                try:
                    runs_tmp["next_slot_frees_min"] = int(r["next_slot_frees_min"])
                except (TypeError, ValueError):
                    pass
            if hours_blocked:
                runs_tmp["active_hours_blocked"] += 1
            cand = int(_num(r.get("candidates")) or 0)
            pub = int(_num(r.get("published")) or 0)
            unproc = int(_num(r.get("unprocessed")) or 0)
            runs_tmp["candidates"] += cand
            runs_tmp["published"] += pub
            runs_tmp["unprocessed"] += unproc
            # 零候选 = 真去抓了但没有候选。配额满/时段外的轮根本没抓（cand=0
            # 只是"没看"），混入会让饱和期被双重标记成"配额饱和 N 轮 / 零候选 N 轮"
            if cand == 0 and pub == 0 and not quota_blocked and not hours_blocked:
                runs_tmp["zero_candidates"] += 1
            # R10：外部依赖降级信号——决定本轮 no_token/盘面缺失是"真的没有"
            # 还是"数据源降级了"（没有它归因会跑偏）。取最近一次出现的值。
            if r.get("symbols_degraded"):
                runs_tmp["symbols_degraded"] = str(r["symbols_degraded"])
            if r.get("market_missing"):
                runs_tmp["market_missing"] = str(r["market_missing"])
            tr = r.get("trending")
            if isinstance(tr, str) and tr.strip():
                runs_tmp["last_trending"] = tr
                # R117：热搜 token 频次——跨快照统计市场注意力的持续度
                for tok in tr.split():
                    runs_tmp["trend_freq"][tok] += 1
            # R190：热点钩子供给（与币种热搜互补的跨域注意力）
            ht = r.get("hot_topics")
            if isinstance(ht, str) and ht.strip():
                runs_tmp["last_hot_topics"] = ht
                runs_tmp["hot_topic_hits"] += 1
            # R193：加权命中数
            _bh = {}
            for _k, _f in (("campaign", "campaign_boost_hits"),
                           ("trend", "trend_boost_hits"),
                           ("hot", "hot_boost_hits"),
                           ("eng_up", "engagement_boost_up"),
                           ("eng_down", "engagement_boost_down")):
                _v = _num(r.get(_f))
                if _v is not None and _v > 0:
                    _bh[_k] = int(_v)
            if _bh:
                runs_tmp["boost_runs"] += 1
                for _k, _v in _bh.items():
                    runs_tmp["boost_hits"][_k] += _v
            # R611：每路信号各自的有数据轮数（eng 合并 up/down 为一路）。
            # 判据：字段**存在**即算有数据轮（值为 0 也是有效观测——"这轮没命中"
            # 与"这轮没这个字段"是两件事，只有前者能进命中率分母）。
            for _sig, _flds in (("campaign", ("campaign_boost_hits",)),
                                ("trend", ("trend_boost_hits",)),
                                ("hot", ("hot_boost_hits",)),
                                ("eng", ("engagement_boost_up",
                                         "engagement_boost_down"))):
                if any(_num(r.get(_f)) is not None for _f in _flds):
                    runs_tmp["boost_runs_by_signal"][_sig] += 1
            _cop = r.get("campaign_off_pool")
            if isinstance(_cop, str) and _cop.strip():
                runs_tmp["last_campaign_off_pool"] = _cop
            for k, v in r.items():
                if k.startswith("skipped_") and isinstance(v, (int, float)):
                    runs_tmp["skips"][k[len("skipped_"):]] += int(v)
            # R215：限流高影响放行（放行字段不带 skipped_ 前缀，显式收集）
            _tlb = _num(r.get("token_limit_bypassed"))
            if _tlb is not None and _tlb > 0:
                runs_tmp["token_limit_bypass"] += int(_tlb)
            # R216：门槛校准两端顶分——窗口内取最大（有则收，历史行无字段不进）
            _ct = _num(r.get("token_limit_capped_top"))
            if _ct is not None and (runs_tmp["token_limit_capped_top"] is None
                                    or _ct > runs_tmp["token_limit_capped_top"]):
                runs_tmp["token_limit_capped_top"] = int(_ct)
            _bt = _num(r.get("token_limit_bypass_top"))
            if _bt is not None and (runs_tmp["token_limit_bypass_top"] is None
                                    or _bt > runs_tmp["token_limit_bypass_top"]):
                runs_tmp["token_limit_bypass_top"] = int(_bt)
            # R220：每源入选率累加（有则收，历史行无字段不进）
            for _fname, _fy in (r.get("per_feed_yield") or {}).items():
                if isinstance(_fy, dict):
                    # R621：桶从 [扫描, 入选] 扩到 5 元组，末三位是丢弃归因。
                    # 历史行只有前两项 => 归因累加 0（R621 原则 4：缺失即 0，
                    # 不因旧数据缺字段而崩）。**但"累加出 0"与"字段缺失"必须
                    # 可区分**：前者是观测到没丢，后者是没观测过。故另置
                    # feed_yield_attr 布尔，而不是靠桶长度去反推（桶总是 5 长，
                    # 反推必然误判——这正是 R617 isinstance(int/float) 那类坑）。
                    _agg = runs_tmp["feed_yield"].setdefault(
                        _fname, [0, 0, 0, 0, 0])
                    _agg[0] += int(_fy.get("entries") or 0)
                    _agg[1] += int(_fy.get("kept") or 0)
                    _agg[2] += int(_fy.get("discarded_stale") or 0)
                    _agg[3] += int(_fy.get("discarded_cached") or 0)
                    _agg[4] += int(_fy.get("discarded_dup") or 0)
                    if any(k in _fy for k in ("discarded_stale",
                                              "discarded_cached", "discarded_dup")):
                        runs_tmp["feed_yield_attr"] = True
            # R334：注入截断（R273/R274 写侧，报表此前零出口）
            _ih = _num(r.get("injection_hits"))
            if _ih is not None and _ih > 0:
                runs_tmp["injection_hits"] += int(_ih)
                if isinstance(ts, str) and ts:
                    _p = runs_tmp["source_alarm_last"]
                    if _p.get("injection") is None or ts > _p["injection"]:
                        _p["injection"] = ts
            _isrc = r.get("injection_feeds")
            if isinstance(_isrc, dict):
                for _fn, _fc in _isrc.items():
                    _v = _num(_fc)
                    if _v is not None and _v > 0:
                        runs_tmp["injection_feeds"][str(_fn)] += int(_v)
            # R335：源健康细化（R276 写侧，报表此前零出口）
            for _sk, _dk in (("feeds_empty_sources", "feeds_empty_sources"),
                             ("fetch_timeout_sources", "fetch_timeout_sources")):
                _sv = r.get(_sk)
                if isinstance(_sv, str) and _sv.strip():
                    for _fn in _sv.split(" | "):
                        if _fn.strip():
                            runs_tmp[_dk][_fn.strip()] += 1
                elif isinstance(_sv, (list, tuple)):
                    for _fn in _sv:
                        if _fn:
                            runs_tmp[_dk][str(_fn)] += 1
                if isinstance(_sv, (str, list, tuple)) and _sv and \
                        isinstance(ts, str) and ts:
                    _p = runs_tmp["source_alarm_last"]
                    if _p.get(_sk) is None or ts > _p[_sk]:
                        _p[_sk] = ts
            # R613：feeds_failed（硬故障）最后发生时刻
            if (_num(r.get("feeds_failed")) or 0) > 0 and \
                    isinstance(ts, str) and ts:
                _p = runs_tmp["source_alarm_last"]
                if _p.get("feeds_failed") is None or ts > _p["feeds_failed"]:
                    _p["feeds_failed"] = ts
            # R342：扫描漏斗累加（R276 写侧，报表此前零出口）——总量给基线、
            # 单轮峰值抓异常尖刺；有则收，历史行无字段/零值不进（不抬计数）。
            for _fk in ("fetched", "stale", "cached", "near_dup"):
                _fv = _num(r.get(_fk))
                if _fv is not None and _fv > 0:
                    _acc = runs_tmp["fetch_funnel"][_fk]
                    _acc[0] += int(_fv)
                    if int(_fv) > _acc[1]:
                        _acc[1] = int(_fv)
            # R344：源硬故障/停放计数累加（R90/R278 写侧，报表此前零出口）——
            # int 字段且 >0 才计入"故障轮/停放轮"（历史行无字段或零值不进，
            # 不抬分母、零故障轮不渲染，与漏斗/源健康同零噪音口径）。
            _ffl = _num(r.get("feeds_failed"))
            if _ffl is not None and _ffl > 0:
                runs_tmp["feed_fail_runs"] += 1
                runs_tmp["feed_fail_total"] += int(_ffl)
                if int(_ffl) > runs_tmp["feed_fail_peak"]:
                    runs_tmp["feed_fail_peak"] = int(_ffl)
            _fpk = _num(r.get("feeds_parked"))
            if _fpk is not None and _fpk > 0:
                runs_tmp["feed_park_runs"] += 1
                runs_tmp["feed_park_total"] += int(_fpk)
                if int(_fpk) > runs_tmp["feed_park_peak"]:
                    runs_tmp["feed_park_peak"] = int(_fpk)
            # R625：feeds_ok（正常抓到条目的源数）——**三态里唯一没有出口的一态**。
            #
            # 为什么必须补：R344/R613 已把故障(feeds_failed)与停放(feeds_parked)
            # 做成带新鲜度的告警行，但**"本轮几个源健康"从来没渲染过**。后果是
            # 面板只能回答"有没有坏源"，回答不了"还剩几个能用的"——而后者才是
            # 源治理的真正问题（3/9 健康与9/9 健康是两种完全不同的处境）。
            # 生产实测 feeds_ok 均 8.94/最大 9，**9 源几乎轮轮全健康**，这条
            # 事实目前完全不可见。
            #
            # 分母纪律（R621）：feeds_ok 只在**非配额饱和轮**出现（270/276 轮），
            # 因为饱和轮 sys.exit 在抓取之前。所以**不抬分母、不与故障轮混算**——
            # 另立"抓取轮"分母，缺该字段的行不进分母（字段存在即计入，值 0 也是
            # 有效观测：真的一个源都没抓到时 fields 仍在）。
            _fok = _num(r.get("feeds_ok"))
            if _fok is not None:
                runs_tmp["feed_ok_runs"] += 1
                runs_tmp["feed_ok_total"] += int(_fok)
                if int(_fok) < runs_tmp["feed_ok_min"]:
                    runs_tmp["feed_ok_min"] = int(_fok)
                if int(_fok) > runs_tmp["feed_ok_max"]:
                    runs_tmp["feed_ok_max"] = int(_fok)
            # R177：分段耗时（有则收，历史行无字段不进）
            _sl = _num(r.get("sleep_elapsed_sec"))
            if _sl is not None and _sl > 0:
                runs_tmp["sleep_elapsed"].append(_sl)
            _llm = _num(r.get("llm_elapsed_sec"))
            if _llm is not None and _llm > 0:
                runs_tmp["llm_elapsed"].append(_llm)
            _intel = _num(r.get("intel_elapsed_sec"))
            if _intel is not None and _intel > 0:
                runs_tmp["intel_elapsed"].append(_intel)
            # R184：配额边界追赶等待——此前混在 run_elapsed 里无法归因
            _qw = _num(r.get("quota_wait_elapsed_sec"))
            if _qw is not None and _qw > 0:
                runs_tmp["quota_wait_elapsed"].append(_qw)
            # R280：抓取/配图/发布分段——R177 起就随 run_summary 落盘，读侧
            # 从未消费（全史 run_summary 三分段零读取面）：总耗时逼近回调节奏
            # 时"哪一段在吃钟"没有任何报表出口
            for _k in ("fetch_elapsed_sec", "image_elapsed_sec", "publish_elapsed_sec"):
                _v = _num(r.get(_k))
                if _v is not None and _v > 0:
                    runs_tmp[_k.replace("_sec", "")].append(_v)
            # R196：饱和轮情报陈旧度（仅 quota_blocked 轮，避免与发帖回执重复计）
            if quota_blocked:
                _ia = _num(r.get("intel_age_hours"))
                if _ia is not None:
                    runs_tmp["quota_intel_ages"].append(_ia)
                if r.get("intel_degraded") is True:
                    runs_tmp["quota_intel_degraded"] += 1
    s["runs"] = {
        **{k: v for k, v in runs_tmp.items() if k not in ("skips", "trend_freq", "elapsed", "sleep_elapsed", "llm_elapsed", "intel_elapsed", "quota_wait_elapsed", "quota_intel_ages", "fetch_elapsed", "image_elapsed", "publish_elapsed")},
        "skips": dict(runs_tmp["skips"]),
        "trend_freq": dict(runs_tmp["trend_freq"].most_common(8)),
    }
    # R126：单轮耗时聚合——平均/最长（秒），20 分钟节奏下的堆积预警
    if runs_tmp["elapsed"]:
        s["runs"]["avg_elapsed_sec"] = round(sum(runs_tmp["elapsed"]) / len(runs_tmp["elapsed"]), 1)
        s["runs"]["max_elapsed_sec"] = round(max(runs_tmp["elapsed"]), 1)
        s["runs"]["n_elapsed"] = len(runs_tmp["elapsed"])
    # R177：拟人 sleep 与 LLM 分段——总耗时 ~370s 的大头是 pacing 不是模型。
    # R6：各分段样本数必须一起透出——LLM 均值只覆盖"有该字段"的轮次，与
    # "单轮耗时"（覆盖全部轮次）不是同一批样本，不标样本量会读出
    # "分段和 > 总量"的假象（实测 平均 29.7s / LLM 52.0s）。
    if runs_tmp["sleep_elapsed"]:
        s["runs"]["avg_sleep_sec"] = round(sum(runs_tmp["sleep_elapsed"]) / len(runs_tmp["sleep_elapsed"]), 1)
        s["runs"]["max_sleep_sec"] = round(max(runs_tmp["sleep_elapsed"]), 1)
        s["runs"]["n_sleep_sec"] = len(runs_tmp["sleep_elapsed"])
    if runs_tmp["llm_elapsed"]:
        s["runs"]["avg_llm_sec"] = round(sum(runs_tmp["llm_elapsed"]) / len(runs_tmp["llm_elapsed"]), 1)
        s["runs"]["n_llm_sec"] = len(runs_tmp["llm_elapsed"])
    if runs_tmp["intel_elapsed"]:
        s["runs"]["avg_intel_sec"] = round(sum(runs_tmp["intel_elapsed"]) / len(runs_tmp["intel_elapsed"]), 1)
        s["runs"]["n_intel_sec"] = len(runs_tmp["intel_elapsed"])
    # R184：追赶等待均值——R177 分段只覆盖情报/LLM/拟人间隔，18:06/18:29 轮
    # 150~270s 的未解释差额实为 R154 等待；R280 补齐抓取/配图/发布后总账可对平
    if runs_tmp["quota_wait_elapsed"]:
        s["runs"]["avg_quota_wait_sec"] = round(
            sum(runs_tmp["quota_wait_elapsed"]) / len(runs_tmp["quota_wait_elapsed"]), 1)
    # R196：饱和轮情报陈旧度
    if runs_tmp["quota_intel_ages"]:
        _qia = runs_tmp["quota_intel_ages"]
        s["runs"]["avg_quota_intel_age_h"] = round(sum(_qia) / len(_qia), 1)
        s["runs"]["max_quota_intel_age_h"] = round(max(_qia), 1)
    # R280：抓取/配图/发布分段聚合。抓取段直接对 R9 deadline（默认 300s）负责，
    # max 比均值更早暴露逼近；配图段含转码+S3 上传（R110：视频转码以分钟计）；
    # 发布段是币安侧延迟的代理（本地写完≠平台可见）
    for _seg in ("fetch", "image", "publish"):
        _vals = runs_tmp[f"{_seg}_elapsed"]
        if _vals:
            s["runs"][f"avg_{_seg}_sec"] = round(sum(_vals) / len(_vals), 1)
            s["runs"][f"max_{_seg}_sec"] = round(max(_vals), 1)
            s["runs"][f"n_{_seg}_sec"] = len(_vals)
    for prov, vals in lat_tmp.items():
        s["latency_by_provider"][prov] = round(sum(vals) / len(vals), 1)
    for prov, vals in tok_tmp.items():
        s["tokens_by_provider"][prov] = {
            "avg": int(round(sum(vals) / len(vals), 0)),
            "total": int(sum(vals)),
        }
    # R612：浏览加权表覆盖审计。回答一个此前没人能回答的问题——
    #「R608 到底在给哪些币加分/减分，被min_n 挡在表外的又是哪些」。
    # by_token 已是**首标的**发布量（与 main.apply_engagement_boost 的
    # extract_tokens(...)[0] 同口径），所以"发得多"与"进表"是直接可比的。
    _eng = load_token_engagement()
    if _eng and _eng["tokens"]:
        _mn = _eng["min_n"]
        _in, _out = [], []
        for _tk, _rec in _eng["tokens"].items():
            _pub_n = int(s["by_token"].get(_tk, 0))
            _item = {"token": _tk, "n": _rec["n"],
                     "median_views": _rec["median_views"], "published": _pub_n}
            (_in if _rec["n"] >= _mn else _out).append(_item)
        _in.sort(key=lambda x: -x["median_views"])
        _out.sort(key=lambda x: -x["median_views"])
        s["eng_boost"] = {"min_n": _mn, "in_table": _in, "out_table": _out}
    return s


def funnel(rows):
    """发布成功率漏斗（R119 改故事口径）：分母 = "真正进入 LLM 尝试的故事"。
    此前按行计数，但 failover 让一个故事落多行（生产实证 13:03Z：b.ai 429 拒一行
    + openrouter 发布一行；12:48Z：拒一行 + 外层 failed 一行）——同一故事被算
    两次尝试，4 天实测行口径 30.5% vs 故事口径 66.7%，failover 噪声把成功率腰斩。
    故事键 = (日期, title)：拒稿行与投递行都带 title 截断（news_id 仅投递侧有，
    title 是唯一全侧可用键）；跨日同题按不同故事计。无 title 孤儿行（异常路径）
    退化为旧行计数，历史测试语义不变。
    返回额外带 failover_rescued = 先拒稿后投递的故事数（failover/重试救回量，
    衡量多提供商链的真实价值）。"""
    delivered_stories: set = set()
    rejected_stories: set = set()
    attempted_stories: set = set()
    orphan_attempted = 0
    orphan_delivered = 0
    for r in rows:
        if not isinstance(r, dict) or r.get("dry_run") is True:
            continue
        outcome = str(r.get("outcome", ""))
        is_del = _is_delivered(r)
        title = str(r.get("title") or "").strip()
        if not title:
            # 孤儿行退化为行计数
            if is_del:
                orphan_delivered += 1
            elif outcome in ATTEMPT_OUTCOMES and r.get("stage") != "campaign_intel":
                orphan_attempted += 1
            continue
        key = (str(r.get("ts", ""))[:10], title)
        if is_del:
            delivered_stories.add(key)
            attempted_stories.add(key)
        elif outcome in ATTEMPT_OUTCOMES and r.get("stage") != "campaign_intel":
            attempted_stories.add(key)
            if outcome in ("llm_rejected", "llm_failed", "publish_failed"):
                rejected_stories.add(key)
    delivered = len(delivered_stories) + orphan_delivered
    attempted = len(attempted_stories) + orphan_attempted + orphan_delivered
    return {"attempted": attempted, "delivered": delivered,
            "rate": round(delivered / attempted, 3) if attempted else None,
            "failover_rescued": len(rejected_stories & delivered_stories)}


def _top(counter, n=TOP_N):
    return counter.most_common(n)


def _format_permanent_failures(counter, last_seen=None, last_success=None):
    """R302：把 (provider, 原因) → 次数 渲染成 '提供商 (原因 ×N)'，命中数降序
    （最该处理的排最前）；空计数器返回空串（沿用"停放的源"零命中零噪音惯例）。
    R304：带 last_seen（(provider,原因)→末次 ISO ts）时追加 '最近 MM-DD'。

    R616：返回 (活警段, 退役段) 两段而非单串。判据是**末次失败与末次成功的时间
    先后**，不是日期本身：
      - 末次成功晚于末次失败 → 该通道此后仍在成功出稿，永久失败**已自愈**
        （典型：某个免费模型被下架后默认名换成聚合路由，通道反而更健康了）
        → 归入退役段，只保留信息不催人工处置；
      - 末次失败晚于末次成功 → 此后再没成功过，**仍死**，需人工处置。
    缺时间戳时归入活警（未知 ≠ 已解决，与 R613 同一方向）。
    """
    if not counter:
        return ("", "")
    last_seen = last_seen or {}
    last_success = last_success or {}
    live, retired = [], []
    for (prov, label), cnt in counter.most_common():
        ts = last_seen.get((prov, label))
        # 失败键是 provider/model，成功键只有 provider —— 取前缀匹配到最近的
        # provider 段（键内不含空格，直接按 "/" 切第一段即 provider 名）。
        ok = last_success.get(prov.split("/", 1)[0])
        recovered = (isinstance(ts, str) and isinstance(ok, str)
                     and len(ts) >= 10 and len(ok) >= 10 and ok > ts)
        seg = f"{prov} ({label} ×{cnt}"
        if isinstance(ts, str) and len(ts) >= 10:
            seg += f", 最近 {ts[5:10]}"
        if recovered:
            seg += "，此后该通道已恢复出稿"
        seg += ")"
        (retired if recovered else live).append(seg)
    return (" | ".join(live), " | ".join(retired))


def _provider_pool_from_main(main_path=None):
    """R623：AST 解析 main.py 的 extra_keys 字面量键名 → 池内通道清单。

    为什么必须 AST 而不是 import / 正则 / 硬编码（R617 已立判据，此处复用）：
    - import main 有副作用（读环境、写状态文件），报表必须保持纯只读；
    - 正则改个缩进就静默漏站（R615 硬编码 2 站、池内 12 站，历史 3 次僵尸名
      事件 2 次落在盲区）；
    - 硬编码清单与池定义各改一处，必然漂移。

    解析失败**必须响亮失败并返回空**（空池 = 面板显式报"失明"），
    绝不能静默退化成"全部通道都已被尝试"——那是把未知显示成通过（R617 纪律：
    沉默不是通过）。
    """
    path = main_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    try:
        import ast
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "extra_keys":
                    if isinstance(node.value, ast.Dict):
                        return [k.value for k in node.value.keys
                                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    return []


def _provider_dispatch_order(by_provider, reject_by_provider, latency_by_provider):
    """R337：把「发/拒」计数按 failover 实际调用顺序（延迟↑=先试）汇总成每通道一
    行，回答反复出现的「某源份额低=没在用？」误读——慢而稳的兜底源（如 stepfun）
    天然排在链尾、只在前序全挂时才够得到，份额小不等于闲置。键统一折叠到短通道名
    （who.split('/',1)[0]，与 latency_by_provider 同粒度=failover 单元；openrouter
    名下多模型合并计入一个通道）；剔除 '-'/'unknown'/空通道（非真实通道无法归位）。
    无延迟样本的通道排在有延迟者之后（视为链尾未知位）。返回
    [(prov, 发, 拒, 延迟或None), ...] 已按 failover 顺序排好；无可归位数据返回 []。"""
    pub = collections.Counter()
    rej = collections.Counter()
    for who, cnt in (by_provider or {}).items():
        pub[str(who).split("/", 1)[0]] += cnt
    for who, cnt in (reject_by_provider or {}).items():
        rej[str(who).split("/", 1)[0]] += cnt
    provs = {p for p in (set(pub) | set(rej)) if p and p not in ("-", "unknown")}
    lat = latency_by_provider or {}
    inf = float("inf")
    rows = [(p, pub.get(p, 0), rej.get(p, 0), lat.get(p)) for p in provs]
    # 延迟升序=failover 调用顺序；无延迟样本视为链尾；同位次按尝试量降序稳定收敛
    rows.sort(key=lambda t: (t[3] if t[3] is not None else inf, -(t[1] + t[2]), t[0]))
    return rows


def render_text(s, rows=None):
    """人类可读简报"""
    lines = [
        "## metrics 遥测简报",
        f"- 样本: {s['total']} 行"
        + (f"（时间跨度 {s['ts_min'][:16]} → {s['ts_max'][:16]}）" if s["ts_min"] else "（尚无带时间戳样本）")
        + (f"，其中 {s['dry_skipped']} 行 dry-run 已排除" if s["dry_skipped"] else ""),
        f"- outcome 分布: {dict(s['by_outcome']) or '—'}",
    ]
    # R330：错误报警因 0 渠道被丢弃必须单独成行——混在 outcome 分布里等于消失
    # （生产实锤：R301 permanent 报警进黑洞，_alert_state 全史为空才发现）。
    # R614：与 R613 同源——这行是全史累计，缺时间维度时陈迹与"此刻仍在丢报警"
    # 同貌。生产实测最后一条静默丢弃距今 66h，渠道 0 个是**持续状态**（不是
    # 瞬时事件），所以主体措辞不变（配置指引长期有效），只补"最后发生"让
    # 读者知道最近一次丢的是什么、隔了多久。
    if s['by_outcome'].get('alert_dropped_no_channel'):
        _drop = [r for r in (rows or [])
                 if r.get("outcome") == "alert_dropped_no_channel"]
        _dlast = ""
        if _drop:
            _ts_d = max((str(r.get("ts") or "") for r in _drop), default="")
            if _ts_d:
                _dlast = _ts_d[:16].replace("T", " ")
        _note_d = f"，最后发生 {_dlast}" if _dlast else ""
        lines.append(
            f"  📵 运营报警静默丢弃 ×{s['by_outcome']['alert_dropped_no_channel']}"
            f"（通知渠道 0 个{_note_d}，permanent 失败/崩溃等错误报警未能送达"
            f"——请配置 SERVERCHAN_KEY/PUSHPLUS_TOKEN/BARK_KEY/TELEGRAM_*/"
            f"WEBHOOK_URL 任一）")
    # R617：provider 默认模型名探针（R263 僵尸名检查）的结论消费面。
    # 放在报警静默丢弃行之后：两者是同一族缺口——「本该被看到的运维信号没有
    # 到达人」。R615 把僵尸名检查自动化了，但只 print 到 Actions 日志，而上一行
    # 刚证明通知渠道是 0 个：探针在跑，答案被丢弃。这里给它一个持久出口。
    # 数据挂在 s["runs"] 下（与 run_summary 同族：都是「本轮跑下来怎么样」的
    # 轮次级结论，而非投递事件）。
    _pp = (s.get("runs") or {}).get("provider_probe")
    if _pp:
        _ppt = str(_pp.get("ts") or "")[:16].replace("T", " ")
        _tot = _pp.get("total")
        _okc = _pp.get("sites_ok")
        _unk = _pp.get("unknown")
        _zz = _pp.get("zombies")
        # _num() 归一化后返回 **float**（不是 int），所以这里按数值有效性判断，
        # 不能用 isinstance(x, int) —— 那样会让分母恒为假、把「核实 3/12」渲染成
        # 「核实 —」，进而让"部分未核实"分支永不触发、退化成"全部通过"。
        # 教训同原则 2：判断字段是否存在时，要用字段实际会被归一化成的那种类型。
        _tot_i = int(_tot) if isinstance(_tot, (int, float)) else None
        _okc_i = int(_okc) if isinstance(_okc, (int, float)) else None
        _unk_i = int(_unk) if isinstance(_unk, (int, float)) else None
        _zom_i = int(_zz) if isinstance(_zz, (int, float)) else None
        # 分母不成立时不显示比例（避免 "None/None"），只报绝对数。
        _cov = (f"{_okc_i}/{_tot_i}" if _okc_i is not None and _tot_i else "—")
        _unk_note = f"，未核实 {_unk_i} 站" if _unk_i else ""
        # R618：僵尸名与未核实必须**分行渲染**，且僵尸名优先。
        # 我在 R617 首版犯的错：用 probe_ok 单判据，把「有站未核实」当成
        # 「整轮结论不可信」→ 渲染成"探针未完成"→ 生产实锤的 aihubmix 僵尸名
        # （coding-glm-5.3-flash-free 已从 417 模型目录消失）被活活吞掉。
        # **已确证的事实不该被"另一批未知"稀释**——这与 R614「瞬时/持续分级」
        # 同一方向：僵尸名是确定结论，未核实是覆盖缺口，两者的处置动作完全不同
        # （换名 vs 补 key）。
        if _zom_i:
            _zn = _pp.get("zombie_sites") or ""
            lines.append(
                f"  💀 provider 默认名僵尸名 {_zom_i} 站（{_zn}，"
                f"最近 {_ppt or '?'}）——改 *_MODEL env 指向该站现存活名，"
                f"或撤掉该 preset；不换则该通道每次调用都404 空转")
        if not _pp.get("ok") and not _zom_i:
            # 探针自己没跑成**且**没抓到任何僵尸名 → 活警：此时面板上关于
            # provider健康的一切结论都不可信。probe_error 本身已带未核实站数，
            # 不再叠加 _unk_note（同一事实说两遍）。
            _why = _pp.get("error") or "原因未记录"
            lines.append(
                f"  🩺 provider 默认名探针未完成（{_why}，"
                f"最近 {_ppt or '?'}）——本轮僵尸名结论不可信，"
                f"检查 scripts/probe_provider_models.py")
        elif _unk_i:
            # 部分未核实：默认名"没被报死"≠"还活着"。R263 的三次事故里有两次
            # 就发生在未核实的站上，所以这行必须显式给出未核实站名，
            # 否则读者会把「没告警」当成「都活着」。
            _names = _pp.get("unknown_sites") or ""
            lines.append(
                f"  ⚠️ provider 默认名仅部分核实：存活 {_cov}{_unk_note}"
                f"（{_names}，最近 {_ppt or '?'}）"
                f"——未核实≠存活，这些站的默认名可能已是僵尸名（R263）")
        elif not _zom_i:
            lines.append(
                f"  🩺 provider 默认名全部核实通过 {_cov}"
                f"（最近 {_ppt or '?'}）")
    elif rows is not None:
        # 有遥测但一条 provider_probe 都没有：探针从未成功落盘过。这与
        # 「核实通过」必须区分——沉默不是通过（R612：没人看=等于没有）。
        lines.append(
            "  ℹ️ provider 默认名探针: 本窗口无结论行（探针未接入或未落盘）"
            "——僵尸名检查处于失明状态")
    if rows is not None:
        f = funnel(rows)
        if f["attempted"]:
            rescued = f.get("failover_rescued") or 0
            lines.append(f"- 发布成功率: {f['delivered']}/{f['attempted']} 篇"
                         f"（{f['rate'] * 100:.1f}%，故事口径=同题多行去重，"
                         f"failover 救回 {rescued} 篇）")
    runs = s.get("runs") or {}
    if runs.get("n"):
        parts = [f"配额饱和 {runs['quota_blocked']} 轮", f"零候选 {runs['zero_candidates']} 轮"]
        if runs.get("active_hours_blocked"):
            parts.append(f"时段外 {runs['active_hours_blocked']} 轮")
        lines.append(f"- 运行摘要（{runs['n']} 轮）: {' / '.join(parts)}"
                     f"，累计候选 {runs['candidates']} → 发布 {runs['published']}"
                     + (f"（未处理 {runs['unprocessed']}）" if runs.get("unprocessed") else ""))
        # R10：降级可见——否则"标的表只剩兜底池"会伪装成"这些新闻没有标的"
        if runs.get("symbols_degraded"):
            lines.append(f"  ⚠️ 有效标的表最近一次降级: {runs['symbols_degraded']}"
                         f"（该轮新币新闻可能被误判为 no_token）")
        if runs.get("market_missing"):
            lines.append(f"  ⚠️ 盘面行情最近一次缺失标的: {runs['market_missing']}")
        # R113：配额释放估算直读——运营者不再需要查原始遥测
        if runs.get("next_slot_frees"):
            frees_min = runs.get("next_slot_frees_min")
            frees_str = f"（约 {frees_min} 分钟后）" if frees_min is not None else ""
            lines.append(f"  ⏳ 下一配额槽: {runs['next_slot_frees'][:16]} UTC{frees_str}")
        if runs.get("last_trending"):
            lines.append(f"  最近热搜 [{runs['last_trending']}]")
        if runs.get("trend_freq"):
            freq_str = " / ".join(f"{k}×{v}" for k, v in
                                  list(runs["trend_freq"].items())[:5])
            lines.append(f"  热搜持续度 {freq_str}")
        # R190：全网热点钩子供给——与币种热搜互补，看跨域注意力是否在喂稿
        if runs.get("last_hot_topics"):
            hits = runs.get("hot_topic_hits") or 0
            hooks = " | ".join(t[:28] for t in runs["last_hot_topics"].split(" | ")[:3])
            lines.append(f"  🌐 热点钩子（{hits} 轮有供给）: {hooks}")
        # R193：四路信号加权命中——供给≠命中
        bh = runs.get("boost_hits") or {}
        if any(bh.values()):
            # R611：分母按信号各自的数据轮数给，不用 boost_runs（四路上线时间不同，
            # 混用分母会让刚上线的信号显示成"几乎不命中"——R611 实测：浏览加权
            # +2-4 全部来自唯一 1 轮有该字段的 run_summary，与 208 轮无关）。
            _rs = runs.get("boost_runs_by_signal") or {}
            _ehr = _rs.get("eng", 0)
            _eng = (f" / 浏览加权 +{bh.get('eng_up', 0)} -{bh.get('eng_down', 0)}"
                    f"（{_ehr} 轮有数据）" if _ehr else
                    f" / 浏览加权 +{bh.get('eng_up', 0)} -{bh.get('eng_down', 0)}"
                    f"（⚠️ 0 轮有数据，上线后尚无观测）")
            lines.append(
                f"  📈 加权命中（活动/热搜/热点 {runs.get('boost_runs', 0)} 轮）: "
                f"活动 {bh.get('campaign', 0)} / 热搜 {bh.get('trend', 0)} / 热点 {bh.get('hot', 0)}{_eng}")
        if runs.get("last_campaign_off_pool"):
            lines.append(f"  🪙 活动币 off-pool: {runs['last_campaign_off_pool']}")
        # R196：饱和轮情报陈旧度——配额期实际在用多旧的情报
        if runs.get("avg_quota_intel_age_h") is not None:
            deg = runs.get("quota_intel_degraded") or 0
            flag = " ⚠️" if deg else ""
            lines.append(
                f"  🧊 饱和轮情报{flag}: 均值 {runs['avg_quota_intel_age_h']}h / "
                f"最长 {runs.get('max_quota_intel_age_h', 0)}h，降级 {deg} 轮")
        # R126：单轮耗时——逼近 20 分钟回调节奏时即为堆积预警
        if runs.get("avg_elapsed_sec") is not None:
            warn = " ⚠️逼近回调节奏" if runs.get("max_elapsed_sec", 0) > 1100 else ""
            lines.append(f"  ⏱️ 单轮耗时: 平均 {runs['avg_elapsed_sec']}s / "
                         f"最长 {runs['max_elapsed_sec']}s（回调节奏 1200s）{warn}")
        # R177：分段拆解——拟人 pacing 是总耗时大头，别误读成 LLM 变慢
        # R280：抓取/配图/发布同样是"总账差额归因"项，六段任一在场即出整行
        _seg_keys = ("avg_sleep_sec", "avg_llm_sec", "avg_intel_sec", "avg_fetch_sec",
                     "avg_image_sec", "avg_publish_sec")
        if any(runs.get(k) is not None for k in _seg_keys):
            parts = []
            n_total = runs.get("n_elapsed") or 0

            def _seg(label, val, n, extra=""):
                """分段均值 + 样本量。样本数少于总轮数时标注，避免与"单轮耗时"误比。"""
                if val is None:
                    return None
                tag = f"(样本 {n} 轮)" if n and n_total and n < n_total else ""
                return f"{label} {val}s{extra}{tag}"

            parts = [p for p in (
                _seg("抓取", runs.get("avg_fetch_sec"), runs.get("n_fetch_sec"),
                     f"(最长 {runs.get('max_fetch_sec', 0)}s)"),
                _seg("情报", runs.get("avg_intel_sec"), runs.get("n_intel_sec")),
                _seg("LLM", runs.get("avg_llm_sec"), runs.get("n_llm_sec")),
                _seg("配图", runs.get("avg_image_sec"), runs.get("n_image_sec"),
                     f"(最长 {runs.get('max_image_sec', 0)}s)"),
                _seg("发布", runs.get("avg_publish_sec"), runs.get("n_publish_sec"),
                     f"(最长 {runs.get('max_publish_sec', 0)}s)"),
                _seg("拟人间隔", runs.get("avg_sleep_sec"), runs.get("n_sleep_sec"),
                     f"(最长 {runs.get('max_sleep_sec', 0)}s)"),
            ) if p]
            # R184：追赶等待——不列则 run_elapsed 的差额无法归因（R177 漏项）
            if runs.get("avg_quota_wait_sec") is not None:
                parts.append(f"配额追赶等待 {runs['avg_quota_wait_sec']}s")
            lines.append(f"  耗时构成（均值）: {' · '.join(parts)}")
        if runs.get("skips"):
            lines.append(f"  跳过分布 {runs['skips']}")
        # R215：放行是限流决策的另一半——只看拦截会把"限流正常"误读成"疯狂拦截"
        # 阈值数字不在此硬编码（脚本独立运行不 import 主模块，双份事实源会漂移），
        # 阈值经 main.py --healthcheck 的「运行策略」行可见
        if runs.get("token_limit_bypass"):
            lines.append(f"  ⭕ 限流高影响放行 {runs['token_limit_bypass']} 次（热度达标绕过单币上限）")
        # R216：门槛校准行——拦截顶分贴门槛 = 真事件被吞需复评；稳居 20~26
        # 常规档 = 门槛健康。门槛数值不在此硬编码（R106：双份事实源会漂移），
        # 经 main.py --healthcheck 的「运行策略」行可见。
        calib = []
        if runs.get("token_limit_capped_top") is not None:
            calib.append(f"拦截顶分 {runs['token_limit_capped_top']}")
        if runs.get("token_limit_bypass_top") is not None:
            calib.append(f"放行顶分 {runs['token_limit_bypass_top']}")
        if calib:
            lines.append(f"  🎚️ 限流门槛校准: {' / '.join(calib)}（顶分贴门槛即复评）")
        # R220：每源入选率——0% 源是"扫描了却从未进入候选池"的死重候选，
        # 换源/撤源决策首次有可回查数据面（此前只进易失 Step Summary）。
        # R621：入选率带丢弃归因。此前只有 入选/扫描 两个数，把三种根因压成
        # 一个比率——离线复现证明「好源被跨源去重吃掉」与「坏源发旧闻」都能
        # 渲染成同一个低入选率，换源决策因此无依据。现在每个入选率数字后面
        # 附主导丢弃原因，且 ⚠️ 只在**源自身问题**（旧闻/重复推送）时点亮：
        # 被跨源去重吃掉不是源的过错（R621 判据：告警必须指向可处置的根因）。
        if runs.get("feed_yield"):
            parts = []
            # R621：判据是「窗口内有没有行携带 discarded_* 字段」（累加期置位
            # feed_yield_attr），而**不是**「有没有非零丢弃值」——一个真的一条
            # 都没被丢的源（25/25）也必须免于"归因暂无数据"的误报，那会把健康源
            # 说成不可信。
            _has_attr = bool(runs.get("feed_yield_attr"))
            for _fname, _v in sorted(runs["feed_yield"].items(),
                                     key=lambda x: (-x[1][1], -x[1][0])):
                _ents, _kept = _v[0], _v[1]
                # 归因取最大项；并列时不猜（留空），避免把"原因不明"渲染成有因
                _ds, _dc, _dd = (_v + [0, 0, 0])[2:5]
                _dom, _domn = max(((_ds, "旧闻"), (_dc, "重复推送"), (_dd, "跨源同题")),
                                  key=lambda t: t[0])
                # 并列时不猜（渲染成"原因不明"），避免把不确定的归因说成有因
                _tied = sum(1 for x in (_ds, _dc, _dd) if x == _dom) > 1
                _why = ""
                if _dom > 0:
                    _why = f"，主因{'并列' if _tied else _domn} {_dom}"
                # ⚠️ 的判据从「kept==0」收紧为「kept==0 且主因是源自身问题」。
                # R621：被跨源去重吃光是**别的源更优**的正常结果（离线复现里
                # 那个 0% 入选的源是好源），标⚠️ 会把运营引去撤掉健康源。
                flag = " ⚠️" if (_ents >= 20 and _kept == 0
                                  and (_dom > 0 and not _tied and _domn != "跨源同题")) else ""
                parts.append(f"{_fname} {_kept}/{_ents}{_why}{flag}")
            lines.append(f"  📡 源入选率(入选/扫描): {' · '.join(parts)}")
            if not _has_attr:
                # R621：归因全缺= 窗口内全是R621 上线前的历史行。此时**不能**
                # 读成"这些源一条都没被丢"，那会把未知显示成通过（R617纪律：
                # 沉默不是通过）。
                lines.append("     ℹ️ 丢弃归因暂无数据（本窗口遥测均为 R621 之前的历史行，"
                             "入选率低无法区分源劣化与跨源同题，暂勿据此换源）")
        # R613：源健康告警的新鲜度。三类都是全史累计，缺时间维度时陈迹与活警
        # 同貌（生产：注入截断最后发生距今 49h、空 feed 67h、硬故障 42h，面板
        # 仍与事发当日完全一样）。这里按"距最后一次发生多久"给新鲜度标签：
        # 24h 内=活警（⚠️，按原口径），超过则降为ℹ️ 陈迹——**不删数据**，
        # 只是不再占用告警视觉预算，让当期真问题浮出来。
        _al = runs.get("source_alarm_last") or {}
        _now = s.get("ts_max")

        def _fresh(key, hours=24):
            """返回 (最后发生 ts, 距今小时数 or None)。

            None = **无法判定新鲜度**（无时间戳/解析失败），调用方须按"活警"
            处理而不是降级。方向性刻意如此：把未知态报成陈迹会藏起一个可能正在
            发生的问题（告警漏判的代价远大于多报一条），与本项目"判负向漏判
            倾斜"的纪律一致。只有拿到**确切的陈旧证据**（距今 ≥24h）才降级。
            """
            _t = _al.get(key)
            if not _t or not _now:
                return (None, None)
            try:
                _a = datetime.fromisoformat(str(_t).replace("Z", "+00:00"))
                _b = datetime.fromisoformat(str(_now).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return (str(_t), None)
            if _a.tzinfo is None:
                _a = _a.replace(tzinfo=timezone.utc)
            if _b.tzinfo is None:
                _b = _b.replace(tzinfo=timezone.utc)
            _h = (_b - _a).total_seconds() / 3600.0
            return (str(_t), max(0.0, _h))

        def _ago(h):
            if h is None:
                return "时间未知"
            if h < 1:
                return f"{h * 60:.0f} 分钟前"
            if h < 48:
                return f"{h:.0f} 小时前"
            return f"{h / 24:.1f} 天前"

        # R334：注入截断（R273/R274 写侧）——R275 补了 Step Summary，metrics_report
        # 此前仍零消费。安全面：某源夹带 payload 时持久巡检页不得静默。
        if runs.get("injection_hits"):
            _srcs = runs.get("injection_feeds") or {}
            if hasattr(_srcs, "most_common"):
                _pairs = _srcs.most_common(5)
            else:
                _pairs = sorted(_srcs.items(), key=lambda x: -int(x[1] or 0))[:5]
            _detail = "、".join(f"{n} ×{v}" for n, v in _pairs) if _pairs else ""
            _note = f"（{_detail}）" if _detail else ""
            # R613：注入截断是安全面告警，**陈迹也必须留在面板上**（不能因
            # 降级而消失——那会变成"看不到就以为没发生过"），只把措辞从"请评估
            # 停放该源"降为"历史累计"并标注最后发生时间。
            _t, _h = _fresh("injection")
            _stale = _h is not None and _h >= 24
            _icon = "  ℹ️" if _stale else "  🚨"
            _act = ("（历史累计，最后发生 " + _ago(_h) + "，当期未复现）"
                    if _stale else "——请评估停放该源")
            lines.append(f"{_icon} 注入截断: {runs['injection_hits']} 条{_note}{_act}")
        # R335：源健康细化（R276 写侧）——空 feed / 抓取超时按源可见，
        # 与源入选率同属源治理面。零命中零噪音。
        _sh = []
        _sh_stale = False
        if runs.get("feeds_empty_sources"):
            _pairs = sorted(runs["feeds_empty_sources"].items(), key=lambda x: -x[1])[:4]
            _sh.append("空feed " + "、".join(f"{n} ×{v}" for n, v in _pairs))
            _t, _h = _fresh("feeds_empty_sources")
            if _h is not None and _h >= 24:
                _sh_stale = True
                _sh[-1] += f"（末次 {_ago(_h)}）"
        if runs.get("fetch_timeout_sources"):
            _pairs = sorted(runs["fetch_timeout_sources"].items(), key=lambda x: -x[1])[:4]
            _sh.append("超时 " + "、".join(f"{n} ×{v}" for n, v in _pairs))
            _t, _h = _fresh("fetch_timeout_sources")
            if _h is not None and _h >= 24:
                _sh_stale = True
                _sh[-1] += f"（末次 {_ago(_h)}）"
        if _sh:
            _head = "  ℹ️" if _sh_stale else "  ⚠️"
            _tail = "（陈迹，当期未见复现）" if _sh_stale else "——请评估换源/撤源"
            lines.append(f"{_head} 源健康异常: {' / '.join(_sh)}{_tail}")
        # R344：源硬故障/停放频率（feeds_failed/feeds_parked 写侧，报表此前零出口）——
        # 空feed/超时按源名已在上方，硬故障（网络/HTTP≠200/畸形XML）与停放的历史
        # 故障轮只有计数无源名，是仅剩的静默源健康信号。全窗零故障零停放不渲染。
        _fh = []
        _fh_stale = False
        if runs.get("feed_fail_runs"):
            _fh.append(f"硬故障 {runs['feed_fail_runs']} 轮/共 {runs.get('feed_fail_total', 0)} 源次"
                       f"（峰 {runs.get('feed_fail_peak', 0)}）")
            _t, _h = _fresh("feeds_failed")
            if _h is not None and _h >= 24:
                _fh_stale = True
                _fh[-1] += f"（末次 {_ago(_h)}）"
        if runs.get("feed_park_runs"):
            _fh.append(f"停放 {runs['feed_park_runs']} 轮/共 {runs.get('feed_park_total', 0)} 源次"
                       f"（峰 {runs.get('feed_park_peak', 0)}）")
        if _fh:
            _head = "  ℹ️" if _fh_stale else "  🩺"
            _tail = ("（陈迹，当期未见复现）" if _fh_stale
                     else "——失败被候选健康表象掩盖，请查源名")
            lines.append(f"{_head} 源故障/停放: {' / '.join(_fh)}{_tail}")
        # R625：健康源数——**三态里唯一没有出口的一态**，与上方故障行同框。
        #
        # 为什么必须与故障同框而不是单独一行：源治理的问题是"**还剩几个能用**"，
        # 只报故障数读者要自己用「源总数 − 故障 − 停放 − 空」去心算，而源总数
        # 又不在面板上（池内 9 源是代码常量）。生产实测 feeds_ok 均 8.94/最大 9，
        # 也就是说**这9 源几乎轮轮全健康**——而这个结论目前完全不可见，
        # 面板只能回答"有没有坏源"。
        #
        # 分母是「抓取轮」而非全部轮：feeds_ok 只在非配额饱和轮出现
        # （生产 270/276 轮，饱和轮 sys.exit 在抓取之前）。混算分母会把
        # 「0.14 个健康源/轮」这种无意义数字渲染出来（R621分母纪律）。
        if runs.get("feed_ok_runs"):
            _ok_runs = runs["feed_ok_runs"]
            _ok_avg = runs.get("feed_ok_total", 0) / _ok_runs
            lines.append(f"  🟢 源健康: {_ok_runs} 个抓取轮平均 {_ok_avg:.1f} 个源正常"
                         f"（最少 {runs.get('feed_ok_min')} / 最多 {runs.get('feed_ok_max')}）"
                         + (f" · 同窗硬故障 {runs.get('feed_fail_runs', 0)} 轮"
                            if runs.get("feed_fail_runs") else " · 同窗零硬故障"))
        # R342：扫描漏斗（R276 写侧，报表此前零出口）——去重/缓存/陈旧趋势，
        # 单轮峰值抓尖刺（near_dup 抬升=去重吞事件 / cached 跳涨=缓存失效 /
        # stale 峰值=源劣化）；全零不渲染（零噪音，沿用源健康惯例）。
        _ff = runs.get("fetch_funnel") or {}
        _ffp = []
        for _fk, _lbl in (("fetched", "抓取"), ("near_dup", "近重"),
                          ("cached", "缓存"), ("stale", "陈旧")):
            _tm = _ff.get(_fk)
            if isinstance(_tm, (list, tuple)) and len(_tm) == 2 and _tm[0] > 0:
                _ffp.append(f"{_lbl} {_tm[0]}(峰{_tm[1]})")
        if _ffp:
            lines.append(f"  🔻 扫描漏斗: {' / '.join(_ffp)}")
    if rows is not None:
        q = quality_scan(rows)
        if q["scanned"]:
            violations = q["fng_anchor"] + q["banned_device"] + q["ai_flavor"]
            status = "全部通过" if not violations else f"{violations} 处命中"
            lines.append(f"- 内容合规巡检（最近 {q['scanned']} 篇回执文本）: {status}"
                         + (f" {q['offenders']}" if q["offenders"] else ""))
            # R162：FNG 禁令咬合度量——armed 篇中避开 vs 违反（历史帖无状态字段不进分母）
            if q["fng_ban_armed"]:
                flag = " ⚠️" if q["fng_violation"] else ""
                lines.append(f"  🚦 FNG 禁令咬合{flag}: 武装 {q['fng_ban_armed']} 篇中避开 "
                             f"{q['fng_avoided']} / 违反 {q['fng_violation']}")
            # R603：操纵归因叙事占比——R596 压「利好不涨=有人出货」的腔调，逐字「我猜」
            # 已消失但语义框架仍在（靠多样化词汇绕过前缀指纹雷达）。按帖占比跨窗追踪：
            # 持续下行=R596 起效；长期高位（>40%）才是加固 R596 的信号，避免 n 小时过拟合。
            if q["manip_frame"]:
                _mf = q["manip_frame"]
                _pct = 100 * _mf / q["scanned"]
            # R610：判定「修复是否生效」看**近半**（修复后新帖）而非整窗。
            # 整窗在修复刚上线时 mostly 是修复前旧稿，会把已生效的加固报成无效
            # （R604 实录：整窗 50% 假警报，近半 0/10 真水位）。近半明显低于
            # 远半即"已生效、待窗口滚出旧稿"；两半都高才是真的无效，才值得再动手。
            if q["manip_frame"]:
                _mf = q["manip_frame"]
                _pct = 100 * _mf / q["scanned"]
                _r = q.get("manip_frame_recent", 0)
                _o = q.get("manip_frame_older", 0)
                _rn = (q["scanned"] + 1) // 2
                _on = q["scanned"] - _rn
                if _rn >= 3 and _on >= 3:
                    _rpct = 100 * _r / _rn
                    _opct = 100 * _o / _on
                    # 判据以**近/远对比**为主、绝对值为辅。理由：发布速率只有
                    # 4~6 篇/天，一次加固上线当天近半必然混有加固前旧稿，此时
                    # 近半绝对值高只说明"样本还没滚干净"，不说明修复无效。
                    # 只有"近半 ≥ 远半"（没降）才值得再动手；降了就是在生效，
                    # 等窗口滚干净即可。绝不因为近半绝对值高就催"再加固"——
                    # 那会让下一轮去改已经修好的东西（R604 就是这么被误判的）。
                    if _rpct >= 40 and _rpct >= _opct:
                        _warn = " ⚠️（近半未低于远半，加固可能未生效，待更多新帖确认）"
                    elif _r == 0:
                        _warn = "（近半已清零↓，加固生效，待旧帖滚出窗口）"
                    else:
                        _warn = "（近半 < 远半，加固生效中↓）"
                    _lo, _hi = q.get("recent_span") or (None, None)
                    _span = f" · 近半跨度 {_lo}→{_hi}" if _lo else ""
                    lines.append(f"  🎭 操纵归因叙事: 近半 {_r}/{_rn}（{_rpct:.0f}%）· "
                                 f"远半对照 {_o}/{_on}（{_opct:.0f}%）· "
                                 f"整窗 {_mf}/{q['scanned']}（{_pct:.0f}%）{_warn}{_span}")
                else:
                    _warn = " ⚠️（叙事指纹，样本不足需继续观察）" if _pct >= 40 else ""
                    lines.append(f"  🎭 操纵归因叙事: {_mf}/{q['scanned']} 篇（{_pct:.0f}%）{_warn}")
            # R605：句长 burstiness（节奏方差）——整合自 textpulse 2026 6万+文本研究：
            # AI 文本句长偏均匀（CV 小），人类起伏大（人≈0.449/AI≈0.376）。我们 prompt
            # 的「长短句交错」此前无度量，这里给中位 CV + 偏平尾；研究自陈个体判决不可靠，
            # 故只观测不设门。中位 ≥0.449 说明节奏比人类基线还活。
            _cvs = q.get("burstiness_cvs") or []
            if len(_cvs) >= 3:
                _cvs_sorted = sorted(_cvs)
                _med = _cvs_sorted[len(_cvs_sorted) // 2]
                _flat = sum(1 for c in _cvs if c < 0.35)
                _tag = "（节奏健康，优于人类基线0.449）" if _med >= 0.449 else (
                    "（偏平，接近 AI 基线0.376，建议强化长短句交错）" if _med < 0.40 else "")
                lines.append(f"  🎵 句长节奏 CV 中位 {_med:.2f}（{len(_cvs)} 篇；人≈0.45/AI≈0.38）"
                             f" · 偏平 {_flat} 篇{_tag}")
            # R607：「利好不涨」描述复读——加密新闻最常见场景被收敛到固定描述句
            # （连个像样的反弹都没有/盘面不买账/连个水花都没溅）。句中短语、前缀雷达
            # 看不见。只追踪：≥40% 才提示（且可能是话题驱动而非风格退化，需读样本甄别）。
            if q.get("flat_desc"):
                _fd = q["flat_desc"]
                _fp = 100 * _fd / q["scanned"]
                # R610：同操纵归因——近半判当期水位、远半作对照。
                # 这个指标尤其需要：它的 33% 上升本就可能是"近期新闻恰好多为
                # 利好不涨"的话题假象，近/远对照能把话题驱动与风格收敛分开。
                _fr = q.get("flat_desc_recent", 0)
                _fo = q.get("flat_desc_older", 0)
                _rn = (q["scanned"] + 1) // 2
                _on = q["scanned"] - _rn
                if _rn >= 3 and _on >= 3:
                    _frpct = 100 * _fr / _rn
                    _fopct = 100 * _fo / _on
                    # 同 R610：近/远对比优先。近半 < 远半 = 话题驱动（这批新闻
                    # 恰好多是利好不涨，描述本就该多），非风格退化；只有近半没降
                    # 甚至升高，才需要读样本甄别是否该给"换着说法"的技法指导。
                    if _frpct >= 40 and _frpct >= _fopct:
                        _fw = " ⚠️（近半未低于远半，读样本辨别风格退化还是话题驱动）"
                    elif _frpct < _fopct:
                        _fw = "（近半 < 远半，话题驱动特征，无需改 prompt）"
                    else:
                        _fw = "（近/远持平，需更多样本）"
                    lines.append(f"  📉 利好不涨描述复读: 近半 {_fr}/{_rn}（{_frpct:.0f}%）· "
                                 f"远半对照 {_fo}/{_on}（{_fopct:.0f}%）· "
                                 f"整窗 {_fd}/{q['scanned']}（{_fp:.0f}%）{_fw}")
                else:
                    _fw = (" ⚠️（描述收敛，读样本辨别是风格退化还是近期多利好不涨）"
                           if _fp >= 40 else "")
                    lines.append(f"  📉 利好不涨描述复读: {_fd}/{q['scanned']} 篇（{_fp:.0f}%）{_fw}")
        # R289：FNG 三件套收口——滞回驱动量直方图 + 武装未剥离一致性告警。
        # hook_count 是近窗引入次数（武装条件 ≥2，故 1 = 距武装一步之遥的压力面）；
        # armed 但 market_stripped=False = R101 互补剥离疑似失效（禁令与盘面行
        # 同时在场=自相矛盾指令），生产现况 58/58 全部一致剥离。
        if s["fng_evaluated"]:
            _hist = " · ".join(f"hook={k} ×{v}"
                               for k, v in sorted(s["fng_hook_hist"].items()))
            lines.append(f"  🔥 FNG 锚点压力（{s['fng_evaluated']} 篇）: {_hist}")
        if s["fng_armed_not_stripped"]:
            lines.append(f"  ⚠️ FNG 武装但盘面未剥离 {s['fng_armed_not_stripped']} 篇"
                         f"——R101 互补剥离疑似失效，需排查")
        # R124：开场指纹雷达——共享前缀 ≥3/10 即预警（禁令仍走 main.py 机制，
        # 这里只负责让新指纹在成形期可见，不再依赖人工抽样发现）
        fp = opener_fingerprint(rows)
        if fp["alerts"]:
            detail = "、".join(f"“{p}…”×{c}" for p, c in fp["alerts"].items())
            lines.append(f"  🔭 开场指纹预警（近 {fp['scanned']} 帖开场共享前缀）: {detail}")
        # R600：分析段指纹雷达——开场雷达只看第一段，漏了第 2 段「我猜这波是主力…」
        # 这类分析段开场的复读（R596 实录 47% dashboard 全程没报，靠人工读 preview
        # 才发现）。同口径扫第二正文段开场，把人工发现过程继续产品化。
        bfp = body_fingerprint(rows)
        if bfp["alerts"]:
            bdetail = "、".join(f"“{p}…”×{c}" for p, c in bfp["alerts"].items())
            lines.append(f"  🔭 分析段指纹预警（近 {bfp['scanned']} 帖第二段共享前缀）: {bdetail}")
    n_pub = sum(s["by_provider"].values())
    if n_pub:
        lines.append(f"- 投递 {n_pub} 篇：分时 {_top(s['by_hour'])} / 来源 {_top(s['by_source'])}")
        # R343：分周节奏（自然周序，缺勤日不渲染）——周末/工作日发布分布，配合
        # 时段均浏览回答「哪天发」；无投递周维数据时整行静默（零噪音）
        if s.get("by_weekday"):
            _wd = " ".join(f"{_WEEKDAY_NAMES[i]}×{s['by_weekday'][i]}"
                           for i in range(7) if s["by_weekday"].get(i))
            if _wd:
                lines.append(f"  分周: {_wd}")
        lines.append(f"  模型 {_top(s['by_provider'])} / 首标的 {_top(s['by_token'])} / 配图率 "
                     f"{s['images']}/{n_pub}")
        # R222：内容新鲜度漂移监控——生产基线中位 ~1.9h / P75 ~3.1h（109 篇全史
        # 79%<3h、零篇 ≥24h）；中位数抬升即查各源日期解析（fail-open 风险面）
        if s.get("pub_ages"):
            _ag = sorted(s["pub_ages"])
            lines.append(f"  ⏱️ 内容新鲜度: 中位 {_ag[len(_ag)//2]}h · P75 "
                         f"{_ag[min(len(_ag)*3//4, len(_ag)-1)]}h · 最老 {_ag[-1]}h"
                         f"（样本 {len(_ag)} 篇）")
        # R123：全文零挂件帖 = Write2Earn 生命线失守（保底机制被绕过）的直接信号
        if s.get("zero_widget_posts"):
            lines.append(f"  ⚠️ 全文零有效挂件 {s['zero_widget_posts']}/{n_pub} 篇——保底机制被绕过，需排查")
        # R610：稳定币-only 零挂件显性列出（R316/R586 契约的合规结果，非失守）。
        # 与上面告警分开：让"告警消失"可归因到口径修正，而不是让人怀疑观测被关掉。
        if s.get("zero_widget_stablecoin_only"):
            lines.append(f"  ℹ️ 稳定币-only 零挂件 {s['zero_widget_stablecoin_only']} 篇"
                         f"（R316「稳定币不做挂件」契约 + R586 选稿跳过，不计失守）")
        # R125：零标签帖 = #Write2Earn 返佣归因丢失
        if s.get("zero_tag_posts"):
            lines.append(f"  ⚠️ 全文零标签 {s['zero_tag_posts']}/{n_pub} 篇——返佣归因丢失，需排查")
        # R284：活动标签覆盖——情报新鲜却零活动标签 = _inject_campaign_tag 疑似回归
        if s.get("campaign_tag_zero_fresh"):
            lines.append(f"  ⚠️ 情报新鲜但无活动标签 {s['campaign_tag_zero_fresh']}/"
                         f"{s['campaign_tag_evaluated']} 篇——创作激励活动标签未注入，需排查")
        # R291：实际注入的活动标签分布（有显式字段的行才统计）
        if s.get("campaign_tags"):
            _cts = " · ".join(f"{k} ×{v}" for k, v in s["campaign_tags"].most_common(3))
            lines.append(f"  🏷️ 活动标签注入: {_cts}")
        # R612：浏览加权表覆盖审计——R608 到底在给哪些币 ±5，谁被挡在表外。
        # 关键不是"表里有谁"，而是**表外那批高浏览币**：它们的样本量 n 小，
        # 而 n 之所以小恰恰因为"我们发得少"——于是"发得多→n 大→留在表内
        # → 低浏览币被持续重排到后置 → 继续发得少"的自我强化回路。
        # 生产实锤：BNB 中位浏览 247（全场最高之一）因 n=1 被挡在表外，
        # 而中位 36 的 DOGE 因 n=3 差一点进表。数据在，机制在，无人可见。
        _eb = s.get("eng_boost")
        if _eb:
            _mn = _eb["min_n"]
            _fmt2 = lambda items: " · ".join(
                f"{i['token']}(浏览{i['median_views']},n={i['n']},发{i['published']})"
                for i in items) or "无"
            lines.append(f"  ⚖️ 浏览加权表（min_n={_mn}，{len(_eb['in_table'])} 币在表内"
                         f"参与 ±5 排序）: {_fmt2(_eb['in_table'][:8])}")
            if _eb["out_table"]:
                # 反馈回路告警：表外币的浏览中位显著高于表内中位 = 加权方向
                # 与数据背离。给阈值而非主观判断：表外最高浏览 > 表内最低浏览。
                _in_min = min((i["median_views"] for i in _eb["in_table"]),
                              default=None)
                _out_max = max((i["median_views"] for i in _eb["out_table"]),
                               default=None)
                _loop = (_in_min is not None and _out_max is not None
                         and _out_max > _in_min)
                _tag = (" ⚠️ 表外高触达币被min_n 挡在加减分之外"
                        if _loop else "")
                lines.append(f"  🚫 表外（样本不足，加权不生效）: "
                             f"{_fmt2(_eb['out_table'][:8])}{_tag}")
                if _loop:
                    lines.append(f"     ↳ 自我强化回路：低浏览币发得多→n 大→留在表内"
                                 f"→ 被持续重排到后置 → 继续发得少；"
                                 f"高浏览币发得少→n 小→被挡表外 → 永远得不到加分。"
                                 f"需人工决定是否放宽 min_n 或改用其他样本来源")
        # R285：浏览/互动面板——有 join 上的样本才渲染（无 stats 时整块不出现）。
        # 三维均浏览是"哪类帖有流量"的第一手答案：时段/体裁/来源各自的样本量
        # 一并给出，样本 <3 的桶只展示不解读（避免小样本误判）。
        #
        # R628：**分母必须显式**——原文案「59 篇有记录」读起来像"共 59 篇"，
        # 而生产实测 274 篇已发布、内容库只 59 篇（**22%**）。两者混同会让
        # "均浏览 137"被读成全站水平，实际只是**头部 1/5 帖**的水平。
        # 与 R621/R624 同判据：指标的覆盖面往往比指标本身更重要。
        #
        # 采集机制（已核实 content_stats.jsonl）：**每日 04:00Z 一次性快照**、
        # 累积式覆盖历史。所以采集日之前的帖永远没有浏览数据（**不是缺口**），
        # 采集日当天的帖要等次日快照（当天显示偏低）——两者都不是数据缺失，
        # 但**读者无法自行区分"没采到"与"没数据"**，故把分母直接摆出来。
        if s.get("stats_posts"):
            _v = s["stats_views"]
            _lk = s["stats_likes"]
            _cm = s["stats_comments"]
            _fmt = lambda xs: f"{sum(xs)/len(xs):.0f}" if xs else "-"
            _den = s.get("delivered_joinable_posts") or 0
            _pct = f"（{s['stats_posts'] / _den * 100:.0f}%）" if _den else ""
            lines.append(f"  📊 内容数据（{s['stats_posts']}"
                         + (f"/{_den} 篇已发布帖有浏览数据{_pct}" if _den else " 篇有记录")
                         + "）: "
                         f"均浏览 {_fmt(_v)} · 均点赞 {_fmt(_lk)} · 均评论 {_fmt(_cm)}"
                         f"（总浏览 {s['stats_views_total']}）"
                         + (f"—— 均值为该 {_pct.strip('（）')} 子集水平、**非全站**"
                            if _den and s["stats_posts"] < _den else ""))
            _hb = _bucket_line(s["stats_by_hourbucket"])
            if _hb:
                lines.append(f"    时段均浏览: {_hb}")
            _gg = _bucket_line(s["stats_by_genre"])
            if _gg:
                lines.append(f"    体裁均浏览: {_gg}")
            _sc = _bucket_line(s["stats_by_source"], top=3)
            if _sc:
                lines.append(f"    来源均浏览: {_sc}")
            # R602：文风维度 × 浏览——把 R521/R288/R130/R592 轮换的开场/人设/结尾/
            # 实操角度各自的真实浏览量摆出来，回答「哪种套路真能带来流量」，让文风
            # 旋钮从「凭最佳实践猜」转向「按 engagement 调」。无样本的维度整行静默。
            for _label, _key in (("开场钩子均浏览", "stats_by_hook"),
                                  ("人设均浏览", "stats_by_persona"),
                                  ("结尾套路均浏览", "stats_by_ending"),
                                  ("实操角度均浏览", "stats_by_cta")):
                _bl = _bucket_line(s[_key])
                if _bl:
                    lines.append(f"    {_label}: {_bl}")
        # R286：长文标题眼钩基线（有长文标题才渲染）+ 禁用领词告警
        if s.get("article_titles"):
            _n = len(s["article_titles"])
            _avg = sum(len(t) for t in s["article_titles"]) / _n
            _hooks = " · ".join(f"{k} {v}/{_n}"
                                for k, v in s["title_hooks"].most_common())
            lines.append(f"  📐 长文标题（{_n} 篇 · 均长 {_avg:.0f} 字）: {_hooks}")
        if s.get("title_leadin_hits"):
            _hits = s["title_leadin_hits"]
            _pref = "、".join(sorted({h[1][:2] for h in _hits}))
            _dates = sorted({h[0] for h in _hits if h[0]})
            _date_note = f"，日期 {'/'.join(_dates)}" if _dates else ""
            # R296 起 TITLE 指令已列全禁令；只有 R296 后的新命中才提示复查
            if _dates and all(d < _TITLE_BAN_DEPLOYED for d in _dates):
                _guard = ("历史残留（命中日均早于 R296 标题禁令），预防侧已闭环"
                          "——勿再当开放缺口追")
            else:
                _guard = "含 R296 后命中，标题禁令预防侧需复查"
            lines.append(f"  ⚠️ 长文标题命中禁用领词 {len(_hits)}/"
                         f"{len(s['article_titles'])} 篇（{_pref}…{_date_note}）——{_guard}")
        # R130：结尾套路分布（验证 ShuffleBag 轮换均匀性；旧 schema 无字段则不渲染）
        if s["by_ending"]:
            ending_str = " · ".join(f"{k} ×{v}" for k, v in s["by_ending"].most_common(5))
            lines.append(f"  结尾套路分布: {ending_str}")
        if s["by_trade_cta_style"]:
            cta_str = " · ".join(f"{k} ×{v}" for k, v in s["by_trade_cta_style"].most_common(5))
            lines.append(f"  实操角度分布: {cta_str}")
        # R288：人设分布 + 近期集中度告警（R287 跨运行预热后的效果监测面）
        if s["by_persona"]:
            persona_str = " · ".join(f"{k} ×{v}" for k, v in s["by_persona"].most_common())
            lines.append(f"  🎭 人设分布: {persona_str}")
            _win = s["persona_seq"][-12:]
            if len(_win) >= 6:
                _top_p, _cnt_p = collections.Counter(_win).most_common(1)[0]
                if _cnt_p * 2 >= len(_win):
                    lines.append(f"  ⚠️ 近 {len(_win)} 帖人设集中: {_top_p} {_cnt_p}/{len(_win)}"
                                 f"——R287 跨运行预热后仍扎堆需排查")
        # R288：热度分分布（门槛校准基线；≥30 = 单币限流高影响放行档）
        if s["impact_scores"]:
            _imps = sorted(s["impact_scores"])
            _med = _imps[len(_imps) // 2]
            _p90 = _imps[int(len(_imps) * 0.9)]
            _hi = sum(1 for i in _imps if i >= 30)
            lines.append(f"  🔥 热度分: 中位 {_med} · P90 {_p90} · "
                         f"≥30 放行档 {_hi}/{len(_imps)} 篇")
        # R292：篇幅分布——prompt 宣称"140~200 字"（短讯，R294 对齐后）/500~800（长文）而
        # 质量门实际只卡 60~1200，两者差 20 倍；按体裁分桶报中位/P90/区间命中率，
        # 回答"模型到底写多长"（有字段的行才统计，旧 schema 行不渲染）
        for _genre, _lo, _hi_b in (("短讯", 140, 200), ("长文", 500, 800)):
            _vals = sorted(s["cjk_by_genre"].get(_genre, []))
            if _vals:
                _m = _vals[len(_vals) // 2]
                _p = _vals[int(len(_vals) * 0.9)]
                _in = sum(1 for v in _vals if _lo <= v <= _hi_b)
                lines.append(f"  📏 {_genre}篇幅（{len(_vals)} 篇）: 中位 {_m} 字 · "
                             f"P90 {_p} · {_lo}~{_hi_b} 区间内 {_in}/{len(_vals)}")
        # R293：首段钩子覆盖率（prompt"首两行定生死"条款的度量面；与标题眼钩
        # 同口径，零钩子率=信息流前两行没有点开理由）
        if s["opener_evaluated"]:
            _n = s["opener_evaluated"]
            _hk = " · ".join(f"{k} {v}/{_n}"
                             for k, v in s["opener_hooks"].most_common())
            lines.append(f"  🪝 首段钩子（{_n} 篇）: {_hk}")
        if s["image_tiers"]:
            lines.append(f"  配图层级 {dict(s['image_tiers'])}")
        # R173：过期情报注入可见化（有字段的帖才进分母，历史行不混入）
        n_intel_marked = s["intel_degraded_posts"] + s["intel_fresh_posts"]
        if n_intel_marked:
            flag = " ⚠️" if s["intel_degraded_posts"] else ""
            age_note = ""
            ages = s.get("intel_age_hours") or []
            if ages:
                age_note = f"（陈旧均值 {round(sum(ages)/len(ages), 1)}h / 最长 {round(max(ages), 1)}h）"
            lines.append(
                f"  💡 情报注入{flag}: 新鲜 {s['intel_fresh_posts']} / "
                f"降级(过期) {s['intel_degraded_posts']}{age_note}"
                f"（共 {n_intel_marked} 篇带标记）")
    # R175：退避跳过独立于投递块（可能 0 篇投递时仍有退避轮次）
    if s.get("intel_cooldown_skips"):
        lines.append(
            f"  ⏭️ 情报刷新被失败退避跳过 {s['intel_cooldown_skips']} 轮"
            f"（2h 冷却期内沿用旧情报）")
    # R199：最近成功刷新时刻——对照 12h 过期窗，判断饱和轮是否在喂陈旧情报
    if s.get("last_intel_refresh_ts"):
        lines.append(f"  🔄 最近情报刷新: {str(s['last_intel_refresh_ts'])[:19]}")
    # R333：R127 写侧的 stale_date_refs 此前零出口——有残留时必须可见
    if s.get("stale_date_refs_refreshes"):
        lines.append(
            f"  ⚠️ 情报 guidance 残留过期日期: {s['stale_date_refs_refreshes']} 次刷新命中"
            f"（共 {s['stale_date_refs_hits']} 处，单次最多 {s['stale_date_refs_max']}）"
            f"——模型未完全遵守剔除指令")
    n_rej = sum(s["reject_by_stage"].values())
    if n_rej:
        lines.append(f"- 拦截 {n_rej} 次：阶段 {_top(s['reject_by_stage'])} / 模型 {_top(s['reject_by_provider'])}")
        if s["reject_reasons"]:
            lines.append(f"  高频原因 {_top(s['reject_reasons'], 5)}")
        _perm_live, _perm_retired = _format_permanent_failures(
            s.get("permanent_failures"), s.get("permanent_failures_last"),
            s.get("provider_last_success"))
        # R616：活警与退役簇分两行。退役簇**不删**（安全面：曾发生过永久失败这件事
        # 是事实），但降为ℹ️ 且不再挂"需人工处置"——那会把人引去改一个早已
        # 自愈的配置，浪费的注意力比噪音更贵。
        if _perm_live:
            lines.append(f"  💀 永久失败(24h冷却): {_perm_live}"
                         f"（需人工处置：余额耗尽→充值 / 模型下架→改配置）")
        if _perm_retired:
            lines.append(f"  ℹ️ 永久失败退役簇: {_perm_retired}"
                         f"（无需处置）")
        if s.get("reject_previews"):
            lines.append("  拒稿快照（最近）:")
            for item in s["reject_previews"][-3:]:
                lines.append(
                    f"    [{item.get('stage')}/{item.get('provider')}] "
                    f"finish={item.get('finish_reason') or '?'} "
                    f"{item.get('preview', '')}")
    # R620：通道质量产出——按通过率排序（**不按调用量**）。
    # 排序指标选错是这行的全部理由：openrouter 全史 109 次调用看着"很常用"，
    # 通过率却只有 8%；stepfun-flash 只有 31 次，通过率 55%。按量排会让人
    # 把主力通道当废物，按通过率排才能看出"谁在真的产出"。
    _pq = s.get("provider_quality") or {}
    if _pq:
        _qrows = []
        for _p, _q in _pq.items():
            _tot = _q["ok"] + _q["rej"] + _q["fail"]
            if _tot <= 0:
                continue
            _qrows.append((_p, _q["ok"], _q["rej"], _q["fail"], _tot,
                           _q["ok"] / _tot))
        if _qrows:
            _qrows.sort(key=lambda t: -t[5])
            lines.append(
                "- 通道质量产出（按通过率排序，非调用量）: ")
            for _p, _ok, _rj, _fl, _tot, _rate in _qrows:
                # 低通过率且样本足= 真信号；样本小则标注"样本少"避免过度解读
                _tag = ""
                if _tot < 5:
                    _tag = "（样本少，勿过度解读）"
                elif _rate < 0.25:
                    _tag = " ⚠️ 大量调用但几乎不产出，考虑降权或换默认模型"
                lines.append(
                    f"    {_p}:通过率 {_rate * 100:.0f}% "
                    f"（成功 {_ok} / 拒 {_rj} / 失败 {_fl}，共 {_tot} 次）{_tag}")
    if s["latency_by_provider"]:
        lines.append(f"- 平均延迟(s) {dict(sorted(s['latency_by_provider'].items()))}")
        _disp = _provider_dispatch_order(
            s.get("by_provider"), s.get("reject_by_provider"), s["latency_by_provider"])
        if _disp:
            _segs = [
                f"{p} 发{pub}/拒{rej}" + (f"·{lat}s" if lat is not None else "·延迟?")
                for p, pub, rej, lat in _disp
            ]
            lines.append("- 通道位次（延迟↑=failover 调用顺序，靠前先试；慢而稳的源"
                         "天然靠后·份额小≠闲置）: " + " › ".join(_segs))
    # R623：池内通道的实际覆盖——**「有key」不等于「会上场」**。
    #
    # 这条补的是 R617 探针的 L4 缺口：探针验的是"默认模型名还活着吗"（端点/
    # 目录可达性），但**完全没有回答"这条通道实际会不会被排序选中"**。生产
    # 实测两者可以彻底脱节：池内 12 站里8 站近30 天零遥测，而它们并非故障、
    # 非熔断、key 也在 workflow 里注入了——只是 `STEPFUN_PRIORITY` 把stepfun
    # 系抬到免费池之上，且 stepfun 系次席成功，于是后位通道永不上场。
    #
    # 为什么必须显式渲染：前面所有通道行都是**「有数据的那些通道」**的画像，
    # 缺失的通道在面板上根本不出现——「全绿」与「7/12 通道从未验证」读起来一样。
    # 呼应 R612「程序在用≠ 人在看」：池在代码里定义了不等于链会用到。
    _pool = _provider_pool_from_main()
    _att = s.get("provider_attempts") or {}
    if _pool:
        _seen = [k for k in _pool if _att.get("Preset-" + k)]
        _never = [k for k in _pool if not _att.get("Preset-" + k)]
        # 分母必须显式：只显"从未被尝试 N 条"会被读成"还有几条在用"之外的孤立
        # 事实，而读者真正要问的是占比——池里到底有多少是未经验证的。
        lines.append(f"- 池内通道覆盖: 有遥测 {len(_seen)}/{len(_pool)}"
                     + (f"（已验证: {' · '.join(_seen)}）" if _seen else ""))
        if _never:
            # ⚠️ 的判据是**窗口内零尝试**——这类通道的正确性从未被任何生产数据
            # 检验，前序通道一挂就会顶上去裸奔（R618 纪律：从未被尝试不是安全信号）。
            #
            # 措辞纪律（R620反面）：这里**只陈述遥测能证实的事实**。
            # 此前草稿写了"key 已注入 workflow 且非熔断状态"——这两句metrics.jsonl
            # 都无法证实（key 在不在 workflow 里、断路器状态如何，都在别处），
            # 而报表写下一句自己证实不了的话，读者会当成已核实的结论。
            # **"是不是没配 key / 有没有被熔断"是处置动作的前提，须由人去对账**
            # （`gh secret list` + campaign_intel 的 _llm_breaker）。
            lines.append(f"  ⚠️ 从未被尝试 {len(_never)}/{len(_pool)}: "
                         f"{' · '.join(_never)}"
                         "（本窗口零遥测：正确性未被生产验证，"
                         "前序通道故障时属盲区；请对账是否缺 key / 被熔断）")
    elif _pool == [] and (s.get("provider_attempts") or s.get("by_provider")
                          or s.get("reject_by_provider")):
        # 空池只有一种成因：main.py 解析失败（语法错误 / 找不到 extra_keys）。
        # 此时**必须响亮报失明**——静默跳过这行等于让"代码损坏"伪装成"全绿"，
        # 而 R617 探针的判据是"语法错误要响亮失败，空池会让全通过"（同型）。
        #
        # 守卫用 provider_attempts / reject_by_provider，**不能用 by_provider**：
        # 后者只统计投递成功，纯拒稿轮次下它是空 Counter，于是"有通道活动但没
        # 成功"这个最需要报失明的场景反而静默了——生产实测拒稿占拒单遥测的主体
        # （quality/numbers/transport 全走llm_rejected），一旦某轮全是拒稿，
        # 空池就会被吞掉。这与 R617「沉默不是通过」同型，是**覆盖不到的一种形态**。
        lines.append("  ⚠️ 池内通道覆盖未知（AST 解析 main.py 的 extra_keys 失败）"
                     "——面板对'哪些通道从未被尝试'已失明，不要读作全部覆盖")
    if s["tokens_by_provider"]:
        lines.append(f"- token 消耗 {dict(sorted(s['tokens_by_provider'].items()))}")
    if s["errors"]:
        # R624：错误串全史计数补新鲜度分级。
        #
        # 为什么必须分级（生产实测四类几乎全是陈迹）：
        #   404 僵尸名 20 次，末次 09-08（24 天前）—— R263 早已闭环
        #   超时23 次，末次 09-21（11 天前）
        #   空内容     24 次，末次 09-22（10 天前）
        #   b.ai 余额  11 次，末次 09-29（3.2 天前）—— **仍活**
        # 原渲染把它们并排成 `错误串 [(404, 20), (超时, 23), ...]`，四个数字
        # 读起来像"当前系统有 78 个问题"，而**陈迹与活警同貌**。
        #
        # 分级判据沿用 R613/R614 的**按根因性质分级**（不是一刀切降级）：
        #   - 陈迹（末次 > 24h）→ ℹ️，**不删数据**（安全面告警：发生过是事实，
        #     消失即"看不到就以为没发生"），只是不再占用告警视觉预算；
        #   - 活警（≤24h）→ ⚠️ 保留；
        #   - **未知（无 ts / 解析失败）→ 按活警处理，不降级**。判据同_fresh：
        #     把未知态报成陈迹会藏起一个可能正在发生的问题。
        _e_now = s.get("ts_max")
        _e_last = s.get("error_last") or {}
        _live, _stale, _unknown = [], [], []
        for _k, _v in _top(s["errors"], 5):
            _t = _e_last.get(_k)
            _h = None
            if _t and _e_now:
                try:
                    _a = datetime.fromisoformat(str(_t).replace("Z", "+00:00"))
                    _b = datetime.fromisoformat(str(_e_now).replace("Z", "+00:00"))
                    if _a.tzinfo is None:
                        _a = _a.replace(tzinfo=timezone.utc)
                    if _b.tzinfo is None:
                        _b = _b.replace(tzinfo=timezone.utc)
                    _h = max(0.0, (_b - _a).total_seconds() / 3600.0)
                except (TypeError, ValueError):
                    _h = None
            if _h is None:
                _unknown.append((_k, _v, _t))
            elif _h <= 24:
                _live.append((_k, _v, _h))
            else:
                _stale.append((_k, _v, _h))
        if _live:
            lines.append("- 错误串·活警（末次≤24h，仍在发生）: "
                         + " · ".join(f"{k} ×{v}（{h:.0f}h 前）" for k, v, h in _live))
        if _unknown:
            lines.append(f"- 错误串·活跃度未知（无时间戳，按活警处理）: "
                         + " · ".join(f"{k} ×{v}" for k, v, _t in _unknown))
        if _stale:
            # 陈迹**只降级不消失**（R614）：注入/模型下架这类安全面告警，消失即
            # "看不到就以为没发生"。措辞带末次时刻让读者自行判断是否真绝迹。
            lines.append("- ℹ️ 错误串·陈迹（全史累计，末次 >24h，无新发生）: "
                         + " · ".join(f"{k} ×{v}（末次 {h:.0f}h 前）"
                                      for k, v, h in _stale))
    return "\n".join(lines)


def filter_days(rows, days):
    """R109：时间窗过滤（--days N）——遥测按 ~85 行/天积累，全量口径会日益
    稀释近期信号（成功率/漏斗混入数天前的防御前历史）。保留 ts 在最近 N 天的行；
    无 ts 的行丢弃（严格"近期视图"语义——生产行恒有 ts，缺 ts 只出现在手搓夹具）。
    days 非正数或解析失败返回 None（不过滤，由调用方保持全量）。"""
    if days is None:
        return None
    try:
        days = float(days)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = []
    for r in rows:
        raw = r.get("ts")
        if not isinstance(raw, str):
            continue
        # R8：不能拿 ISO 串直接比大小。`append_metrics` 写的是 +00:00，但历史/手写行
        # 可能是 Z 结尾（'Z' > '+'，字典序恒大于任何 +00:00 行）或别的偏移量——
        # 前者会让陈旧行永远被判"在窗口内"。按真实时间解析，解析失败的行丢弃
        # （与"缺 ts 即丢弃"的严格近期视图语义一致）。
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept.append(r)
    return kept


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    days = None
    if "--days" in argv:
        i = argv.index("--days")
        if i + 1 < len(argv):
            days = argv[i + 1]
        else:
            print("--days 需要一个数字参数，例: --days 2", file=sys.stderr)
            return 2
    paths = [a for a in argv if not a.startswith("-")
             and a != str(days)]
    path = paths[0] if paths else DEFAULT_PATH
    if not os.path.exists(path):
        print(f"遥测文件尚不存在: {path}（有过投递/拦截后自动产生）", file=sys.stderr)
        return 2
    try:
        rows, bad = load_rows(path)
    except OSError as e:
        # 目录/权限等打不开的情况：给人话，不抛 traceback（定时任务日志里全是堆栈最烦人）
        print(f"无法读取遥测文件 {path}: {e}", file=sys.stderr)
        return 2
    if bad:
        print(f"跳过坏行 {bad} 行（不影响其余统计）", file=sys.stderr)
    filtered = filter_days(rows, days)
    if filtered is not None:
        rows = filtered
        print(f"时间窗过滤: 仅统计最近 {days} 天（{len(rows)} 行）", file=sys.stderr)
    s = summarize(rows)
    if as_json:
        doc = dict(s)
        doc["funnel"] = funnel(rows)
        doc["quality_scan"] = quality_scan(rows)
        print(json.dumps(doc, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(s, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
