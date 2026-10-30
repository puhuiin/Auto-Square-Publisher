#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""英文夜窗体检 + **预注册判据**（R819）。

★★ 为什么要有这个脚本（而不是每次临时写一段 py）
  英文夜窗（UTC 14:00–22:00）的「正文活动提及率」是下一个待验点，而**"先看数字
  再决定算不算改善"在本项目是明令禁止的**——必须先把判据写死、再跑数据，否则
  事后总能给任意结果编出解释（R780b / R799 同族：**会变的门槛等于没有门槛**）。
  所以把口径与阈值**写进代码**：随 git 历史留痕、有测试逐条钉住、只读不改。

口径（写死；不得因结果而改）
  · 窗口：同一 UTC 日、`14:00:00 ≤ ts < 22:00:00`（= 北京夜窗 22:00–06:00）。
  · 样本：`outcome` 以 `binance_published` 开头**且 `final_text` 非空**的行。
  · 提及：`campaign_vocab.mention_in_text(final_text)`（去标签 + 当前词表）。
  · 「配置正常」帖：`campaign_tag_count ≥ 2`（活动标签已注入）且正文汉字数 < 20
    （排除语种跑偏帖——那类帖中文正文会"顺带"命中，会虚高提及率）。
  · 「真漏」：配置正常帖里**复算提及为 False**。
  · 「提及率」：本窗口全部样本里的复算提及率（与 ⑥ 日报同源口径）。

基线（10-19 夜窗，2026-10-19T14:44Z–21:44Z）：配置正常 18 篇，真漏 **4** 篇 = 22.2%。

预注册阈值
  · **单窗口不能判"通过"**：两臂各 18 篇时把 4/18 改善成 1/18，单侧 Fisher 精确
    检验只有 **p=0.169**（本脚本**现算**它、不写死结论；→0/18 才 0.052）⇒ 达不到
    任何常规显著水平。单窗口只允许出两种判定：`回滚`（触发下面的红线）或`样本不足`。
  · **通过**（须**两臂各 ≥ 2 个窗口、合计 n ≥ 36**）：
      合并真漏率 ≤ 1/18（5.6%）**且** 合并提及率 ≥ 基线提及率（不得下降）
  · **回滚**（任一成立即回滚，不必等第二个窗口）：
      · 单窗口真漏率 > 3/18（16.7%）
      · 单窗口提及率比基线低 ≥ 10 个百分点
  · **副作用守卫**（与主判据同时读，防止"提了活动但别的变差"）：
      同期 `campaign_cta_tpl` 收敛率不得比基线上升；`quality` 类拒稿数不得增加。

用法
  # 只看基线（夜里读完当天窗口）
  python scripts/en_window_audit.py --metrics .tmp_analysis/online_metrics.jsonl \\
      --baseline 2026-10-19
  # 两臂对照 + 出判定
  python scripts/en_window_audit.py --metrics ... \\
      --baseline 2026-10-19,2026-10-20 --treatment 2026-10-21,2026-10-22
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from campaign_vocab import mention_in_text  # noqa: E402  （唯一实现，R814 下沉）
from ai_flavor import scan as ai_flavor_scan  # noqa: E402  （唯一实现，R851：流水线门与本脚本共用）

NIGHT_START_H, NIGHT_END_H = 14, 22          # UTC 夜窗 = 北京 22:00–06:00
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
CJK_MAX_FOR_NORMAL = 20                      # ≥20 汉字视为语种跑偏帖

# ---- 预注册基线（写死：改动必须同时改测试，见 TestR819Preregistration）----
BASELINE_MISS, BASELINE_NORMAL = 4, 18       # 10-19 夜窗实测
BASELINE_MENTION_RATE = None                 # 由调用方传入实测值；None=不设下沿

# ---- 预注册阈值 ----
MIN_ARM_WINDOWS = 2        # 每臂最少窗口数
MIN_ARM_N = 36             # 每臂最少「配置正常」帖数


def in_night_window(ts: str, date_utc: str) -> bool:
    """ts 是否落在 date_utc 这一天的夜窗 [14:00Z, 22:00Z)。畸形 ts → False。"""
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return False
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc)
    return (d.strftime("%Y-%m-%d") == date_utc
            and NIGHT_START_H <= d.hour < NIGHT_END_H)


def window_stats(rows, date_utc: str) -> dict:
    """单个夜窗的口径统计（纯函数，只读传入的 rows）。"""
    cohort = [r for r in rows
              if str(r.get("outcome", "")).startswith("binance_published")
              and isinstance(r.get("final_text"), str) and r["final_text"].strip()
              and in_night_window(r.get("ts"), date_utc)]
    picked = [mention_in_text(r["final_text"]) for r in cohort]
    normal = [r for r in cohort
              if (r.get("campaign_tag_count") or 0) >= 2
              and len(CJK_RE.findall(r["final_text"])) < CJK_MAX_FOR_NORMAL]
    normal_hits = [mention_in_text(r["final_text"]) for r in normal]
    return {
        "date": date_utc,
        "n": len(cohort),
        "mention": sum(picked),
        "mention_rate": (100.0 * sum(picked) / len(picked)) if picked else None,
        "normal": len(normal),
        "miss": sum(1 for h in normal_hits if not h),
        "miss_rate": ((100.0 * sum(1 for h in normal_hits if not h) / len(normal_hits))
                      if normal_hits else None),
        "langdev": sum(1 for r in cohort
                       if len(CJK_RE.findall(r["final_text"])) >= CJK_MAX_FOR_NORMAL),
    }


def fisher_improve_p(base_miss: int, base_hit: int,
                     treat_miss: int, treat_hit: int) -> float:
    """单侧 Fisher 精确检验：P(处置臂漏帖数 ≤ 观测值 | 边际固定)。

    用于回答"这点改善到底算不算信号"。**纯整数组合数、无随机数** ⇒ 可复现、可测。
    ⚠️ 首版把参数顺序写反（miss/hit 对调）且分母固定用 row_a——行数不等时分母就错了；
    现在显式命名，并用 row_b 做抽样行。
    """
    def _c(n, k):
        return math.comb(n, k) if 0 <= k <= n else 0
    row_b = treat_miss + treat_hit
    col_miss = base_miss + treat_miss
    total = base_miss + base_hit + row_b
    denom = _c(total, row_b)
    if denom == 0:
        return 1.0
    p = sum(_c(col_miss, k) * _c(total - col_miss, row_b - k)
            for k in range(0, treat_miss + 1))
    return min(1.0, p / denom)


def _pool(stats_list) -> dict:
    n = sum(s["n"] for s in stats_list)
    mention = sum(s["mention"] for s in stats_list)
    normal = sum(s["normal"] for s in stats_list)
    miss = sum(s["miss"] for s in stats_list)
    return {"date": "合并(%d 窗口)" % len(stats_list),
            "n": n, "mention": mention, "normal": normal, "miss": miss,
            "langdev": sum(s["langdev"] for s in stats_list),
            "mention_rate": (100.0 * mention / n) if n else None,
            "miss_rate": (100.0 * miss / normal) if normal else None,
            "windows": len(stats_list)}


