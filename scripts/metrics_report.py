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

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "metrics.jsonl")
TOP_N = 8

# R105：内容合规巡检模式（与 main.py 同步——脚本独立运行不 import 主模块）。
# prompt 级禁令是软约束，模型可能不遵守——合规度此前零度量，全靠人工读帖。
# 注意：final_preview 只存前 120 字符，巡检覆盖的是"前两行钩子区"（算法首屏
# 所在），非全文。
_FNG_ANCHOR_RE = re.compile(
    r"(贪婪|恐惧|情绪)指数|贪婪区|恐惧区|(?:贪婪|恐惧|情绪)[^。！？\n]{0,8}\d{2}")
_OVERUSED_DEVICES = ("先泼盆冷水",)  # main._OVERUSED_OPENING_DEVICES
_AI_FLAVOR_HARD = (  # main.MultiLLMEngine._AI_FLAVOR_HARD
    "拭目以待", "未来可期", "保驾护航", "谱写", "新篇章", "扬帆起航",
    "值得注意的是", "值得一提的是", "综上所述", "总而言之", "让我们一起",
    "毋庸置疑", "不言而喻", "共同见证",
)
QUALITY_SCAN_WINDOW = 20  # 最近 N 篇发布帖做合规扫描


def quality_scan(rows, window=QUALITY_SCAN_WINDOW):
    """对最近 N 篇发布帖的 final_preview 做禁令合规扫描（信息性，非门禁）。
    返回 {"scanned", "fng_anchor", "banned_device", "ai_flavor", "offenders": {...}}。
    R101 前的历史帖命中 FNG 属预期（防线尚未上线），解读时对照时间线。"""
    previews = []
    for r in rows:
        if not isinstance(r, dict) or r.get("dry_run") is True:
            continue
        if not str(r.get("outcome", "")).startswith("binance_published"):
            continue
        pv = (r.get("final_preview") or "").strip()
        if pv:
            previews.append(pv)
    previews = previews[-window:]
    out = {"scanned": len(previews), "fng_anchor": 0, "banned_device": 0,
           "ai_flavor": 0, "offenders": collections.Counter()}
    for pv in previews:
        if _FNG_ANCHOR_RE.search(pv):
            out["fng_anchor"] += 1
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
        "reject_by_stage": collections.Counter(),
        "reject_by_provider": collections.Counter(),
        "reject_reasons": collections.Counter(),
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
            if r.get("image"):
                s["images"] += 1
            tier = r.get("image_tier")
            if tier:
                s["image_tiers"][str(tier)] += 1
        elif outcome == "llm_rejected":
            s["reject_by_stage"][str(r.get("stage", "unknown"))] += 1
            s["reject_by_provider"][who] += 1
            if r.get("reason"):
                s["reject_reasons"][str(r["reason"])[:60]] += 1
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
            quota_blocked = r.get("quota_blocked") is True
            hours_blocked = r.get("active_hours_blocked") is True
            if quota_blocked:
                runs_tmp["quota_blocked"] += 1
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
            tr = r.get("trending")
            if isinstance(tr, str) and tr.strip():
                runs_tmp["last_trending"] = tr
            for k, v in r.items():
                if k.startswith("skipped_") and isinstance(v, (int, float)):
                    runs_tmp["skips"][k[len("skipped_"):]] += int(v)
    s["runs"] = {
        **{k: v for k, v in runs_tmp.items() if k != "skips"},
        "skips": dict(runs_tmp["skips"]),
    }
    for prov, vals in lat_tmp.items():
        s["latency_by_provider"][prov] = round(sum(vals) / len(vals), 1)
    for prov, vals in tok_tmp.items():
        s["tokens_by_provider"][prov] = {
            "avg": int(round(sum(vals) / len(vals), 0)),
            "total": int(sum(vals)),
        }
    return s


