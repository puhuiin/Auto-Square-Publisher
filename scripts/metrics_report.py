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
# TestQualityPatternSync）。R121/R282 守卫只覆盖正文开场句，而标题是信息流里
# 决定点不点开的第一触点、比正文开场更显眼：生产实录 11 篇长文标题里
# "刚出炉：Fed升息落地…"命中 R282 刚晋升进静态表的"刚出"族。
_TITLE_LEADINS = ("刚刚", "突发", "重磅", "快讯", "注意", "刚出")
_AI_FLAVOR_HARD = (  # main.MultiLLMEngine._AI_FLAVOR_HARD
    "拭目以待", "未来可期", "保驾护航", "谱写", "新篇章", "扬帆起航",
    "值得注意的是", "值得一提的是", "综上所述", "总而言之", "让我们一起",
    "毋庸置疑", "不言而喻", "共同见证",
)
QUALITY_SCAN_WINDOW = 20  # 最近 N 篇发布帖做合规扫描


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
            previews.append((pv, r.get("fng_ban_active")))
    previews = previews[-window:]
    out = {"scanned": len(previews), "fng_anchor": 0, "banned_device": 0,
           "ai_flavor": 0, "offenders": collections.Counter(),
           "fng_ban_armed": 0, "fng_violation": 0, "fng_avoided": 0}
    for pv, ban_active in previews:
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
    out["offenders"] = dict(out["offenders"])
    return out


# R124：开场指纹雷达——把 R104（"先泼盆冷水"三犯）与 R121（"刚刚"10 帖 3 次）
# 的人工发现过程产品化：自动扫描近期开场句的共享前缀，新指纹成形前预警。
# 只产预警不做禁令（自动禁令有误杀风险，禁令仍走 main.py 的窗口/永久机制）。
_FINGERPRINT_WINDOW = 10
_FINGERPRINT_MIN_HITS = 3
_ARTICLE_HEADER_RE = re.compile(r"^[一二三四五六七八九十]、")