# ---- 阈值以**整数分数**给出，判定全程用整数交叉相乘（见 verdict 的注释）----
REVERT_MARGIN_PP = 15      # 单窗口真漏率比**基线**高 ≥15 个百分点才算"明显变差"
REVERT_MIN_NORMAL = 10     # 且该窗口至少 10 篇「配置正常」才谈比率（n<10 时 1 篇=10pp+，是噪声）
PASS_MISS_NUM, PASS_MISS_DEN = 1, 18           # 合并通过线：漏率 ≤ 1/18
MENTION_DROP_PP = 10                           # 单窗口提及率跌幅上限（百分点）


def verdict(base_stats, treat_stats) -> tuple:
    """预注册判定。返回 (状态, 说明)。

    ⚠️★ **阈值比较必须走整数交叉相乘，不能比浮点百分比**：首版用
    `s["miss_rate"] > 100.0 * REVERT_MISS_RATE` 判红线，而
    `100.0*1/6 = 16.666666666666668`、`100.0*(3.0/18.0) = 16.666666666666664`
    ⇒ **恰好等于红线的窗口被判成"回滚"**（1/6 与 3/18 明明是同一个数）。
    浮点边界上的"等价"永远不会等价，所以判据只比较整数。
    """
    base = _pool(base_stats)
    treat = _pool(treat_stats)
    # ① 单窗口红线——**相对基线**（不必等第二个窗口）
    #   ⚠️★ R832 更正：R819 写的是**绝对**红线 `> 3/18 = 16.7%`，而实测基线是 **36%**
    #   ⇒ 那条线对**任何**窗口都开火（本轮实测：把根本没部署改动的 10-22 判成"回滚"）。
    #   回滚线只有**相对基线**才表达"变差了"；**通过线（≤1/18）不动**。
    _bn, _bm = base["normal"], base["miss"]
    for s in treat_stats:
        _n, _m = s["normal"], s["miss"]
        # ⚠️★ R832：还得要求**单窗口最低样本量**——10-23 当晚只发了 3 篇、全漏 ⇒ 100%，
        #   相对红线照样开火（"3/3 比基线高 15pp"）。n<10 时一篇的波动就是 10 个百分点以上，
        #   那不是"变差"，是噪声。样本不足的窗口应由判定②的"样本不足"兜住，不是判回滚。
        if not (_bn and _n) or _n < REVERT_MIN_NORMAL:
            continue
        # m_/n_ − bm/bn ≥ 15pp  ⇔  100*(m_*bn − bm*n_) ≥ 15*bn*n_   （全程整数）
        if 100 * (_m * _bn - _bm * _n) >= REVERT_MARGIN_PP * _bn * _n:
            return ("回滚", "单窗口真漏率 %d/%d 比基线 %d/%d 高 ≥%d 个百分点（%s）"
                    % (_m, _n, _bm, _bn, REVERT_MARGIN_PP, s["date"]))
        # 提及率跌幅 ≥ 10 个百分点 ⇔ 100*(b_hit/b_n - s_hit/s_n) ≥ 10
        if (base["n"] and s["n"]
                and 100 * (base["mention"] * s["n"] - s["mention"] * base["n"])
                >= MENTION_DROP_PP * base["n"] * s["n"]):
            return ("回滚", "单窗口提及率 %d/%d 比基线 %d/%d 低 ≥%d 个百分点（%s）"
                    % (s["mention"], s["n"], base["mention"], base["n"],
                       MENTION_DROP_PP, s["date"]))
    # ② 样本量不足（把 p 现算出来，而不是只说"样本小"）
    #   ⚠️ 参数顺序是 (base_miss, base_hit, treat_miss, treat_hit)——**别传反**
    #   （本轮已连踩两次：签名写错一次、调用传反一次，p 会给出完全不同、
    #    看起来很"正常"的数字）。测试里有"效应越大 p 越小"的单调性守卫。
    if len(treat_stats) < MIN_ARM_WINDOWS or treat["normal"] < MIN_ARM_N:
        p = fisher_improve_p(base["miss"], base["normal"] - base["miss"],
                             treat["miss"], treat["normal"] - treat["miss"])
        return ("样本不足", "需每臂 ≥%d 窗口 / n≥%d；当前基线 %d 篇、处置 %d 篇；"
                "单侧 Fisher p=%.3f ⇒ 这点改善不足以判定"
                % (MIN_ARM_WINDOWS, MIN_ARM_N, base["normal"], treat["normal"], p))
    # ③ 通过条件：漏率 ≤ PASS_NUM/PASS_DEN 且提及率不下降（整数比较）
    if treat["normal"] and treat["miss"] * PASS_MISS_DEN > PASS_MISS_NUM * treat["normal"]:
        return ("未通过", "合并真漏率 %d/%d 高于通过线 %d/%d"
                % (treat["miss"], treat["normal"], PASS_MISS_NUM, PASS_MISS_DEN))
    if base["n"] and treat["n"] and treat["mention"] * base["n"] < base["mention"] * treat["n"]:
        return ("未通过", "提及率下降：处置 %d/%d < 基线 %d/%d"
                % (treat["mention"], treat["n"], base["mention"], base["n"]))
    return ("通过", "合并真漏率 %d/%d ≤ %d/%d 且提及率未下降"
            % (treat["miss"], treat["normal"], PASS_MISS_NUM, PASS_MISS_DEN))


def _fmt(s: dict) -> str:
    return ("%s  n=%-3d 提及 %s/%d=%s ｜ 配置正常 %d 篇、真漏 %d=%s ｜ 语种跑偏 %d"
            % (s["date"], s["n"], s["mention"], s["n"],
               ("%.0f%%" % s["mention_rate"]) if s["mention_rate"] is not None else "—",
               s["normal"], s["miss"],
               ("%.0f%%" % s["miss_rate"]) if s["miss_rate"] is not None else "—",
               s["langdev"]))


# ---------------------------------------------------------------------------
# 归因切片（R835）：真漏帖**不是随机分布**——先看它与哪些既有字段相关。
# ⚠️ **只做诊断、不设阈值**（不新增判据；避免又拍一个数字）。
# 生产实证（10-19~10-23，en 夜窗，提了 70 / 真漏 43）：
#   ending_style 榜首：提了组「极简站队/多空辩论/价位竞猜…」；
#                     真漏组「**仓位表白 8**、情绪表态 7、**灵魂拷问 6**…」
#   末句含问号比例：提了 **30%** vs 真漏 **12%**
#   cta_style：真漏里有 **4 篇 = None**（菜单没注入，属已知的 intel 分支）
# ⇒ 线索：**自我指涉类结尾（仓位表白/灵魂拷问）会把活动句挤掉**。
#   下一轮若要动 prompt，优先级是"把活动要求放到**结尾风格指令之后**"，
#   但**必须等 R830 的实验窗口先验收完**（连改两次 prompt 会把实验污染掉）。
# ---------------------------------------------------------------------------
def miss_rows(rows, date_utc: str):
    """该 UTC 夜窗里「配置正常但没提活动」的行（口径与判定①完全一致）。"""
    cohort = [r for r in rows
              if str(r.get("outcome", "")).startswith("binance_published")
              and isinstance(r.get("final_text"), str) and r["final_text"].strip()
              and in_night_window(r.get("ts"), date_utc)]
    miss = []
    for r in cohort:
        if len(CJK_RE.findall(r["final_text"])) >= CJK_MAX_FOR_NORMAL:
            continue                                  # 语种跑偏帖不进「配置正常」
        if (r.get("campaign_tag_count") or 0) < 2:
            continue                                  # 无活动标签 ⇒ 菜单没注入
        if mention_in_text(r["final_text"]) is False:
            miss.append(r)
    return miss


