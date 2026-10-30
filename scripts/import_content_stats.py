#!/usr/bin/env python3
"""内容浏览/互动数据导入（R285：浏览量数据闭环第一块）。

背景：metrics.jsonl 自 R125 起逐帖落 `content_id`（币安帖子永久标识，
注释原话"未来拉互动数据时做 join 键"），但 129 条生产回执里这个键从未被
任何消费面读取——浏览量/点赞/评论在系统里零度量，"哪类帖有流量"只能盲猜。

本脚本补上数据入口的第一块：把人工从币安创作者中心后台导出的 CSV（中文表头）
规整成 `content_stats.jsonl`（与 metrics.jsonl 同目录，按 content_id 去重、
各指标取 max——浏览次数单调递增，重复导出取最新观测），供 metrics_report
做「时段/体裁/来源 × 浏览」相关性归因。

为什么先做 CSV 而不是 API：
- 币安 Square OpenAPI 是否提供内容统计查询接口尚待核实（本机网络无法访问
  binance.com 域名，无法读官方文档），不写未核实端点的投机代码；
- 创作者中心后台本身提供内容数据导出，人工每周导出一次即可启动整个数据循环；
- content_stats.jsonl 的形状与未来的 API 拉取完全一致（同一 join 键），
  届时替换数据来源不改消费面。

用法：
    python scripts/import_content_stats.py [csv路径]
    # 默认 stats/content_stats.csv，也可用环境变量 CONTENT_STATS_CSV 指定
    # 输出固定 <repo>/content_stats.jsonl（与 METRICS_FILE 同目录规则一致）

    python scripts/import_content_stats.py --paste <粘贴文本> [--metrics <线上件>]
    # ★ R820：创作者中心后台的**文本**导出（北京时间 + 浏览量，**没有 content_id**）
    #   走这条分支：按 UTC ±3min 联到 metrics.jsonl 回填 content_id。
    #   见下方 import_pasted_views() 的口径说明。

CSV 表头（中英均可，至少要有 id 列，其余列缺失即跳过该指标）：
    content_id/帖子ID ｜ 浏览量/views/阅读量 ｜ 点赞/likes ｜ 评论/comments
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Dict, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CSV = os.path.join(REPO_ROOT, "stats", "content_stats.csv")
OUT_PATH = os.path.join(REPO_ROOT, "content_stats.jsonl")

# 表头别名（小写比较）——后台导出为中文，代码里用英文键
_ID_KEYS = ("content_id", "contentid", "帖子id", "帖子 id", "帖子编号", "id")
_VIEW_KEYS = ("views", "浏览量", "阅读量", "查看量", "reads", "浏览")
_LIKE_KEYS = ("likes", "点赞", "喜欢", "赞", "点赞数")
_COMMENT_KEYS = ("comments", "评论", "留言", "回复", "评论数")


def _pick(headers, aliases) -> Optional[str]:
    for h in headers:
        if h.strip().lower() in aliases:
            return h
    return None


def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in {"-", "--"}:
        return None
    # R311：币安创作者后台的大数展示/导出用中文数量单位（"1.2万"=12000、"2亿"），
    # 裸 int(float()) 对这类值抛 ValueError → 返 None → **高浏览帖的浏览量被静默丢弃**，
    # 归因分析只剩低浏览帖 = 系统性偏倚（恰与用户"浏览量低怎么办"的诉求相反）。
    # 数字+单位形态先换算；裸整数/千分位逗号走原路径，两者互不影响。
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(万|萬|亿|億)", s)
    if m:
        scale = 1e4 if m.group(2) in ("万", "萬") else 1e8
        return int(float(m.group(1)) * scale)
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def read_csv(path: str) -> Dict[str, Dict[str, int]]:
    """解析 CSV → {content_id: {views/likes/comments}}，同 id 多行取 max。"""
    out: Dict[str, Dict[str, int]] = {}
    # utf-8-sig：后台导出常带 BOM；gbk 兜底：部分导出工具默认中文编码
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return out
    id_col = _pick(reader.fieldnames, _ID_KEYS)
    if not id_col:
        raise SystemExit(f"CSV 缺少 id 列（支持别名 {_ID_KEYS}）: {reader.fieldnames}")
    view_col = _pick(reader.fieldnames, _VIEW_KEYS)
    like_col = _pick(reader.fieldnames, _LIKE_KEYS)
    comment_col = _pick(reader.fieldnames, _COMMENT_KEYS)
    for row in reader:
        cid = (row.get(id_col) or "").strip()
        if not cid:
            continue
        rec = out.setdefault(cid, {})
        for col, key in ((view_col, "views"), (like_col, "likes"),
                         (comment_col, "comments")):
            if not col:
                continue
            v = _to_int(row.get(col))
            if v is not None:
                rec[key] = max(rec.get(key, 0), v)  # 浏览量单调递增，取最大观测
    return out


# ─────────────────────────────────────────────────────────────────────────────
# R820：创作者中心**文本**导出导入（无 content_id ⇒ 按时间联表回填）
#
# 为什么需要这条分支：后台的「复制」形态只有「北京时间 + 浏览量」，没有帖子 ID，
# 而 content_stats.jsonl 的主键是 content_id。此前只能导 CSV（有 ID），
# 用户实际拿到的是文本 ⇒ 数据进不来，整个浏览闭环停在 10-16。
#
# 联表口径（每条都可由自校验兜住，不靠信任）：
#   · 导出时刻是**北京时间**（UTC+8）；metrics 的 ts 是 UTC ⇒ 先换算再比。
#   · 匹配窗口 ±3min：生产发帖间隔 ≥15min（R713），3min 足以吸收转录/时区误差，
#     又小于半间隔 ⇒ 不会跨到相邻帖。
#   · **撞车**（同一行遥测被两条导出记录认领）：保留 dt 更小者。生产实测 6 组
#     撞车全符合这条规律（自洽的自校验），故不是随手定的规则。
#   · **联不上**的条目必须**单独报出来**，不得静默丢弃：它们要么是转录错位，
#     要么是遥测滚动窗（5000 行）外的老帖——两者含义不同，混在一起会让人
#     以为"数据都导进来了"。
#   · 导出里混有**非本流水线**的帖（人工发布/别的工具）：它们没有遥测行，
#     因此天然落在"联不上"里；但必须**明确点名**，因为其中常含最高浏览的帖，
#     误算进来会伪造出"我们的爆款"（实测 12/130，其中 4 篇是最高的 78/57/44/29）。
# ─────────────────────────────────────────────────────────────────────────────

_RICH_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::\d{2})?\s*已发布\s*$")
_RICH_VIEWS_LABEL = re.compile(r"^浏览量\s*$")
_BARE_INT = re.compile(r"^([\d,]+)\s*$")


def _parse_rich_export(text: str):
    """★★ R848b：创作者中心**富导出**形态（用户实际复制出来的那种）。

    形态（每帖一段，指标块在时间戳**之前**）：

        <正文若干行>
        浏览量
        4
        点赞量
        0
        回复
        0
        引用
        0
        分享量
        0
        2026-10-30 20:29已发布

    ⇒ 取「…已发布」那行的时间为帖时间（**北京时间**，与简形同口径），
      取**同一段里最后出现的「浏览量」**下面的数字为浏览数。

    ★ 为什么必须支持：真实导出就是这个形态，而旧 `_parse_paste` 只认
      `2026-10-21 00:04  0` 那种一行式 ⇒ **真实导出根本进不来**，
      浏览闭环停在库里最后一次成功导入的时刻（本地那份停在 10-20）。
    ★ 与简形**互斥**：命中任意一段就整段按富形走 —— 否则正文那几千行会被
      逐行报成 `invalid`，把"联不上必须单独报出来"这条自校验彻底淹掉。
    返回 `(rows, info)`；`info["invalid"]` 只收**看起来像时间戳却没配上浏览量**的行
    （那才是真错误：转录错位/漏抄一行）。
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    tz = _tz(_td(hours=8))
    lines = text.splitlines()
    rows, invalid = [], []
    pending = None          # 本段里最近一次「浏览量」的数值
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _RICH_STAMP.match(line)
        if m:
            if pending is None:
                invalid.append(line)          # 有时间戳却没有浏览量 ⇒ 真错误，别静默丢
            else:
                stamp = "%s %s" % (m.group(1), m.group(2))
                try:
                    rows.append((_dt.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=tz),
                                 pending))
                except ValueError:
                    invalid.append(line)
            pending = None
            continue
        if _RICH_VIEWS_LABEL.match(line):
            pending = "???"                  # 标记"看到了标签"，等下一行数值
            continue
        if pending == "???":
            mm = _BARE_INT.match(line)
            pending = int(mm.group(1).replace(",", "")) if mm else None
    return rows, {"invalid": invalid}


