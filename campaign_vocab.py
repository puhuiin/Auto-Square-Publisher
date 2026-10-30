#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★★ R814：活动提及判据词表（**全仓唯一一份实现**）。

为什么拆出独立模块：`main.py`（发布时计算 `campaign_mention` 遥测）与
`scripts/metrics_report.py`（日报⑥雷达）原先各有一份判据——报表侧只能读发布时
写死的标记，于是每次词表升级（R774/R787/R801/R805）都会让雷达被**旧行**压低约
24h：R805 生产实证，真实提及率 ~90%，面板却报 70%（3 篇 False 是 R805 部署前
的旧标记，用新词表复算全部命中）。`$cashtag`/活动提及是返佣生命线雷达，
不能被词表升级拖累一天。

口径只有一份实现（R731 纪律）：两侧都 `from campaign_vocab import ...`。
本模块**零副作用**（只 import re），可被脚本/测试/主模块安全 import。
"""
import re

# 判据词表：正文（去标签后）出现任一形态即算「提及当期活动」。
# 形态全部来自生产实测（注释里的日期是实证出处），不 Provisions 未观测变体。
CAMPAIGN_MENTION_RE = re.compile(
    r"锦标赛|竞赛|競賽|錦標賽|交易大賽|交易大赛|交易賽事|交易赛事"
    r"|交易赛|交易賽|大赛|大賽|contest|tournament"
    # R774：英文战役提法形态——实测英文帖不提 tournament/contest 这些词，
    # 而是用**产品名/奖励机制**指代活动（tail_conflict 留存的结尾原文实录：
    # "prize pools are still live for active traders"、"new zero-fee RWUSD
    # trading pairs are live to practice on"、"Trading Tournament Season 6 FF
    # for practice rewards"）⇒ 旧正则下英文帖 mention 恒为 False（R766/R768
    # 同族：判据只覆盖一种语种的真实形态）。
    # ⚠️ 精度取舍（已知）："154,000 USDD total reward pool" 这类**非本期活动**
    # 的奖励池会误报——该字段是趋势度量不是门，误报率 ~1/12 可接受；
    # 若未来要当门用，须改为「奖励池 + 币安/官方语境」的双条件。
    r"|write\s*2\s*earn|write to earn|prize pool|reward pool"
    r"|zero[- ]fee|season [0-9]|S[0-9](?:/S[0-9])?"
    r"|bStocks|TraFi|P2P anniversary|LoL (?:worlds|championship)"
    r"|trading (?:tournament|championship|competition|event|race)"
    # ★★ R787：**中文写法的 Write2Earn**（写2赚/寫2賺）——R774 补英文形态时
    #   漏了中文形态（R615/R611 同族：一侧修了另一侧没对齐，只不过这次是
    #   「英中两侧」而非「两个分支」）。生产实证（R784 全文遥测首篇复盘）：
    #   10-19 04:24 帖正文「想边看边练手的，去**写2赚**评论区互动拿奖励就行」
    #   ——通篇在讲当期活动机制，campaign_mention 却判 False（该帖无锦标赛/
    #   竞赛字样，唯一的活动指代就是写2赚）。中文帖占产出 ~70%，这个漏检
    #   让「正文活动提及率」对一类真实提及系统性低估。
    r"|写\s*2\s*[赚賺]|寫\s*2\s*[赚賺]"
    # ★★ R805：新话术「Community Voice / 评论互动领奖励」——R774/R787/R801 同族
    #   第四次（判据词表总是落后模型的新提法）。生产实证（10-20 中文窗口）：
    #   3 篇 campaign_mention=False 的帖正文其实都提了活动——
    #     「去廣場評論互動領獎勵比瞎操作穩」
    #     「參加 Community Voice 評論互動拿獎勵」
    #     「去币安广场 Community Voice 投个票,领点奖励」
    #   ⇒ 中文提及率被系统性低估，日报把「中文 100%」报成 zh-TW 1/3。
    #   精度取舍同 R774：这是趋势度量不是门，个别误报可接受。
    r"|community\s*voice"
    r"|评论互动|評論互動"
    r"|(?:广场|廣場)[^。！？\n]{0,16}(?:投[^。！？\n]{0,3}票|评论|評論|互动|互動)"
    r"[^。！？\n]{0,16}(?:奖励|獎勵)"
    # ★★ R828：**第 5 次词表补漏**——英文帖用 `trading challenge` 指代当期活动。
    #   生产实证（10-20 19:43 英文夜窗，全文遥测）：正文原句
    #   「If you're trading alts this week, join **Binance Square's current trading
    #   challenge** for a shot at bonus rewards while you wait for confirmation.」
    #   ——这是标准的活动导流句，但旧词表只有 tournament/championship/competition/
    #   event/race，没有 challenge ⇒ 判 False，把一篇**真提了活动**的帖算进"真漏"，
    #   直接把英文夜窗真漏率从 9/23 抬到 10/23。
    #   ⚠️ 判据要窄：裸 `challenge` 会误伤「price challenge/technical challenge」
    #   这类行情措辞，故只收 `trading challenge` 与 `challenge` 紧邻活动语境的形态。
    r"|trading\s+challenge"
    r"|challenge[^.!?\n]{0,24}(?:reward|prize|bonus|earn)"
    r"|(?:join|enter|take part in)[^.!?\n]{0,20}challenge"
    # ★★ R830：**第 6 次词表补漏**——当期活动的**入口名**换了一茬：Season 7/8
    #   之外还多了四个模型真在用的入口，而旧词表只认 tournament/season/challenge。
    #   生产实证（近 40 篇我们自己的帖，正则实测频次）：`Binance Pick` ×4、
    #   `Qafila 2026` ×2、`RWUSD`（以"练手场地"出现）、`$FF vouchers`。
    #   注：`Alpha DNA 交易竞赛` 已被 `交易竞赛`/`競賽` 兜住，故不收裸 `Alpha DNA`。
    #   ⚠️ 一律**窄形态**（产品全名/固定专名），不收裸 pick / 裸 reward / 裸 ff。
    r"|binance\s+pick|qafila|word\s*of\s*the\s*day|rwusd|ff\s*voucher"
    # ★★ R831：**第 7 次补漏，这次是拼写错误**——R774 写进去的是 `TraFi`（少一个 d），
    #   而模型真在写的是 `TradFi USDS-Margined 永续合约`（把 TradFi 当练手场地），
    #   `trafi` 匹配不到 `TradFi` ⇒ 又是一条静默假阴性。
    #   生产实证（10-23 前后 40 篇，正则实测）：`TradFi USDS-Margined` ×1、
    #   `Binance Starter Carnival` / `新手嘉年华`（新一期的活动入口名）。
    #   ⚠️ 保留 `TraFi` 不删（历史帖/别处可能真写过这个拼法），两种都收。
    r"|trad?fi|starter\s+carnival|新手嘉年华|新手嘉年華"
    # ★★ R841：**第 8 次补漏**——当期活动名换成「**万圣节南瓜狂欢 / Halloween**」，
    #   而词表里既没有「万圣节/萬聖節/Halloween」，也没有中文分支实际在用的
    #   「**交易活动/交易活動**」（旧表只有「交易赛/交易賽/大赛」，那是**上一期**的写法）。
    #   证据（用户 10-27 粘贴的**已发布**正文，逐条探针）：7/19 漏——
    #     「想去参加币安**万圣节交易活动**」→ 漏 ｜ 「**萬聖節**活動剩不到一週」→ 漏
    #     「**Halloween** trading carnival lets you practice」→ 漏
    #     「顺便去**万圣节交易活动**练手」→ 漏
    #   而「万圣节交易**赛**」命中（靠旧表的「交易赛」兜住）⇒ 漏的正是**新活动名与新措辞**。
    #   ⚠️ 精度取舍同 R774/R805：这是**趋势度量不是门**；「交易活动」可能出现在
    #   「交易活动频繁」这类泛化语境里（误报面已知、可接受）。不收裸「活动」——
    #   太泛（「市场活动」「链上活动」都会命中）。
    r"|万圣节|萬聖節|halloween|交易活动|交易活動"
    # ★★ R842：**第 9 次补漏**——当期活动名是「**$AXIS Alpha 交易竞赛**」（Binance
    #   Alpha 系列的代币交易竞赛），模型在正文里写成 `AXIS Alpha comp/contest/
    #   competition/竞赛`，而旧表只认 `trading (tournament|championship|competition|
    #   event|race)`——「**Alpha + 活动名词**」这一层完全没覆盖（`trading comp`
    #   也漏，`comp` 不是 `competition`）。
    #   ★ 证据①（**实测词频**，AXIS 情报窗口 10-26T13:20~10-27T01:40 的 29 篇
    #     我们自己的已发布帖）：`AXIS Alpha`（专名）**19/29** ｜ `Alpha…contest` 7 ｜
    #     中文`竞赛` 5 ｜ `Alpha…comp` 3 ｜ `Alpha…competition` 2 ｜
    #     裸 `tournament` / 裸 `carnival` **0 篇**（⇒ 不为没观测到的形态加词）。
    #   ★ 证据②（**来源**已查清，不是模型编的）：intel 历史逐版扫描
    #     （`/commits?path=campaign_intel.json` + 逐版取 blob），只有两版出现过
    #     AXIS，其中 10-26T13:45 那版 strategy_guidance 原文是
    #     「今日最紧迫的为今日截止的 **$AXIS Alpha交易竞赛**」，同版
    #     `incentivized_tokens` 含 `$AXIS`——而 AXIS 帖的时间跨
    #     （10-26T13:25 → 10-27T00:31）与之**完全对齐**。
    #   ⇒ 后果：旧表把 **4 篇真提了活动的帖**判成「真漏」，把 10-26 夜窗真漏率
    #     从真值 **8/22(36%)** 虚高成 **12/22(55%)**，直接触发判定① 的相对红线
    #     （虚报 +22pp）——**差点按一个假数回滚**。
    #   ⚠️ 精度取舍（三条都是实测反例换来的）：
    #     ① **不收裸 `alpha`**——alpha=超额收益，是行情高频词；必须与活动名词紧邻。
    #     ② **不收裸 `comp`**（`comp` 从不在表里，本轮也不加）。
    #     ③ ⚠️⚠️ **更正一处随口断言（本轮自己先写错、被测试咬住）**：R842 初稿注释写
    #        「不收裸 `contest/competition`」——**是错的**：`contest|tournament` 从
    #        R774 起就在表里（裸词，见上文第一段），`competition` 只在 `trading `
    #        后才算。后果是一条**既有**误报面：`#QNT TradingContest` 这类**畸形
    #        标签**（标签内混空格），`strip_tags` 只吃掉 `#QNT`、把 `TradingContest`
    #        留在正文 ⇒ 命中裸 `contest` ⇒ **保底标签替正文背了提及率**（R291 同族）。
    #        这不是 R842 引入的，修它要动 `strip_tags`（＝动口径），须单开一轮并先
    #        写预注册判据；本轮只在测试里**锁住现状**（见
    #        `TestR842AlphaVocab::test_known_pre_existing_false_positive_face`）。
    r"|axis\s+alpha"
    r"|alpha\s+(?:trading\s+)?(?:comp|contest|competition|tournament|race|event|carnival)"
    r"|alpha\s*[（(]?\s*(?:交易)?(?:竞赛|競賽|活动|活動)"
    # ★★ R849：**第 10 次补漏**——当期活动的**奖励计划叫法**（`USD1` / `WLFI`）没进表。
    #   取证（10-30 英文夜窗）：**11 篇被判"未提及"的帖里有 6 篇正文明写着**
    #     `…USD1 holder rewards.  Are you holding $BTC through this…`
    #     `…USD1 WLFI pool  …` / `…rewards entry. Official route only…`
    #   而 `usd1`/`wlfi` **不在表里** ⇒ 真漏率被虚报成 11/13 = 85%（真值 **5/13 = 38%**，
    #   与基线 15/45 = 33% 同一水平）⇒ **又一次"词表停在上一期措辞"**（R841/R842 同族）。
    #   ⚠️ **窄形态**（延续 R830/R842 纪律）：`usd1`/`wlfi` **只有在与奖励/活动类名词
    #     相邻时**才算——否则 "$WLFI 现价" 这种**单纯行情提及**会被误判成活动提及
    #     （那是主题标的，不是活动入口，正是 R842 注释里说过的陷阱）。
    r"|(?:usd1|wlfi)[^.!?\n]{0,40}(?:reward|rewards|holder|bonus|prize|yield|pool|campaign|活动|奖励|奖池)"
    r"|(?:reward|rewards|holder|bonus|prize|yield|pool|campaign|活动|奖励|奖池)[^.!?\n]{0,40}(?:usd1|wlfi)"
    # ⚠️ `hold` **不能**放进上面两张通用表：实测会误中「I hold some USD1 in my wallet」✗
    #   ⇒ 改成**方向特定**的形态（生产原句是「the **USD1 hold** campaign」）：
    #   `usd1` **后面紧跟** hold/holding 才算，「hold USD1」这种持仓叙述不算 ✓。
    r"|usd1\s+hold(?:ing)?"
    r"|\busd1\s+wlfi\b|\bwlfi\s+usd1\b",
    re.IGNORECASE)

# 标签形态：注入的 #TradingTournament / #Write2Earn 含 contest/tournament 字样，
# 不先去标签再判会让「活动提及」每帖恒真（把保底标签当活动提及，R291 同族）。
_TAG_RE = re.compile(r"#[^\s#]+")


def strip_tags(text: str) -> str:
    """去掉 #标签 再判正文（判据语义的一部分，两边必须同一份）。"""
    return _TAG_RE.sub(" ", str(text or ""))


def mention_in_text(text) -> "bool | None":
    """正文是否提及当期活动。

    返回 None = 空文本/非字符串（「未知」不是 False，遥测纪律 12）。
    两边调用点必须一致：main.py 发布时写遥测、metrics_report 面板重算。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    return bool(CAMPAIGN_MENTION_RE.search(strip_tags(text)))