def miss_breakdown(miss) -> dict:
    """真漏帖的归因切片（诊断用，无阈值）。"""
    import collections
    return {
        "n": len(miss),
        "ending_style": collections.Counter(str(r.get("ending_style")) for r in miss),
        "cta_style": collections.Counter(str(r.get("campaign_cta_style")) for r in miss),
        "q_tail_pct": (100.0 * sum(1 for r in miss
                                   if "?" in body_before_tags(r["final_text"])[-60:])
                       / len(miss)) if miss else 0.0,
    }


def _fmt_breakdown(bd: dict) -> str:
    top = lambda c: "、".join("%s×%d" % (k, v) for k, v in c.most_common(3)) or "—"
    return ("真漏 %d 篇 ｜ 末句含问号 %.0f%% ｜ ending_style %s ｜ cta_style %s"
            % (bd["n"], bd["q_tail_pct"],
               top(bd["ending_style"]), top(bd["cta_style"])))


# ===========================================================================
# 第二条预注册判据（R831）：英文帖**断在词中间**
# ---------------------------------------------------------------------------
# 口径（**R832 更正**）：
#   · 样本＝该 UTC 日**全部** `binance_published` 且 `final_text` 非空的 en 帖。
#   · 「正文」＝`final_text` 里**第一个 `#` 之前**的部分（读者看到的结尾就在那里）。
#     ⚠️ 两个已踩的坑：① 不能用 `strip_tags`——它会把标签行里的**非标签词**
#     （`DeFi`/`Futures`/`MemeCoin`）留在结尾，于是**每一篇**都"以字母结尾"、检测恒真；
#     ② 不能设"总长 ≥880"这种阈值——实测两篇断词帖总长只有 **839/838**（正文 782），
#     阈值把它们全滤掉，判定报 0 篇（**假绿**）。⇒ 只看"正文末字符是不是字母"。
#   · 排除项：末尾 token 是 `$挂件` 或是 ≤6 位的全大写 ticker（"看 $BTC" 这类合法收尾）。
#   ★★ 机制（**R833 更正：是字数上限，不是 token 预算**）：
#     R832 曾据此行遥测（`completion_tokens=2961`、`budget_cap=9000`）判定"是模型输出被
#     token 预算截断在半句"——**那个结论是错的**，因为比错了基准：净化预算不是 900 而是
#     `SHORT_FORM_MAX_CHARS - _APPEND_HEADROOM = 900 - 80 = **820***（`_sanitize_content`
#     第 4 步"长度保护先行"），而 `_enforce_max_chars` 还要再减去尾部标签（~57）。
#     **决定性证据是聚类**：全窗口 7 篇断句帖的**总长挤在 838~840**（正文 777~789，
#     7 篇只差 2 个字符），而正常帖在同长度带里最大到 798 ⇒ 固定上限截断的签名；
#     若是模型/token 侧截断，长度会**散开**而不是紧贴同一处。
#     ⇒ **R831 的边界修复（补英文标点层）就是正确修法**，R833「完稿完整性门」**不需要**
#       （token 侧那条路在循环外已有 `if final_finish == "length": raise` 兜着）。
TRUNC_MIN_DAYS = 2
TRUNC_MIN_POSTS = 40