def _parse_paste(text: str):
    """解析粘贴文本 → ([(aware_bj_dt, views)], invalid)。

    两种导出形态都收（**先试富形**，见 `_parse_rich_export`）：
      · 富形：正文 + 指标块 + 「…已发布」时间戳；
      · 简形（R820 起、向后兼容）：
            "2026-10-21 00:04  0"
            "2026-10-21 00:04\t0"
            "2026-10-21 00:04:33  12"        # 带秒
    行首 # 视为注释。无法解析的行**不静默跳过**，计入 invalid 一并返回。
    """
    rich, info = _parse_rich_export(text)
    if rich:
        return rich, info["invalid"]

    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    tz = _tz(_td(hours=8))
    rows, invalid = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 时间戳在前、浏览量在后；用正则抓，避免被中间的多空格/制表符干扰
        m = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\s+([\d,]+)\s*$",
                     line)
        if not m:
            invalid.append(line)
            continue
        stamp, val = m.group(1), m.group(2).replace(",", "")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M"):
            try:
                rows.append((_dt.strptime(stamp, fmt).replace(tzinfo=tz), int(val)))
                break
            except ValueError:
                continue
        else:
            invalid.append(line)
    return rows, invalid


def _parse_iso_utc(s):
    """metrics 的 ts → aware datetime（UTC）。不可解析返回 None。"""
    if not isinstance(s, str) or not s.strip():
        return None
    from datetime import datetime as _dt, timezone as _tz
    try:
        d = _dt.fromisoformat(s.strip().replace("Z", "+00:00"))
    except Exception:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_tz.utc)