def funnel(rows):
    """发布成功率漏斗：分母用"真正进入 LLM 尝试的故事"——
    llm_rejected + llm_failed + llm_success + 任意投递成功。拒稿必记、
    成功只在投递时记（append_metrics 语义），所以 success 行与 delivered 行
    不会重复计数同一故事：一个故事要么在质量/传输层被拦（rejected/failed），
    要么走到投递（此时只有投递行、没有 success 行）。
    R100：stage=campaign_intel 的行是情报刷新（每 12h 一次的运营性 LLM 调用），
    不是发帖尝试——混入分母会把成功率系统性稀释（生产实测：131 分母里
    混着 15 条情报行）。"""
    delivered = sum(1 for r in rows
                    if isinstance(r, dict) and r.get("dry_run") is not True and _is_delivered(r))
    attempted = delivered
    for r in rows:
        if not isinstance(r, dict) or r.get("dry_run") is True:
            continue
        if r.get("stage") == "campaign_intel":
            continue  # 情报刷新不是发帖尝试
        outcome = str(r.get("outcome", ""))
        if outcome in ("llm_rejected", "llm_failed", "llm_success") and not _is_delivered(r):
            attempted += 1
    return {"attempted": attempted, "delivered": delivered,
            "rate": round(delivered / attempted, 3) if attempted else None}


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
            lines.append(f"- 发布成功率: {f['delivered']}/{f['attempted']} 篇"
                         f"（{f['rate'] * 100:.1f}%，分母=进入 LLM 尝试的故事）")
    runs = s.get("runs") or {}
    if runs.get("n"):
        parts = [f"配额饱和 {runs['quota_blocked']} 轮", f"零候选 {runs['zero_candidates']} 轮"]
        if runs.get("active_hours_blocked"):
            parts.append(f"时段外 {runs['active_hours_blocked']} 轮")
        lines.append(f"- 运行摘要（{runs['n']} 轮）: {' / '.join(parts)}"
                     f"，累计候选 {runs['candidates']} → 发布 {runs['published']}"
                     + (f"（未处理 {runs['unprocessed']}）" if runs.get("unprocessed") else ""))
        if runs.get("last_trending"):
            lines.append(f"  最近热搜 [{runs['last_trending']}]")
        if runs.get("skips"):
            lines.append(f"  跳过分布 {runs['skips']}")
    if rows is not None:
        q = quality_scan(rows)
        if q["scanned"]:
            violations = q["fng_anchor"] + q["banned_device"] + q["ai_flavor"]
            status = "全部通过" if not violations else f"{violations} 处命中"
            lines.append(f"- 内容合规巡检（最近 {q['scanned']} 篇前 120 字）: {status}"
                         + (f" {q['offenders']}" if q["offenders"] else ""))
    n_pub = sum(s["by_provider"].values())
    if n_pub:
        lines.append(f"- 投递 {n_pub} 篇：分时 {_top(s['by_hour'])} / 来源 {_top(s['by_source'])}")
        lines.append(f"  模型 {_top(s['by_provider'])} / 首标的 {_top(s['by_token'])} / 配图率 "
                     f"{s['images']}/{n_pub}")
        if s["image_tiers"]:
            lines.append(f"  配图层级 {dict(s['image_tiers'])}")
    n_rej = sum(s["reject_by_stage"].values())
    if n_rej:
        lines.append(f"- 拦截 {n_rej} 次：阶段 {_top(s['reject_by_stage'])} / 模型 {_top(s['reject_by_provider'])}")
        if s["reject_reasons"]:
            lines.append(f"  高频原因 {_top(s['reject_reasons'], 5)}")
    if s["latency_by_provider"]:
        lines.append(f"- 平均延迟(s) {dict(sorted(s['latency_by_provider'].items()))}")
    if s["tokens_by_provider"]:
        lines.append(f"- token 消耗 {dict(sorted(s['tokens_by_provider'].items()))}")
    if s["errors"]:
        lines.append(f"- 错误串 {_top(s['errors'], 5)}")
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    paths = [a for a in argv if not a.startswith("-")]
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