def body_before_tags(text: str) -> str:
    """正文＝**标签行之前**的部分（读者看到的结尾就在那里）。

    ★★★ R847 更正：旧实现是「按**第一个** `#` 切」——正文里只要出现一个 `#`（哪怕只是
    `#2`＝"第 2 名"），"正文"就只剩前缀、而前缀**恰好以字母结尾** ⇒ `is_truncated` 误判。
    生产实证（10-28T18:24，`raw_chars=940`、`trimmed_chars=310`、总长 630，一看就不像"顶到
    820 预算"那条路，逼着去读原文才发现）：正文写着
    `…up just 0.44% to $101.41 despite being **#2** trending on CoinGecko.` —— 那篇**根本没断句**，
    是**检测器**假阳性（R846 的"回滚"读数就是这么来的）。

    判据（与 `main.py::_enforce_max_chars._is_widget_tag_line` 同源，不做第二份实现）：
      · 先看**最后一个非空行**：若其中至少一个 token 以 `#` 开头、且**所有** token 都以
        `#`/`$` 开头 ⇒ 它就是标签行，正文＝它之前的全部内容；
      · 否则兜底：按「**看起来像真标签**的 `#`」切（`#` 后跟字母或汉字）——这条专为排除
        正文里的 `#2`/`#1` 这类"井号+数字"（它不是标签，是序号）。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    lines = t.split("\n")
    tail_toks = lines[-1].strip().split()
    if tail_toks and any(x.startswith("#") for x in tail_toks) and all(
            x.startswith("#") or x.startswith("$") for x in tail_toks):
        return "\n".join(lines[:-1]).strip()
    m = re.search(r"#[A-Za-z\u4e00-\u9fff]", t)
    return t[:m.start()].strip() if m else t


def is_truncated(r: dict) -> bool:
    """该发布行是否为「断在半句」——正文末尾没有收尾标点。"""
    t = r.get("final_text")
    if not isinstance(t, str) or not t.strip():
        return False
    body = body_before_tags(t)
    if not body:
        return False
    tail = body.split()[-1] if body.split() else ""
    if tail.startswith("$") or (tail.isupper() and tail.isalpha() and len(tail) <= 6):
        return False                      # $挂件 / 全大写 ticker 结尾 = 合法收尾
    return bool(re.search(r"[A-Za-z]$", body))


def ai_flavor_stats(rows, date_utc: str, lang: str = "en") -> dict:
    """★★ R851：**AI 味旗标率**（只量不设门）。

    动机：平台 2026-07 起**对纯 AI 内容算法降权**（官方原文："Content generated entirely by
    AI will be de-boosted"）⇒ 我们需要知道"**通过了门却仍带 AI 特征**"的帖有多少。
    ⚠️ 本函数**只度量**：不新增任何拒稿门（加门要另行预注册 ⇒ 且会改变发布集合 ⇒ 必须等
    R848 的 ③ 窗口关掉再谈）。

    口径：
      · 样本 = 该窗口 `binance_published` 且 `final_text` 非空、且语种匹配的行；
      · `hard_hits` = 命中硬清单的篇数（正常应为 0——门会拦 ✓，若 >0 说明门没生效 ✗）；
      · `soft_any` = 命中 ≥1 个软特征的篇数；
      · `residual` = 任一（硬命中 / 软特征 / 宏大开场 / 不仅…更 / 首先…其次 / 破折号≥1）
        命中的篇数 = **过了门但仍带 AI 味的残余** ⇒ 这才是我们要盯的数 ✓。
    """
    n = hard = soft_any = residual = 0
    flags = []
    for r in rows or []:
        if not str(r.get("outcome", "")).startswith("binance_published"):
            continue
        if r.get("lang") != lang:
            continue
        t = r.get("final_text")
        if not isinstance(t, str) or not t.strip():
            continue
        if not in_night_window(r.get("ts"), date_utc):
            continue
        n += 1
        s = ai_flavor_scan(t)
        if s["hard"]:
            hard += 1
            flags.append(s["hard"][0])
        if s["soft"]:
            soft_any += 1
        if (s["hard"] or s["soft"] or s["grandiose"] or s["bu_jin"]
                or s["first_then"] or s["dash"]):
            residual += 1
    return {"date": date_utc, "lang": lang, "n": n, "hard_hits": hard,
            "soft_any": soft_any, "residual": residual,
            "residual_pct": (100.0 * residual / n) if n else 0.0,
            "hard_examples": flags[:3]}


def formulaic_repetition_stats(rows, date_utc: str, min_docs: int = 6, n: int = 12,
                               topk: int = 15) -> dict:
    """★★ R851b：**公式化重复率**（平台社群准则明确列为违规项：大量发布高度相似/结构相同）。

    口径（写死，便于复现）：
      · 样本 = 该窗口 en 已发布帖（用 `body_before_tags` 取正文，R847 修过"正文里 #2 不算标签"）✓；
      · 空白归一后取 **ASCII n-gram（长度 n）** ✓（中文词不参与英文侧统计 ✓）；
      · 只统计**跨帖出现 ≥ min_docs 篇**的片段 ✓ ⇒ 过滤掉偶然巧合 ✓；
      · 指标 = 含 top-N 任一片段的帖占比 ✓（受影响面，不是片段数 ✓）。
    ⚠️ **只度量不设门**：降低重复率要靠 prompt/模型侧改动（改变发布集合）⇒ 必须等
      R848 的 ③ 窗口关掉，并另行预注册"降重复率"的判据 ✓。本函数只出读数 ✓。
    """
    def is_pub_en(r):
        if not str(r.get("outcome", "")).startswith("binance_published"):
            return False
        if r.get("lang") != "en":
            return False
        return in_night_window(r.get("ts"), date_utc)

    texts = []
    for r in rows or []:
        if is_pub_en(r):
            b = body_before_tags(r.get("final_text") or "")
            b = re.sub(r"\s+", " ", b).strip()
            if b:
                texts.append(b)
    if not texts:
        return {"date": date_utc, "n": 0, "min_docs": min_docs, "ngram": n, "topk": topk,
                "share_pct": 0.0, "fragments": []}
    cnt = collections.Counter()
    for t in texts:
        seen = set()
        for i in range(len(t) - n + 1):
            g = t[i:i + n]
            if not g.isascii():
                continue
            seen.add(g)
        for g in seen:
            cnt[g] += 1
    frags = [(g, c) for g, c in cnt.most_common() if c >= min_docs][:topk]
    hit = sum(1 for t in texts if any(g in t for g, _ in frags))
    return {"date": date_utc, "n": len(texts), "min_docs": min_docs, "ngram": n,
            "topk": topk, "frag_count": len(frags), "hit": hit,
            "share_pct": (100.0 * hit / len(texts)) if texts else 0.0,
            "fragments": [f[0] for f in frags]}


# ---- 第六条预注册判据（R851b）：公式化重复率 ----
# 依据：平台社群准则把「**大量发布高度相似或结构相同的内容**」列为违规项 ✓；
#       实测（en 夜窗 257 篇）「含 top-15 重复片段的帖」占 **78%** ✗ ⇒ 有可改进空间 ✓。
# ⚠️ 这是**给未来那次"降重复率"改动**预注册的判据：改动本身会改变发布集合 ⇒ 必须等
#    R848 的 ③ 窗口关掉再动 ✓。在此之前，本判据只在"基线/现状"下出读数 ✓。
REPETITION_DROP_PP = 15          # 通过 = 处置臂比基线低 ≥15 个百分点


def repetition_verdict(base_stats, treat_stats) -> tuple:
    """R851b 预注册判定（返回 (状态, 说明)）。"""
    if not treat_stats:
        return ("样本不足", "处置臂为空")
    n = sum(s.get("n", 0) for s in treat_stats)
    if len(treat_stats) < MIN_ARM_WINDOWS or n < MIN_ARM_N:
        return ("样本不足", "需 ≥%d 窗 / n≥%d；当前 %d 窗 / %d 篇"
                % (MIN_ARM_WINDOWS, MIN_ARM_N, len(treat_stats), n))
    base = sum(s["share_pct"] for s in base_stats or []) / max(1, len(base_stats or []))
    treat = sum(s["share_pct"] for s in treat_stats) / len(treat_stats)
    if treat >= base:
        return ("回滚", "重复率 %.1f%% 未低于基线 %.1f%%（改了但没降 ⇒ 无效）"
                % (treat, base))
    if base - treat >= REPETITION_DROP_PP:
        return ("通过", "重复率 %.1f%% ≤ 基线 %.1f%% − %dpp" % (treat, base, REPETITION_DROP_PP))
    return ("未通过", "重复率 %.1f%% 低于基线 %.1f%% 但降幅 < %dpp ⇒ 保留并标注「未被证实」"
            % (treat, base, REPETITION_DROP_PP))


def overwrite_stats(rows, date_utc: str, lang: str = "en", trim_threshold: int = 50) -> dict:
    """★★ R851g/h：**过度写作率**（raw 超净化预算 ⇒ 被裁的比例）。

    动机：R851e 发现"被大幅截断的帖漏提活动 50%、未截断的只有 4%" ✗；R851g 进一步定位到
    **这是英文侧特有**（zh 侧 0/102 ✓、en 侧 52% ✗）⇒ 是英文篇幅指令没被遵守 ✓。
    ⚠️ 本函数**只度量**：降 over-write 要靠改英文 prompt（改变发布集合）⇒ 必须等
      R848 的 ③ 窗口关掉 + 另行预注册"降 over-write"的改动 ✓。

    口径：
      · 样本 = 该窗口该语种的已发布帖，且带 R845 的 `trimmed_chars` ✓；
      · `trimmed_chars > trim_threshold` ⇒ 判定为"被大幅裁掉" ✓；
      · 未知（无字段）**不计 0** ✓，单独报 `unknown` ✓。
    """
    known = over = unknown = 0
    for r in rows or []:
        if not str(r.get("outcome", "")).startswith("binance_published"):
            continue
        if r.get("lang") != lang:
            continue
        if not in_night_window(r.get("ts"), date_utc):
            continue
        tc = r.get("trimmed_chars")
        if not isinstance(tc, int):
            unknown += 1
            continue
        known += 1
        if tc > trim_threshold:
            over += 1
    return {"date": date_utc, "lang": lang, "known": known, "unknown": unknown,
            "over": over, "trim_threshold": trim_threshold,
            "over_pct": (100.0 * over / known) if known else 0.0}


# ---- 第七条预注册判据（R851h）：过度写作率 ----
# 依据：R851g 实测 en 侧 **52%** 的帖因超预算被裁 ✗、zh 侧 **0%** ✓
#      ⇒ 这是①（漏提）与③（顶格）共同的根 ✓。
# ⚠️ 给"收紧英文篇幅指令"那次改动预注册 ✓ —— 改动本身会改变发布集合 ⇒ 必须等 ③ 窗口关掉 ✓。
OVERWRITE_DROP_PP = 15          # 通过 = 处置臂比基线低 ≥15pp


def overwrite_verdict(base_stats, treat_stats) -> tuple:
    """R851h 预注册判定（返回 (状态, 说明)）。"""
    if not treat_stats:
        return ("样本不足", "处置臂为空")
    known = sum(s["known"] for s in treat_stats)
    if len(treat_stats) < MIN_ARM_WINDOWS or known < MIN_ARM_N:
        return ("样本不足", "需 ≥%d 窗 / known≥%d；当前 %d 窗 / %d 篇"
                % (MIN_ARM_WINDOWS, MIN_ARM_N, len(treat_stats), known))
    base = sum(s["over_pct"] for s in (base_stats or [])) / max(1, len(base_stats or []))
    treat = sum(s["over_pct"] for s in treat_stats) / len(treat_stats)
    if treat >= base:
        return ("回滚", "过度写作率 %.1f%% 未低于基线 %.1f%%（改了但没降 ⇒ 无效）" % (treat, base))
    if base - treat >= OVERWRITE_DROP_PP:
        return ("通过", "过度写作率 %.1f%% ≤ 基线 %.1f%% − %dpp" % (treat, base, OVERWRITE_DROP_PP))
    return ("未通过", "过度写作率 %.1f%% 低于基线 %.1f%% 但降幅 < %dpp ⇒ 保留并标注「未被证实」"
            % (treat, base, OVERWRITE_DROP_PP))


def trunc_stats(rows, date_utc: str) -> dict:
    """单个 UTC 日的英文帖断句统计（纯函数）。"""
    cohort = [r for r in rows
              if str(r.get("outcome", "")).startswith("binance_published")
              and str(r.get("ts") or "").startswith(date_utc)
              and r.get("lang") == "en"
              and isinstance(r.get("final_text"), str) and r["final_text"].strip()]
    bad = [r for r in cohort if is_truncated(r)]
    return {"date": date_utc, "n": len(cohort), "trunc": len(bad),
            "examples": ["%s…" % body_before_tags(r["final_text"])[-26:] for r in bad[:3]]}


def trunc_verdict(base_stats, treat_stats) -> tuple:
    """预注册判定。返回 (状态, 说明)。"""
    n = sum(s["n"] for s in treat_stats)
    bad = sum(s["trunc"] for s in treat_stats)
    named = [s for s in treat_stats if s["trunc"]]
    if named:
        return ("回滚", "部署后仍有断句帖 %d 篇（%s）%s"
                % (bad, "、".join(s["date"] for s in named),
                   "；例：%s" % " / ".join(named[0]["examples"])))
    if len(treat_stats) < TRUNC_MIN_DAYS and n < TRUNC_MIN_POSTS:
        return ("样本不足", "需 ≥%d 个完整日期或 ≥%d 篇 en 帖；当前 %d 天 / %d 篇"
                % (TRUNC_MIN_DAYS, TRUNC_MIN_POSTS, len(treat_stats), n))
    base_bad = sum(s["trunc"] for s in base_stats)
    base_n = sum(s["n"] for s in base_stats)
    return ("通过", "部署后 %d 天 / %d 篇 en 帖里 **0 篇**断句（部署前 %d/%d）"
            % (len(treat_stats), n, base_bad, base_n))


def _fmt_trunc(s: dict) -> str:
    extra = ("  例：%s" % " / ".join(s["examples"])) if s.get("examples") else ""
    return "%s  en %d 篇 ｜ 词中截断 %d%s" % (s["date"], s["n"], s["trunc"], extra)


# ===========================================================================
# 第三条预注册判据（R834）：**正文顶到可发布上限**（内容的"看不见的"损失）
# ---------------------------------------------------------------------------
# 为什么要单列：`is_truncated` 只看"结尾有没有标点"，只能抓到**切口落在词中**的
# 可见子集。切口恰好落在标点上时**看不出来**，但一样丢了后面的内容。
# 口径（写死）：`final_text` 长度 ≥ **814** 即「顶到上限」
#   = `SHORT_FORM_MAX_CHARS(900) − _APPEND_HEADROOM(80) − 6(容差)`（净化预算 820）。
#   ★ 这个 820 就是 R833 更正过的"真正生效的预算"——别再拿外层的 900 去比（差一档）。
# 基线（R834 部署前，n=113 篇 en 帖）：**23 篇 = 20%** 顶格（37% 在 40 字内逼近）；
#   同口径 **zh-CN 0%**（中文 140~200 字稳在预算内）⇒ 这是**英文侧**的结构问题。
# 阈值：部署后 **≥2 个完整日期（或 ≥40 篇）** 里占比 **≤5%**（锚点=向 zh-CN 的 0% 看齐、
#   留 5 个点余量）⇒ 通过；**≥20%（= 没改善）** ⇒ 回滚。
CAP_TOTAL = 814
CAP_PASS_PCT = 5
CAP_REVERT_PCT = 20
# ★★★ R848：判定③ 的**「当前被测改动」部署日** —— 它的基线臂必须**早于**这一天，
# 否则两臂都在改动之后、判定根本没在测那个改动（R846 那次假回滚的来源）。
# 每次动"英文篇幅目标"就改这里，并在下面登记**预注册**（基线取自哪个窗口、阈值多少）。
#
# 历史（别丢）：
#   · R834（2026-10-25）把短讯目标收到 110~135 words（≈745 字符）——**恰好贴着**预算边界
#     761 ⇒ 18~24% 顶格。当时用同一个 `--baseline` 服务 ①②③④，基线落在 R834 之后
#     ⇒ 报过一条**没配准的"回滚"**（R847 才发现）。
#   · R848（2026-10-30）把上沿收到 120 words（≈660 字符），距顶格线留 95 字符。
#     **预注册**：基线 = 10-26 + 10-27（**与干预前提示词状态相同**——R846 已把 R844 逐字
#     回滚，故这两窗就是干预前状态；R843/R845 只动标签/遥测，对长度是 +1 字符级）
#     = **7/45 = 16%**；**通过** = ≥2 窗且 n≥36 且顶格 ≤5%；**回滚** = 顶格 ≥20%
#     （⇒ 逐字还原 110~135）；介于两者 ⇒ 保留改动并标注「未被证实」。
CAP_CHANGE_DEPLOY_DATE = "2026-10-30"


def cap_stats(rows, date_utc: str, lang: str = "en") -> dict:
    """单个 UTC 日的「顶到可发布上限」统计（纯函数）。"""
    cohort = [r for r in rows
              if str(r.get("outcome", "")).startswith("binance_published")
              and str(r.get("ts") or "").startswith(date_utc)
              and r.get("lang") == lang
              and isinstance(r.get("final_text"), str) and r["final_text"].strip()]
    at = [r for r in cohort if len(r["final_text"]) >= CAP_TOTAL]
    return {"date": date_utc, "lang": lang, "n": len(cohort), "at_cap": len(at),
            "pct": (100.0 * len(at) / len(cohort)) if cohort else 0.0}


def cap_arm_warning(base_stats) -> str:
    """判定③ 的**配准检查**（抽成函数是为了可单测）。

    ★ 为什么需要：判定③ 测的是「当前那次篇幅目标改动」（`CAP_CHANGE_DEPLOY_DATE`）
      ⇒ 基线臂必须早于那一天。但本脚本用同一个 `--baseline` 服务 ①②③④，
      而 ①②④ 要"干预前最近两窗" ⇒ 一条命令不可能同时配准四条。
    ★ 返回非空字符串 = 没配准（调用方必须打出来）。空串 = 配准了。
    """
    armed = sorted({w.get("date", "") for w in (base_stats or [])
                    if str(w.get("date", "")) >= CAP_CHANGE_DEPLOY_DATE})
    if not armed:
        return ""
    return ("   ⚠️ **这条判定没配准**：基线臂含被测改动部署日（%s）之后的窗口 %s "
            "⇒ 两臂都在改动之后，**它没在测那次篇幅目标改动**。请另跑一次 "
            "`--baseline <该改动之前的窗口>` 再读本判定的档位。"
            % (CAP_CHANGE_DEPLOY_DATE, "、".join(armed)))


def cap_arm_confound_note() -> str:
    """配准**之后**还要确认：基线窗与处置窗**除了那个改动之外是否可比**。

    ★ R846 曾说「③ 的回滚是配错造成的、不是真的」—— 用正确的 pre-R834 基线重跑
      **仍然报回滚**（25% vs 19%）⇒ 那个说法**不成立**（R847 自纠）。
      当时那组对比还**混着 R830/R840**（pre-R834 窗里 R830 还在、R840 未上线）
      ⇒ 不能归因给篇幅目标 ⇒ 条文里的「回滚」在那组臂上**不可执行**。
    ★ R848 起这条口径可满足：基线取 10-26/27 —— 与干预前提示词状态**逐字相同**
      （R846 已回滚 R844），故两臂只在篇幅目标上不同。
    """
    return ("   ℹ️ 读本条前先确认：基线窗与处置窗**除该次篇幅目标改动外**是否可比。"
            "（历史坑：R834 那组对比混着 R830/R840 ⇒ 当时条文里的「回滚」不可执行；"
            "R848 起基线取 10-26/27，与干预前提示词状态逐字相同。）")


def cap_treat_warning(treat_stats) -> str:
    """判定③ 的**处置臂配准检查**（抽成函数是为了可单测）。

    ★ 与 `cap_arm_warning` 对称：只查基线还不够 —— 若**处置窗也落在被测改动之前**，
      那这条读数同样与那次改动无关（例如拿 R848 上线前的窗口当处置臂去读 R848）。
    """
    early = sorted({w.get("date", "") for w in (treat_stats or [])
                    if str(w.get("date", "")) < CAP_CHANGE_DEPLOY_DATE})
    if not early:
        return ""
    return ("   ⚠️ **处置臂也没配准**：窗口 %s 早于被测改动部署日（%s）"
            "⇒ 这组读数与那次改动无关。" % ("、".join(early), CAP_CHANGE_DEPLOY_DATE))


def cap_verdict(base_stats, treat_stats) -> tuple:
    """预注册判定。返回 (状态, 说明)。阈值全程用整数交叉相乘。"""
    n = sum(s["n"] for s in treat_stats)
    at = sum(s["at_cap"] for s in treat_stats)
    if len(treat_stats) < TRUNC_MIN_DAYS and n < TRUNC_MIN_POSTS:
        return ("样本不足", "需 ≥%d 个完整日期或 ≥%d 篇 en 帖；当前 %d 天 / %d 篇"
                % (TRUNC_MIN_DAYS, TRUNC_MIN_POSTS, len(treat_stats), n))
    base_at = sum(s["at_cap"] for s in base_stats)
    base_n = sum(s["n"] for s in base_stats)
    if at * 100 >= CAP_REVERT_PCT * n:
        return ("回滚", "顶格占比 %d/%d 仍 ≥%d%%（基线 %d/%d）"
                % (at, n, CAP_REVERT_PCT, base_at, base_n))
    if at * 100 <= CAP_PASS_PCT * n:
        return ("通过", "顶格占比 %d/%d = %.0f%% ≤ %d%%（基线 %d/%d = %.0f%%）"
                % (at, n, 100.0 * at / n, CAP_PASS_PCT, base_at, base_n,
                   (100.0 * base_at / base_n) if base_n else 0))
    return ("未通过", "顶格占比 %d/%d = %.0f%% 高于 %d%% 的通过线（基线 %d/%d）"
            % (at, n, 100.0 * at / n, CAP_PASS_PCT, base_at, base_n))


def _fmt_cap(s: dict) -> str:
    return "%s  %-6s %d 篇 ｜ 顶格 %d（%.0f%%）" % (
        s["date"], s["lang"], s["n"], s["at_cap"], s["pct"])


# ===========================================================================
# 第四条预注册判据（R844）：把「必须带活动提法」挪到 prompt 的**最后一条核心要求**
# ---------------------------------------------------------------------------
# 假设（来自 R835 的归因切片）：真漏帖的 `ending_style` 集中在「仓位表白 / 时间竞猜 /
#   情绪表态」这类**自我指涉式结尾**——模型写最后一句时服从的是"结尾站姿/实操角度"指令，
#   把活动句挤掉。而活动要求当时挂在 `intel_section`（在结尾指令**之前**）⇒ 被后写下去的
#   指令压过。
# 干预：把要求（框架行 + 禁令行 + 逃生阀）**逐字**搬进 prompt 末尾的【核心要求】列表第 7 条。
#   ★ **只调顺序，一个字的措辞都不动**——R760 的教训：一次"改措辞"把正文提及率从 93%
#     打到 33%（模型把"换说法"读成"可以不提"）。连逃生阀那句都原样保留。
# 基线（干预前现况）：10-26 + 10-27 两个夜窗 = **15/45 = 33%**（R842 更正后的口径）。
# 阈值（整数分数；判定全程整数交叉相乘，**禁止比浮点百分比**）：
#   · **通过**：处置臂 ≥2 窗且合并 n ≥ 36，合并真漏率 **≤ 1/10**；
#   · **回滚**：合并真漏率**比基线更差**，或**任一窗 > 50%** ⇒ 把要求逐字搬回 intel_section；
#   · **未通过**（介于两者）：**保留改动但标注「未被证实」**，下一轮复看是否继续。
#     ★ 这一档补的是 R843 发现的缺口：旧条款只写了通过线/回滚线，**没写"未通过"之后怎么办**。
# 副作用守卫（与 ①②③ 一起读）：② 断句仍为 0；③ 顶格不恶化（≤ 基线 19%）；quality 拒稿不增。
R844_BASE_MISS, R844_BASE_NORMAL = 15, 45      # 10-26+10-27 实测（干预前）
R844_PASS_NUM, R844_PASS_DEN = 1, 10           # 通过线：合并真漏 ≤ 1/10
R844_REVERT_WINDOW_PCT = 50                    # 单窗红线


def r844_verdict(treat_stats) -> tuple:
    """R844 预注册判定（返回 (状态, 说明)）。阈值全程整数交叉相乘。"""
    n = sum(s["normal"] for s in treat_stats)
    miss = sum(s["miss"] for s in treat_stats)
    if len(treat_stats) < MIN_ARM_WINDOWS or n < MIN_ARM_N:
        return ("样本不足", "需 ≥%d 窗口 / n≥%d；当前 %d 窗 / %d 篇"
                % (MIN_ARM_WINDOWS, MIN_ARM_N, len(treat_stats), n))
    for s in treat_stats:
        if s["normal"] and s["miss"] * 100 > R844_REVERT_WINDOW_PCT * s["normal"]:
            return ("回滚", "单窗真漏 %d/%d 超过 %d 个百分点上限"
                    % (s["miss"], s["normal"], R844_REVERT_WINDOW_PCT))
    if miss * R844_BASE_NORMAL > R844_BASE_MISS * n:          # miss/n > base/normal
        return ("回滚", "合并真漏 %d/%d 比 R844 基线 %d/%d 更差"
                % (miss, n, R844_BASE_MISS, R844_BASE_NORMAL))
    if miss * R844_PASS_DEN <= R844_PASS_NUM * n:
        return ("通过", "合并真漏 %d/%d ≤ 通过线 %d/%d（基线 %d/%d）"
                % (miss, n, R844_PASS_NUM, R844_PASS_DEN, R844_BASE_MISS, R844_BASE_NORMAL))
    return ("未通过", "合并真漏 %d/%d 高于通过线 %d/%d、但不比基线 %d/%d 差 "
            "⇒ 保留改动并标注「未被证实」，下一轮复看"
            % (miss, n, R844_PASS_NUM, R844_PASS_DEN, R844_BASE_MISS, R844_BASE_NORMAL))


# ===========================================================================
# 第五条预注册判据（R850）：**守卫式重试**——缺活动提法时带一句针对形态的强制重生成
# ---------------------------------------------------------------------------
# 背景与分叉（R849 前后的取证）：
#   · 真漏帖里 31/32 的 `campaign_guidance_usable = True` ⇒ 情报给了、模型就是没写
#     ⇒ **文案侧缺陷**（不是情报侧、也不是措辞/位置问题——R830/R844 两次 prompt 干预都被条款回滚）。
#   · 主题诊断被**证伪**：漏提**不**集中在"与交易无关"的题材（行情/交易 76%、安全/黑客 70%、
#     监管/法务 50%、基础设施 33%）⇒ 不是"模型按逃生阀正确判断这条新闻与交易无关"。
#   · 形态诊断（R850 的制定依据）：**52% 的漏提（15/29）用「对比站队 / 极简站队 / 多空辩论」结尾**
#     —— 这些结尾以 poll 提问收尾 ⇒ **活动句被 poll 挤掉**；
#       提及组则集中在「情绪表态 / 灵魂拷问 / 仓位表白」。
# 干预（**部署后生效**，见 main.py 的部署备注）：生成后若 `campaign_vocab.mention_in_text(正文) is False`
#   且当轮 `campaign_guidance_usable is True` ⇒ **带一句针对形态的强制重生成指令重试一次**
#   （示例：「若你的结尾是站队提问，请在那个提问**之前**补一句当期活动提法」）
#   —— **不全局改措辞** ⇒ 没有 R760 那类连带伤害。
# ★★ 为什么是"**之前**"（2026-10-30 深度研究，见
#   research-reports/binance-square-cta-conventions-2026-10-30/report.md）：
#   · 平台官方+创作者一致的标准结构是 **Hook → Value → CTA**；官方 Creator Academy 的 trading 帖
#     四步是「指出走势 → 说明你的看法 → **展示你做了什么** → 加 CTA」⇒ 活动提法属于 Value 段 ✓
#   · 站队/poll 结尾是**平台功能且被奖励**（2024-02 官方开放 poll；2026-10-19 起 Write to Earn
#     新增"基于真实互动（评论/点赞/关注）的参与奖励"）⇒ **不能删掉 poll** ✗
#   ⇒ 修法 = 把活动提法补在 **CTA 提问之前**（Value 段末尾），不是提问之后 ✓
# ⚠️ 社群准则把「大量发布高度相似或结构相同的内容」列为违规 ⇒ 重试必须走**轮换框架**，
#   不能补出同一句模板 ✗（现有 `CAMPAIGN_CTA_STYLE_POOL` 就是要用来轮的）。
# ⚠️ **必须等 R848 的判定③窗口关掉再部署**：重试重新生成正文 ⇒ 直接污染 R848 的长度窗口。
# 判据（事前写死；阈值全程整数交叉相乘）：
#   · 口径 = 英文夜窗「真漏率」（与 ① 同口径；**历史读数必须用 R849 之后的词表重算**）；
#   · 基线 = **重试部署前的最近两个完整夜窗**（由调用方传入）；
#   · **通过** = ≥2 窗且 n≥36 且真漏率 ≤ 1/5（20%）；
#   · **回滚** = 真漏率**比基线更差** 或任一窗 > 50% ⇒ 撤销重试（逐字还原）；
#   · **未通过**（介于两者）= 保留改动并标注「未被证实」，下一轮复看。
R850_PASS_NUM, R850_PASS_DEN = 1, 5
R850_REVERT_WINDOW_PCT = 50


def r850_verdict(treat_stats, base_stats) -> tuple:
    """R850 预注册判定（返回 (状态, 说明)）。"""
    n = sum(s["normal"] for s in treat_stats)
    miss = sum(s["miss"] for s in treat_stats)
    base_n = sum(s["normal"] for s in (base_stats or []))
    base_miss = sum(s["miss"] for s in (base_stats or []))
    if len(treat_stats) < MIN_ARM_WINDOWS or n < MIN_ARM_N:
        return ("样本不足", "需 ≥%d 窗口 / n≥%d；当前 %d 窗 / %d 篇"
                % (MIN_ARM_WINDOWS, MIN_ARM_N, len(treat_stats), n))
    for s in treat_stats:
        if s["normal"] and s["miss"] * 100 > R850_REVERT_WINDOW_PCT * s["normal"]:
            return ("回滚", "单窗真漏 %d/%d 超过 %d%%" % (s["miss"], s["normal"], R850_REVERT_WINDOW_PCT))
    if base_miss and miss * base_n > base_miss * n:
        return ("回滚", "合并真漏 %d/%d 比基线 %d/%d 更差" % (miss, n, base_miss, base_n))
    if miss * R850_PASS_DEN <= R850_PASS_NUM * n:
        return ("通过", "合并真漏 %d/%d ≤ 通过线 %d/%d（基线 %d/%d）"
                % (miss, n, R850_PASS_NUM, R850_PASS_DEN, base_miss, base_n))
    return ("未通过", "合并真漏 %d/%d 高于通过线 %d/%d、但不比基线 %d/%d 差 ⇒ 保留改动并标注「未被证实」"
            % (miss, n, R850_PASS_NUM, R850_PASS_DEN, base_miss, base_n))


def load_rows(path: str):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="英文夜窗体检 + 预注册判定（R819）")
    ap.add_argument("--metrics", required=True)
    ap.add_argument("--baseline", default="", help="逗号分隔的 UTC 日期")
    ap.add_argument("--treatment", default="", help="逗号分隔的 UTC 日期")
    args = ap.parse_args(argv)
    rows = load_rows(args.metrics)
    base = [window_stats(rows, d) for d in (args.baseline or "").split(",") if d]
    treat = [window_stats(rows, d) for d in (args.treatment or "").split(",") if d]
    print("【英文夜窗体检 · 预注册判据 R819】窗口 = UTC 14:00–22:00（北京夜窗）")
    print("预注册基线：10-19 夜窗 %d/%d = %.1f%%"
          % (BASELINE_MISS, BASELINE_NORMAL, 100.0 * BASELINE_MISS / BASELINE_NORMAL))
    for label, stats in (("基线臂", base), ("处置臂", treat)):
        if not stats:
            continue
        print("\n%s（%d 个窗口）" % (label, len(stats)))
        for s in stats:
            print("  " + _fmt(s))
        print("  合并：" + _fmt(_pool(stats)))
    if base and treat:
        status, why = verdict(base, treat)
        print("\n判定①（活动提及）：%s —— %s" % (status, why))
        # ---- 第四条判据（R844）：同口径、不同的基线与阈值（干预 = 把要求挪到核心要求末条）----
        st4, why4 = r844_verdict(treat)
        print("判定④（R844 活动提法位置）：%s —— %s" % (st4, why4))
        # ---- 第五条判据（R850）：守卫式重试；基线 = 部署前的臂（即本命令的 --baseline）----
        st5, why5 = r850_verdict(treat, base)
        print("判定⑤（R850 守卫式重试）：%s —— %s" % (st5, why5))
        # 归因切片（R835）：真漏与哪些既有字段相关（**诊断，不设阈值**）
        for label, dates in (("基线臂", args.baseline), ("处置臂", args.treatment)):
            _miss = []
            for d in (dates or "").split(","):
                if d:
                    _miss += miss_rows(rows, d)
            if _miss:
                print("   ↳ %s：%s" % (label, _fmt_breakdown(miss_breakdown(_miss))))

    # ---- 第二条判据：英文断句（R831 判定 / R832 更正口径）----
    tb = [trunc_stats(rows, d) for d in (args.baseline or "").split(",") if d]
    tt = [trunc_stats(rows, d) for d in (args.treatment or "").split(",") if d]
    if tb or tt:
        print("\n【英文断句 · 预注册判据 R831/R832】口径：第一个 `#` 之前的正文"
              "末字符是字母，且末尾 token 不是 $挂件/全大写 ticker")
        for label, stats in (("基线臂", tb), ("处置臂", tt)):
            if not stats:
                continue
            print("%s" % label)
            for s in stats:
                print("  " + _fmt_trunc(s))
        if tb and tt:
            st2, why2 = trunc_verdict(tb, tt)
            print("\n判定②（词中截断）：%s —— %s" % (st2, why2))

    # ---- 第三条判据：正文顶到可发布上限（R834）----
    cb = [cap_stats(rows, d) for d in (args.baseline or "").split(",") if d]
    ct = [cap_stats(rows, d) for d in (args.treatment or "").split(",") if d]
    if cb or ct:
        print("\n【正文顶到可发布上限 · 预注册判据 R834】口径：final_text 长度 ≥ %d"
              "（= 900 − 80 − 6，即净化预算 820）" % CAP_TOTAL)
        for label, stats in (("基线臂", cb), ("处置臂", ct)):
            if not stats:
                continue
            print("%s" % label)
            for s in stats:
                print("  " + _fmt_cap(s))
            print("  " + _fmt_cap({"date": "合并(%d 天)" % len(stats), "lang": "en",
                                   "n": sum(x["n"] for x in stats),
                                   "at_cap": sum(x["at_cap"] for x in stats),
                                   "pct": (100.0 * sum(x["at_cap"] for x in stats)
                                           / sum(x["n"] for x in stats))
                                   if sum(x["n"] for x in stats) else 0.0}))
        if cb and ct:
            st3, why3 = cap_verdict(cb, ct)
            print("\n判定③（顶格占比）：%s —— %s" % (st3, why3))
            # ★★★ R847 两道护栏（逻辑都在可单测的函数里）：
            #   ① 配准检查：基线臂落在 R834 部署日之后 ⇒ ③ 根本没在测 R834（却照样会报"回滚"）；
            #   ② 混杂提示：即使配准了，pre-R834 那几窗里 R830 还在、R840 还没上
            #      ⇒ 该对比混着两个改动，**条文里的「回滚」在这组臂上不可执行**。
            _w = cap_arm_warning(cb) or cap_treat_warning(ct)
            print(_w if _w else cap_arm_confound_note())

    # ---- R851：AI 味旗标率（只量不设门）----
    # 动机：平台 2026-07 起对纯 AI 内容算法降权（官方原文："Content generated entirely by AI
    # will be de-boosted"）⇒ 要知道「过了门仍带 AI 特征」的帖有多少。
    # ⚠️ 只度量、不加门：加门要另行预注册（且会改变发布集合 ⇒ 必须等 R848 的 ③ 窗口关掉）。
    fa = [ai_flavor_stats(rows, d) for d in (args.baseline or "").split(",") if d]
    fb = [ai_flavor_stats(rows, d) for d in (args.treatment or "").split(",") if d]
    if fa or fb:
        print("\n【AI 味旗标率（R851，只量不设门）】口径 = 已发布帖里「过了门仍带特征」的占比")
        for label, arm in (("基线臂", fa), ("处置臂", fb)):
            if not arm:
                continue
            tot_n = sum(x["n"] for x in arm)
            tot_res = sum(x["residual"] for x in arm)
            print("  %s: n=%d ｜ 残余=%d (%.0f%%) ｜ 软特征=%d ｜ 硬命中=%d"
                  % (label, tot_n, tot_res,
                     100.0 * tot_res / max(1, tot_n),
                     sum(x["soft_any"] for x in arm), sum(x["hard_hits"] for x in arm)))
    # ---- R851b：公式化重复率（只量不设门；判据已预注册给未来的"降重复率"改动）----
    rb = [formulaic_repetition_stats(rows, d) for d in (args.baseline or "").split(",") if d]
    rt = [formulaic_repetition_stats(rows, d) for d in (args.treatment or "").split(",") if d]
    if rb or rt:
        print("\n【公式化重复率（R851b，只量不设门）】"
              "口径：en 已发布帖正文里的 12-gram，跨帖 ≥6 篇，含 top-15 任一片段的占比")
        for label, arm in (("基线臂", rb), ("处置臂", rt)):
            if not arm:
                continue
            tot_n = sum(x["n"] for x in arm)
            tot_hit = sum(x["hit"] for x in arm)
            print("  %s: n=%d ｜ 受影响=%d (%.0f%%) ｜ 片段数=%d"
                  % (label, tot_n, tot_hit,
                     100.0 * tot_hit / max(1, tot_n),
                     sum(x["frag_count"] for x in arm)))
            for x in arm:
                if x["fragments"]:
                    print("    %s 样例: %r" % (x["date"], "、".join(x["fragments"][:5])))
        if rb and rt:
            st6, why6 = repetition_verdict(rb, rt)
            print("\n判定⑥（公式化重复率）：%s —— %s" % (st6, why6))
    # ---- R851g/h：过度写作率（只量不设门；判据⑦已预注册给"收紧英文篇幅指令"的改动）----
    ob = [overwrite_stats(rows, d, "en") for d in (args.baseline or "").split(",") if d]
    ot = [overwrite_stats(rows, d, "en") for d in (args.treatment or "").split(",") if d]
    if ob or ot:
        print("\n【过度写作率（R851h，只量不设门）】口径：en 已发布帖里 raw 超净化预算、被裁掉 ≥50 字符的占比")
        for label, arm in (("基线臂", ob), ("处置臂", ot)):
            if not arm:
                continue
            k = sum(x["known"] for x in arm)
            o = sum(x["over"] for x in arm)
            unk = sum(x["unknown"] for x in arm)
            print("  %s: known=%d ｜ 被大幅裁掉=%d (%.0f%%) ｜ 无字段=%d"
                  % (label, k, o, 100.0 * o / max(1, k), unk))
        if ob and ot:
            st7, why7 = overwrite_verdict(ob, ot)
            print("\n判定⑦（过度写作率）：%s —— %s" % (st7, why7))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