def join_pasted_views(rows, metrics_rows, window_sec: int = 180):
    """把 (北京时间, views) 联到 metrics 发布行，回填 content_id。

    返回 dict：
        records   {content_id: {"views": int}}   —— 可并入 content_stats 的
        unmatched [(bj_dt, views)]               —— 联不上（转录错位 / 遥测窗外 / 非本流水线）
        collisions int                            —— 发生撞车（多行争抢同一遥测行）的次数
        ambiguous  [(content_id, kept_dt, dropped_dt)] —— 撞车明细，供审阅

    ★ 为什么返回值要这么细：这个函数的**全部风险**都在"悄悄少算或多算"。
      只返回 records 的话，调用方无从知道丢了什么——那正是 R612「沉默不是通过」。
    """
    from datetime import timezone as _tz

    # 只为发布行建索引（别的 outcome 没有浏览量的语义）
    cand = []
    for r in metrics_rows or []:
        if not str(r.get("outcome", "")).startswith("binance_published"):
            continue
        t = _parse_iso_utc(r.get("ts"))
        cid = str(r.get("content_id") or "")
        if t is None or not cid:
            continue
        cand.append((t.astimezone(_tz.utc), cid))

    # 每条导出记录认领一个遥测行；同一行被多条记录认领时，dt 更小者胜出
    claim = {}      # cid -> (dt_sec, bj_dt, views)
    ambiguous = []
    collisions = 0
    unmatched = []
    for bj_dt, views in rows:
        t_utc = bj_dt.astimezone(_tz.utc)
        best = None
        for t, cid in cand:
            dt = abs((t - t_utc).total_seconds())
            if dt <= window_sec and (best is None or dt < best[0]):
                best = (dt, cid)
        if best is None:
            unmatched.append((bj_dt, views))
            continue
        dt, cid = best
        prev = claim.get(cid)
        if prev is None:
            claim[cid] = (dt, bj_dt, views)
        elif dt < prev[0]:
            collisions += 1
            ambiguous.append((cid, bj_dt, prev[1]))
            claim[cid] = (dt, bj_dt, views)
        else:
            collisions += 1
            ambiguous.append((cid, prev[1], bj_dt))

    records = {cid: {"views": views} for cid, (_dt, _bj, views) in claim.items()}
    return {"records": records, "unmatched": unmatched,
            "collisions": collisions, "ambiguous": ambiguous}


