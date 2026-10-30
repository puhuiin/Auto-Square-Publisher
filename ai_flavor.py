# -*- coding: utf-8 -*-
"""★★★ R851：AI 腔扫描的**唯一实现**（流水线拒稿门 + 验收脚本的"旗标率"共用）。

为什么抽出来：此前清单与门逻辑只写在 `SquarePublisher` 里，验收脚本无法量
"**通过了门、却仍带软特征**"的帖占比 —— 而平台 2026-07 起**对纯 AI 内容算法降权**
（官方原文：Content generated entirely by AI will be de-boosted），所以我们需要这个
度量来做风险对冲；若另起一份实现 ⇒ 口径必然漂移（本项目已有多次同族事故）。

语义（与原 `_passes_ai_flavor_gate` 逐字一致，R580/R644 的补充也保留）：
  · **硬命中** = 真人交易员几乎不会写、模型却高频产出的标志性短语，出现即判废；
  · **软特征** = 边界词/结构特征，单中不拦（防误杀），累计 ≥2 才判废；
  · ⚠️ "看多的扣1"式互动问句是 prompt 自身要求，**绝不能进清单**。

来源：Wikipedia "Signs of AI writing" 的中文交易语境移植 + R580 扩容。
"""
import re

AI_FLAVOR_HARD = (
    "拭目以待", "未来可期", "保驾护航", "谱写", "新篇章", "扬帆起航",
    "值得注意的是", "值得一提的是", "综上所述", "总而言之", "让我们一起",
    "毋庸置疑", "不言而喻", "共同见证",
    "由此可见", "众所周知", "简而言之", "简言之", "换言之",
    "一言以蔽之", "备受瞩目",
)
AI_FLAVOR_SOFT = (
    "赋能", "标志着", "显而易见",
    "需要注意的是", "总的来说", "总体而言", "换句话说", "备受关注",
)

_WALL_RE = re.compile(r"一方面[^。！？\n]{0,40}另一方面")
_GRANDIOSE_RE = re.compile(r"随着[^，。！？\n]{2,30}(的)?(不断|持续|逐步|日益)?(发展|推进|深入|普及|到来|升温|铺开|扩大)")
_BU_JIN_RE = re.compile(r"不仅[^。！？\n]{0,24}(更|还|也|而且)")


def _sentence_lengths(text):
    """句长列表（按中/英句末标点切；剔除标签行）。供句长均匀度（CV）使用。"""
    c = text or ""
    body = re.sub(r"#[^\s#]+", " ", c)          # 标签行里的词不参与句长统计
    parts = re.split(r"[。！？.!?\n]+", body)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        out.append(len(p))
    return out


def scan(content):
    """逐条扫描 → 结构化结果（供门与度量共用）。"""
    c = content or ""
    hard = [p for p in AI_FLAVOR_HARD if p in c]
    soft = [w for w in AI_FLAVOR_SOFT if w in c]
    wall = bool(_WALL_RE.search(c))
    grandiose = bool(_GRANDIOSE_RE.search(c))
    bu_jin = bool(_BU_JIN_RE.search(c))
    first_then = ("首先" in c and "其次" in c)
    soft_for_gate = list(soft)
    if bu_jin:
        soft_for_gate.append("不仅…更/还 句式")
    if first_then:
        soft_for_gate.append("首先…其次 结构")
    if grandiose:
        soft_for_gate.append("随着…发展 宏大开场")
    # ★ R851b：**句长均匀度（CV）**——语言无关的 AI 特征。
    # 依据：本项目 prompt 明写「AI 腔最稳的信号就是句长过于均匀」「别清一色中长句，
    # 每段穿插一两句短句制造节奏停顿」⇒ 均匀度高 = 违反了自己的写作规则 ✓。
    # ⚠️ 阈值先**测量再定**（本函数只输出原始分布，不在这里定阈值 ✓）。
    lens = _sentence_lengths(c)
    if len(lens) >= 4:
        mu = sum(lens) / len(lens)
        sd = sum((x - mu) ** 2 for x in lens) / len(lens)
        cv = (sd ** 0.5) / mu if mu else 0.0
    else:
        cv = None
    return {
        "hard": hard, "soft": soft, "wall": wall,
        "bu_jin": bu_jin, "first_then": first_then, "grandiose": grandiose,
        "dash": c.count("——"), "soft_for_gate": soft_for_gate,
        "sent_n": len(lens), "sent_cv": cv,
    }


def gate(content, label="正文"):
    """与原 `_passes_ai_flavor_gate` 逐字同语义：返回 (ok, reason)。"""
    s = scan(content)
    if s["hard"]:
        return False, "%s AI 腔硬命中「%s」（真人交易员不会这么说话）" % (label, s["hard"][0])
    if s["dash"] >= 2:
        return False, "%s AI 腔硬命中：破折号出现 2 次以上（AI 写作最可靠的指纹之一）" % label
    if s["wall"]:
        return False, "%s AI 腔硬命中：一方面…另一方面 骑墙句式（真人交易员有鲜明态度，不和稀泥）" % label
    if len(s["soft_for_gate"]) >= 2:
        return False, "%s AI 腔软特征累计 %d 项: %s" % (label, len(s["soft_for_gate"]),
                                                       "、".join(s["soft_for_gate"][:3]))
    return True, ""


def flag_rate_text(rows):
    """便捷入口：给定 (final_text) 行列表，返回每篇的旗标计数（供验收脚本用）。"""
    out = []
    for r in rows:
        t = r.get("final_text")
        if not isinstance(t, str) or not t.strip():
            continue
        s = scan(t)
        out.append({
            "hard": s["hard"],
            "soft": s["soft"],
            "dash": s["dash"],
            "wall": s["wall"],
            "soft_n": len(s["soft_for_gate"]),
            # 门语义下的"过关但带特征"：任何 soft/grandiose/bu_jin/first_then/dash 单中
            "residual": (bool(s["soft"] or s["grandiose"] or s["bu_jin"] or s["first_then"] or s["dash"])),
        })
    return out