def opener_fingerprint(rows, window=_FINGERPRINT_WINDOW, min_hits=_FINGERPRINT_MIN_HITS):
    """抽最近 N 帖开场句的首 2/4 字前缀，同一前缀 ≥min_hits 次即报预警。
    长文分节头（"一、发生了什么"）不是开场句，跳过取正文段（与 main.py
    _recent_openers 的 R121 修复同语义）。返回 {"scanned", "alerts": {前缀: 次数}}。"""
    openers = []
    for r in reversed(rows if isinstance(rows, list) else []):
        if not isinstance(r, dict) or r.get("dry_run") is True:
            continue
        if not _is_delivery_outcome(r.get("outcome")):
            continue
        pv = (r.get("final_preview") or "").strip()
        if not pv:
            continue
        opener = ""
        for seg in (s.strip() for s in re.split(r"[。\n]", pv)):
            if not seg:
                continue
            if _ARTICLE_HEADER_RE.match(seg):
                continue
            opener = seg
            break
        if opener:
            openers.append(opener)
        if len(openers) >= window:
            break
    # 4 字簇优先，2 字簇仅在其不是任何 4 字簇前缀时才报（去重：同簇只报最长）。
    # R131：2 字簇要求词边界——"Bitwise/BitGo"共享的"Bi"只是词的前半，不是
    # 指纹；"刚刚,$SHIB"/"刚刚 Solana"的"刚刚"后接标点/空格才是完整领词。
    # 生产实录：雷达报"Bi…"×3 实为两个不同实体名。边界=第 3 字符非 ASCII
    # 字母数字（CJK 跟随算边界："刚刚看涨"就是"刚刚"领句）。
    def _lead_word_boundary(opener: str) -> bool:
        nxt = opener[2:3]
        return nxt == "" or not (nxt.isascii() and nxt.isalnum())

    from collections import Counter
    alerts = {}
    clusters4 = {p: c for p, c in
                 Counter(o[:4] for o in openers if len(o) >= 4).items() if c >= min_hits}
    alerts.update(clusters4)
    for p, c in Counter(o[:2] for o in openers
                        if len(o) >= 2 and _lead_word_boundary(o)).items():
        if c >= min_hits and not any(p4.startswith(p) for p4 in clusters4):
            alerts[p] = c
    return {"scanned": len(openers), "alerts": dict(sorted(alerts.items(),
                                                           key=lambda kv: -kv[1]))}


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
    """与 cost_analysis.is_delivery_outcome / main._is_delivery_outcome 同语义（R211）：
    binance_published* 与副平台 *_delivered* 都算投递成功；
    already_delivered 是幂等跳过标记，排除。"""
    o = str(outcome or "")
    if o.startswith("binance_published"):
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
        "by_source": collections.Counter(),
        "by_provider": collections.Counter(),
        "by_token": collections.Counter(),
        "images": 0,
        "image_tiers": collections.Counter(),
        "zero_widget_posts": 0,
        "zero_tag_posts": 0,
        # R284：活动标签返佣归因覆盖（保底双标签之外的创作激励活动标签）
        "campaign_tag_evaluated": 0,
        "campaign_tag_covered": 0,
        "campaign_tag_zero_fresh": 0,
        # R285：浏览/互动 join（content_id × content_stats.jsonl）与三维归因样本
        "stats_posts": 0,
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
        "by_ending": collections.Counter(),
        # R288：人设分布与近期集中度（R287 修的是生成端，报表端监测其效果）
        "by_persona": collections.Counter(),
        "persona_seq": [],
        # R288：热度分分布——TOKEN_LIMIT_BYPASS_IMPACT(30)/ARTICLE_MIN_IMPACT(20)
        # 等门槛的校准基线，此前只能即席探针
        "impact_scores": [],
        # R222：发布内容新鲜度样本（age_hours 发布行全量携带，此前只能手工统计）
        "pub_ages": [],
        # R173：过期情报注入计数——R171 写侧已直录，报表端同轮补齐（R92 纪律）
        "intel_degraded_posts": 0,
        "intel_fresh_posts": 0,
        # R181：情报陈旧小时样本（有 last_updated 的帖才进）
        "intel_age_hours": [],
        # R199：最近一次情报成功刷新时刻（campaign_intel llm_success）
        "last_intel_refresh_ts": None,
        # R175：情报刷新被失败退避跳过的轮次（区分「配额早退没刷」vs「想刷被退避挡」）
        "intel_cooldown_skips": 0,
        "reject_by_stage": collections.Counter(),
        "reject_by_provider": collections.Counter(),
        "reject_reasons": collections.Counter(),
        # R163：质量门拒稿正文快照（最近几条）——短回/拒答型故障只报长度无法归因
        "reject_previews": [],
        "latency_by_provider": {},
        "tokens_by_provider": {},
        "errors": collections.Counter(),
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
        "feed_yield": {},
        "trend_freq": collections.Counter(),
        "last_hot_topics": "",  # R190：全网实时热点钩子供给（HN 等）
        "hot_topic_hits": 0,    # 出现过 hot_topics 的发帖轮数
        # R193：四路信号加权命中数（供给≠命中，此前不可见）
        "boost_hits": {"campaign": 0, "trend": 0, "hot": 0},
        "boost_runs": 0,
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
            hour = r.get("hour_bj", "unknown")
            try:
                hour = int(hour)
            except (TypeError, ValueError):
                hour = "unknown"
            s["by_hour"][hour] += 1
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
            wc = r.get("widget_count")
            if wc == 0:
                s["zero_widget_posts"] += 1
            # R125：返佣归因标签覆盖率——零标签帖 = #Write2Earn 归因丢失
            tc = r.get("tag_count")
            if tc == 0:
                s["zero_tag_posts"] += 1
            # R284：活动标签（返佣归因第 3 席）覆盖——tag_count>0 只证明保底双
            # 标签在，活动标签静默丢失（intel 无 active_tags / _inject_campaign_tag
            # 回归）时零可见。零覆盖且 intel_degraded≠True = 情报新鲜却没活动标签
            # 可注入 = 疑似注入回归；intel 降级时的零覆盖是合法语境（无供给）。
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
            # R286：长文标题眼钩普查（article_title 仅长文帖非空）——标题是信息流
            # 第一触点，数字/$挂件/疑问三类眼钩元素的覆盖率要有基线可查
            _at = r.get("article_title")
            if isinstance(_at, str) and _at.strip():
                s["article_titles"].append(_at.strip())
                if any(c.isdigit() for c in _at):
                    s["title_hooks"]["数字钩子"] += 1
                if "$" in _at:
                    s["title_hooks"]["$挂件"] += 1
                if "？" in _at or "?" in _at:
                    s["title_hooks"]["疑问钩子"] += 1
                if any(_at.startswith(w) for w in _TITLE_LEADINS):
                    s["title_leadin_hits"].append(_at.strip())
            # R130：结尾套路分布——验证 ShuffleBag 生产轮换均匀性
            if r.get("ending_style"):
                s["by_ending"][str(r["ending_style"])] += 1
            # R288：人设分布 + 时序（集中度告警要按发布顺序取近窗）
            if r.get("persona"):
                s["by_persona"][str(r["persona"])] += 1
                s["persona_seq"].append(str(r["persona"]))
            _imp = _num(r.get("impact_score"))
            if _imp is not None:
                s["impact_scores"].append(int(_imp))
            # R173：情报降级注入——None=历史行无字段，不进分母
            if r.get("intel_degraded") is True:
                s["intel_degraded_posts"] += 1
            elif r.get("intel_degraded") is False:
                s["intel_fresh_posts"] += 1
            # R181：情报陈旧小时数
            _iah = _num(r.get("intel_age_hours"))
            if _iah is not None:
                s["intel_age_hours"].append(_iah)
        if outcome == "intel_cooldown_skip":
            # R175：想刷新但被 2h 失败退避挡下——与配额早退（根本没走到这里）区分
            s["intel_cooldown_skips"] += 1
        elif outcome == "llm_success" and r.get("stage") == "campaign_intel":
            # R199：最近成功刷新时刻——判断下一次 12h 过期、以及饱和轮是否在喂陈旧情报
            if isinstance(ts, str) and ts:
                if s["last_intel_refresh_ts"] is None or ts > s["last_intel_refresh_ts"]:
                    s["last_intel_refresh_ts"] = ts
        elif outcome == "llm_rejected":
            s["reject_by_stage"][str(r.get("stage", "unknown"))] += 1
            s["reject_by_provider"][who] += 1
            if r.get("reason"):
                s["reject_reasons"][str(r["reason"])[:60]] += 1
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
            s["errors"][str(err)[:60]] += 1
        lat = _num(r.get("llm_latency_sec"))
        if lat is not None:
            lat_tmp[str(prov)].append(lat)
        tok = _num(r.get("tokens_used"))
        if tok is not None:
            tok_tmp[str(prov)].append(tok)
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
                           ("hot", "hot_boost_hits")):
                _v = _num(r.get(_f))
                if _v is not None and _v > 0:
                    _bh[_k] = int(_v)
            if _bh:
                runs_tmp["boost_runs"] += 1
                for _k, _v in _bh.items():
                    runs_tmp["boost_hits"][_k] += _v
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
                    _agg = runs_tmp["feed_yield"].setdefault(_fname, [0, 0])
                    _agg[0] += int(_fy.get("entries") or 0)
                    _agg[1] += int(_fy.get("kept") or 0)
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