def import_pasted_views(text: str, metrics_path: str = None) -> dict:
    """粘贴文本 → 并入 content_stats.jsonl 的完整流程（含自校验报告）。

    ★ 合并语义（重要，别改成覆盖）：content_stats.jsonl 是**累积库**，
      本次导出只是最近一小段。实测 10-20 这次导出 130 条（约 2 天），
      而库里已有 378 条（10-02~10-16）——两者 content_id **零交集**。
      若"刷新"成覆盖，会把 13 天的老数据全删掉、进表币种从 34 掉到 19。
      ⇒ 一律走 merge_into_jsonl 的并集语义。
    """
    import json as _json

    rows, invalid = _parse_paste(text)
    if not rows:
        raise SystemExit("粘贴文本里没有解析出任何『北京时间 浏览量』行（检查格式）")
    mp = metrics_path or METRICS_PATH
    metrics_rows = []
    if os.path.exists(mp):
        with open(mp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    metrics_rows.append(_json.loads(line))
                except Exception:
                    continue
    if not metrics_rows:
        raise SystemExit(f"读不到遥测行：{mp}（联表必须有 metrics.jsonl，"
                         f"本地那份可能是旧快照，请用 git show origin/main:metrics.jsonl）")

    res = join_pasted_views(rows, metrics_rows)
    changed = merge_into_jsonl(res["records"])
    n_tok = rebuild_token_engagement()
    return {"parsed": len(rows), "invalid": invalid,
            "joined": len(res["records"]), "records": res["records"],
            "unmatched": res["unmatched"],
            "collisions": res["collisions"], "ambiguous": res["ambiguous"],
            "changed": changed, "n_tokens": n_tok}


def merge_into_jsonl(records: Dict[str, Dict[str, int]]) -> int:
    """把新记录并入 content_stats.jsonl（同 id 各指标取 max，保留已有观测）。
    返回写入的记录条数。

    R641：**逐条保留各自的快照时刻 ts**，不得用本次导入时刻统一覆盖。
    ts 不是元数据而是归因输入——消费侧（metrics_report._exposure_days）用它
    算"该帖已曝光几天"，把浏览量（累积量）换算成速率。首版在此写死一个
    全局 ts，等于宣称"所有帖的曝光时长相同"，会让刚发布的帖与 5 天前的帖
    按绝对值直接比较——而那正是 R641 要修的偏差。
    同一id 的多次导入取**较新**的 ts（快照只会向前推进）。
    """
    existing: Dict[str, Dict[str, int]] = {}
    old_ts: Dict[str, str] = {}
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, "r", encoding="utf-8") as f:
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
                cid = str(cid)
                rec = existing.setdefault(cid, {})
                for k in ("views", "likes", "comments"):
                    v = r.get(k)
                    if isinstance(v, int) and v >= 0:
                        rec[k] = max(rec.get(k, 0), v)
                _t = r.get("ts")
                if isinstance(_t, str) and _t.strip():
                    old_ts[cid] = _t.strip()
    changed = 0
    for cid, rec in records.items():
        cur = existing.setdefault(cid, {})
        before = dict(cur)
        for k, v in rec.items():
            cur[k] = max(cur.get(k, 0), v)
        if cur != before:
            changed += 1
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for cid in sorted(existing):
            rec = existing[cid]
            # 本次导入涉及的 id 用新ts；未涉及的保留旧 ts。
            # （未涉及的 id 若是首次出现在库里则不存在这种情况——existing
            #   里的 id 都来自旧文件，都带ts。）
            _line_ts = ts if cid in records else (old_ts.get(cid) or ts)
            f.write(json.dumps({"content_id": cid, "ts": _line_ts, **rec},
                               ensure_ascii=False) + "\n")
    return changed


