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
import sys

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "metrics.jsonl")
TOP_N = 8


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
        "reject_by_stage": collections.Counter(),
        "reject_by_provider": collections.Counter(),
        "reject_reasons": collections.Counter(),
        "latency_by_provider": {},
        "tokens_by_provider": {},
        "errors": collections.Counter(),
        "ts_min": None,
        "ts_max": None,
    }
    lat_tmp, tok_tmp = collections.defaultdict(list), collections.defaultdict(list)
    for r in rows:
        if not isinstance(r, dict):
            continue
        s["by_outcome"][str(r.get("outcome", "unknown"))] += 1
        ts = r.get("ts")
        if isinstance(ts, str) and ts:
            if s["ts_min"] is None or ts < s["ts_min"]:
                s["ts_min"] = ts
            if s["ts_max"] is None or ts > s["ts_max"]:
                s["ts_max"] = ts
        err = r.get("error") or r.get("error_code")
        if err:
            s["errors"][str(err)[:60]] += 1
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
        elif r.get("outcome") == "llm_rejected":
            s["reject_by_stage"][str(r.get("stage", "unknown"))] += 1
            s["reject_by_provider"][who] += 1
            if r.get("reason"):
                s["reject_reasons"][str(r["reason"])[:60]] += 1
        lat = _num(r.get("llm_latency_sec"))
        if lat is not None:
            lat_tmp[str(prov)].append(lat)
        tok = _num(r.get("tokens_used"))
        if tok is not None:
            tok_tmp[str(prov)].append(tok)
    for prov, vals in lat_tmp.items():
        s["latency_by_provider"][prov] = round(sum(vals) / len(vals), 1)
    for prov, vals in tok_tmp.items():
        s["tokens_by_provider"][prov] = {
            "avg": int(round(sum(vals) / len(vals), 0)),
            "total": int(sum(vals)),
        }
    return s


def _top(counter, n=TOP_N):
    return counter.most_common(n)


def render_text(s):
    """人类可读简报"""
    lines = [
        "## metrics 遥测简报",
        f"- 样本: {s['total']} 行"
        + (f"（时间跨度 {s['ts_min'][:16]} → {s['ts_max'][:16]}）" if s["ts_min"] else "（尚无带时间戳样本）"),
        f"- outcome 分布: {dict(s['by_outcome']) or '—'}",
    ]
    n_pub = sum(s["by_provider"].values())
    if n_pub:
        lines.append(f"- 投递 {n_pub} 篇：分时 {_top(s['by_hour'])} / 来源 {_top(s['by_source'])}")
        lines.append(f"  模型 {_top(s['by_provider'])} / 首标的 {_top(s['by_token'])} / 配图率 "
                     f"{s['images']}/{n_pub}")
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
        print(json.dumps(s, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