def render_text(s, rows=None):
    """人类可读简报"""
    lines = [
        "## metrics 遥测简报",
        f"- 样本: {s['total']} 行"
        + (f"（时间跨度 {s['ts_min'][:16]} → {s['ts_max'][:16]}）" if s["ts_min"] else "（尚无带时间戳样本）")
        + (f"，其中 {s['dry_skipped']} 行 dry-run 已排除" if s["dry_skipped"] else ""),
        f"- outcome 分布: {dict(s['by_outcome']) or '—'}",
    ]
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
            lines.append(
                f"  📈 加权命中（{runs.get('boost_runs', 0)} 轮）: "
                f"活动 {bh.get('campaign', 0)} / 热搜 {bh.get('trend', 0)} / 热点 {bh.get('hot', 0)}")
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
        # 换源/撤源决策首次有可回查数据面（此前只进易失 Step Summary）
        if runs.get("feed_yield"):
            parts = []
            for _fname, (_ents, _kept) in sorted(runs["feed_yield"].items(),
                                                 key=lambda x: (-x[1][1], -x[1][0])):
                flag = " ⚠️" if _ents >= 20 and _kept == 0 else ""
                parts.append(f"{_fname} {_kept}/{_ents}{flag}")
            lines.append(f"  📡 源入选率(入选/扫描): {' · '.join(parts)}")
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
        # R124：开场指纹雷达——共享前缀 ≥3/10 即预警（禁令仍走 main.py 机制，
        # 这里只负责让新指纹在成形期可见，不再依赖人工抽样发现）
        fp = opener_fingerprint(rows)
        if fp["alerts"]:
            detail = "、".join(f"“{p}…”×{c}" for p, c in fp["alerts"].items())
            lines.append(f"  🔭 开场指纹预警（近 {fp['scanned']} 帖开场共享前缀）: {detail}")
    n_pub = sum(s["by_provider"].values())
    if n_pub:
        lines.append(f"- 投递 {n_pub} 篇：分时 {_top(s['by_hour'])} / 来源 {_top(s['by_source'])}")
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
        # R125：零标签帖 = #Write2Earn 返佣归因丢失
        if s.get("zero_tag_posts"):
            lines.append(f"  ⚠️ 全文零标签 {s['zero_tag_posts']}/{n_pub} 篇——返佣归因丢失，需排查")
        # R284：活动标签覆盖——情报新鲜却零活动标签 = _inject_campaign_tag 疑似回归
        if s.get("campaign_tag_zero_fresh"):
            lines.append(f"  ⚠️ 情报新鲜但无活动标签 {s['campaign_tag_zero_fresh']}/"
                         f"{s['campaign_tag_evaluated']} 篇——创作激励活动标签未注入，需排查")
        # R285：浏览/互动面板——有 join 上的样本才渲染（无 stats 时整块不出现）。
        # 三维均浏览是"哪类帖有流量"的第一手答案：时段/体裁/来源各自的样本量
        # 一并给出，样本 <3 的桶只展示不解读（避免小样本误判）。
        if s.get("stats_posts"):
            _v = s["stats_views"]
            _lk = s["stats_likes"]
            _cm = s["stats_comments"]
            _fmt = lambda xs: f"{sum(xs)/len(xs):.0f}" if xs else "-"
            lines.append(f"  📊 内容数据（{s['stats_posts']} 篇有记录）: "
                         f"均浏览 {_fmt(_v)} · 均点赞 {_fmt(_lk)} · 均评论 {_fmt(_cm)}"
                         f"（总浏览 {s['stats_views_total']}）")
            _hb = _bucket_line(s["stats_by_hourbucket"])
            if _hb:
                lines.append(f"    时段均浏览: {_hb}")
            _gg = _bucket_line(s["stats_by_genre"])
            if _gg:
                lines.append(f"    体裁均浏览: {_gg}")
            _sc = _bucket_line(s["stats_by_source"], top=3)
            if _sc:
                lines.append(f"    来源均浏览: {_sc}")
        # R286：长文标题眼钩基线（有长文标题才渲染）+ 禁用领词告警
        if s.get("article_titles"):
            _n = len(s["article_titles"])
            _avg = sum(len(t) for t in s["article_titles"]) / _n
            _hooks = " · ".join(f"{k} {v}/{_n}"
                                for k, v in s["title_hooks"].most_common())
            lines.append(f"  📐 长文标题（{_n} 篇 · 均长 {_avg:.0f} 字）: {_hooks}")
        if s.get("title_leadin_hits"):
            _hits = s["title_leadin_hits"]
            _pref = "、".join(sorted({h[:2] for h in _hits}))
            lines.append(f"  ⚠️ 长文标题命中禁用领词 {len(_hits)}/"
                         f"{len(s['article_titles'])} 篇（{_pref}…）——R121/R282 "
                         f"守卫只覆盖正文开场，标题是更显眼的指纹位")
        # R130：结尾套路分布（验证 ShuffleBag 轮换均匀性；旧 schema 无字段则不渲染）
        if s["by_ending"]:
            ending_str = " · ".join(f"{k} ×{v}" for k, v in s["by_ending"].most_common(5))
            lines.append(f"  结尾套路分布: {ending_str}")
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
    n_rej = sum(s["reject_by_stage"].values())
    if n_rej:
        lines.append(f"- 拦截 {n_rej} 次：阶段 {_top(s['reject_by_stage'])} / 模型 {_top(s['reject_by_provider'])}")
        if s["reject_reasons"]:
            lines.append(f"  高频原因 {_top(s['reject_reasons'], 5)}")
        if s.get("reject_previews"):
            lines.append("  拒稿快照（最近）:")
            for item in s["reject_previews"][-3:]:
                lines.append(
                    f"    [{item.get('stage')}/{item.get('provider')}] "
                    f"finish={item.get('finish_reason') or '?'} "
                    f"{item.get('preview', '')}")
    if s["latency_by_provider"]:
        lines.append(f"- 平均延迟(s) {dict(sorted(s['latency_by_provider'].items()))}")
    if s["tokens_by_provider"]:
        lines.append(f"- token 消耗 {dict(sorted(s['tokens_by_provider'].items()))}")
    if s["errors"]:
        lines.append(f"- 错误串 {_top(s['errors'], 5)}")
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