# R608：按币种聚合浏览量 → token_engagement.json（committed，供 main.py 发帖选择做
# 「浏览加权」排序微调）。join content_stats.jsonl（content_id→views）× metrics.jsonl
# （content_id→首标的 tokens[0]）。min_n 以下的币不进表（避免对噪声加权）。
TOKEN_ENG_PATH = os.path.join(REPO_ROOT, "token_engagement.json")
METRICS_PATH = os.path.join(REPO_ROOT, "metrics.jsonl")


def rebuild_token_engagement(min_n: int = 4) -> int:
    """从 content_stats.jsonl × metrics.jsonl 重算每币均浏览，写 token_engagement.json。
    返回写入的币种数（含 n<min_n 的，便于审阅；main.py 侧按 min_n 过滤）。

    ★★ R820 决定**不做**「按曝光天数归一」，理由是可复现的实测（不是偏好）：
      R641 说"浏览是累计量、必须按曝光天数归一"，这条在**报表归因**里成立
      （_exposure_days 至今仍是正确口径）。但用在这里是另一回事——这里的输出
      会被 main.apply_engagement_boost 拿去做**币种间分位比较**（median×1.3/×0.7）。
      逐帖除以各自的曝光天数**不是全局缩放**，它改的是币种间的相对排序：
        · 同一份数据（496 条，旧快照 378 + 新导出 118）只差归不归一：
          min_n=2 时 **50% 的币档位翻转**、排名 Spearman 仅 0.494；
          min_n=4 时 48% / 0.571；min_n=6 时 50% / 0.785。
        · 翻转由**小样本**驱动：ARB(n=2) 归一后中位 242.89、TIA(n=3) 93.16、
          WLD(n=3) 89.79 —— n 小 ⇒ 分母（曝光天数）差异被放大成假信号。
      「按曝光归一」的前提是浏览随曝光**单调增长**，实测不成立：
        · 合并样本 Spearman(曝光天数, 浏览) = +0.256，看似支持；
        · 但**同语种 × 同批次**内：A 批 zh-CN +0.086 / zh-TW −0.081 / en −0.218；
          B 批 zh-CN +0.301 / zh-TW −0.150 / en −0.192
          ⇒ 那个 +0.256 是**语种 × 曝光共线**伪造的（en 帖曝光长且浏览低），
            不是曝光效应。
      ⇒ 结论：**保留原始累计浏览**（现状口径）。归一若将来要做，必须先有
        「同语种内、控制批次」的显著正相关证据，而不是拿跨层相关当依据。
      ⚠️ 别把这段读成"R641 错了"：R641 修的是**报表速率**（同一面板内比较
        不同年龄的帖），那里必须归一；这里是**跨币种排序**，口径不同。
    """
    import statistics
    from collections import defaultdict
    cs = {}
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                v = d.get("views")
                if isinstance(v, int) and v >= 0 and d.get("content_id"):
                    cs[str(d["content_id"])] = v
    if not cs or not os.path.exists(METRICS_PATH):
        return 0
    tok_views = defaultdict(list)
    with open(METRICS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("outcome") != "binance_published":
                continue
            cid = str(d.get("content_id") or "")
            toks = d.get("tokens") or []
            if cid in cs and toks:
                tok_views[str(toks[0]).upper().replace("$", "")].append(cs[cid])
    tokens = {t: {"avg_views": round(statistics.mean(v)),
                  "median_views": round(statistics.median(v)),  # R609：抗离群值的主指标
                  "n": len(v)}
              for t, v in tok_views.items()}
    out = {"as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
           "min_n": min_n,
           "note": "per-token avg/median Square views (creator-center export); ranking-only "
                   "engagement boost (R608/R609; boost uses median_views for outlier robustness). "
                   "R820: values are RAW cumulative views, NOT normalized by exposure days — "
                   "per-post normalization flips 50% of token buckets (Spearman 0.494) because "
                   "the 'views grow with exposure' premise fails within language/batch strata; "
                   "see rebuild_token_engagement docstring for the evidence.",
           "tokens": dict(sorted(tokens.items(), key=lambda x: -x[1]["median_views"]))}
    with open(TOKEN_ENG_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return len(tokens)


def main() -> int:
    # ★ R820：--paste 分支（后台文本导出，无 content_id）
    argv = sys.argv[1:]
    if argv and argv[0] == "--paste":
        if len(argv) < 2:
            raise SystemExit("--paste 后面要跟粘贴文本（或 --paste @文件路径）")
        src = argv[1]
        if src.startswith("@"):
            with open(src[1:], encoding="utf-8") as f:
                text = f.read()
        else:
            text = src
        mpath = None
        if "--metrics" in argv:
            mpath = argv[argv.index("--metrics") + 1]
        res = import_pasted_views(text, metrics_path=mpath)
        print(f"📋 解析 {res['parsed']} 条（无法解析 {len(res['invalid'])} 条）")
        if res["invalid"]:
            print("   ⚠️ 无法解析的行（未被导入，请检查格式）：")
            for ln in res["invalid"][:10]:
                print(f"      {ln[:80]}")
        print(f"🔗 联表成功 {res['joined']} 条 → 可并入 {len(res['records'])} 个 content_id")
        print(f"⚠️ 联不上 {len(res['unmatched'])} 条"
              f"（转录错位 / 遥测滚动窗外 / **非本流水线**——三者含义不同，"
              f"需人工判断；其中常含最高浏览的帖，误算进来会伪造爆款）")
        if res["collisions"]:
            print(f"🔀 撞车 {res['collisions']} 次（同一遥测行被多条导出记录认领，"
                  f"已保留 dt 更小者）：")
            for cid, kept, dropped in res["ambiguous"][:10]:
                print(f"      {cid}: 保留 {kept.strftime('%m-%d %H:%M')} "
                      f"丢弃 {dropped.strftime('%m-%d %H:%M')}")
        print(f"✅ 并入 {res['changed']} 条 → {OUT_PATH}（并集语义，不清空历史）")
        print(f"✅ 刷新币种浏览基线 {res['n_tokens']} 币 → {TOKEN_ENG_PATH}")
        return 0

    path = argv[0] if argv else os.getenv("CONTENT_STATS_CSV", DEFAULT_CSV)
    if not os.path.exists(path):
        raise SystemExit(f"找不到 CSV: {path}\n（从币安创作者中心后台导出内容数据，"
                         f"或用第一个参数/ CONTENT_STATS_CSV 指定路径；"
                         f"若手里是『北京时间 浏览量』文本，用 --paste）")
    records = read_csv(path)
    if not records:
        raise SystemExit("CSV 未解析出任何有效行（检查 id 列与数值格式）")
    changed = merge_into_jsonl(records)
    n_tok = rebuild_token_engagement()
    print(f"✅ 导入 {len(records)} 条（更新 {changed} 条）→ {OUT_PATH}")
    print(f"✅ 刷新币种浏览基线 {n_tok} 币 → {TOKEN_ENG_PATH}（R608 浏览加权数据源）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
