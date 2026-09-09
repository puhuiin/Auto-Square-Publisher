#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 成本 / 延迟可观测化分析器。

读取 append_metrics 产出的 metrics.jsonl，按 provider 聚合：
  - 投递 / 拒单次数（stage=summarize, campaign_intel）
  - 总 token 消耗（usage 缺失的记录自动跳过，不影响聚合）
  - 平均 / 累计延迟（llm_latency_sec）
  - 各 provider 的单次调用成本估算（按 blend 单价，单位：美元 / 千 token）

这是上一轮优化（成本可观测化）的交付物之一：把散落在 metrics.jsonl
里的 tokens_used / llm_latency_sec 变成一张可直接贴进报告的对比表。

用法:
  python scripts/cost_analysis.py [--days N] [--json] [--price path.json]

约定:
  - metrics.jsonl 不存在 / 为空时打印友好提示并退出码 0（不报错）。
  - 单价是 *示意* 值，请按各 provider 官方价目表更新 --price 所指 JSON，
    形如 {"reasonix-gateway": {"per_1k": 0.002}, ...}，否则成本列仅作相对排序参考。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METRICS_FILE = os.path.join(BASE_DIR, "metrics.jsonl")

# 示意单价（美元 / 千 token，混合输入输出）。务必按官方价目覆盖。
DEFAULT_PRICE = {
    "reasonix-gateway": {"per_1k": 0.0025},
    "openai": {"per_1k": 0.010},
    "deepseek": {"per_1k": 0.0014},
    "siliconflow": {"per_1k": 0.001},
    "moonshot": {"per_1k": 0.012},
}


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def load_records(days: int | None, include_dry: bool = False) -> list[dict]:
    if not os.path.exists(METRICS_FILE):
        return []
    cutoff = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict] = []
    with open(METRICS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            stage = rec.get("stage")
            if stage not in ("summarize", "campaign_intel"):
                continue
            # R66 引入 DRY 打标：报表默认排除本地试跑数据（正式投递才计入成本）
            if not include_dry and rec.get("dry"):
                continue
            if cutoff is not None:
                ts = _parse_ts(rec.get("ts", ""))
                if ts is None or ts < cutoff:
                    continue
            rows.append(rec)
    return rows


def load_published(days: int | None, include_dry: bool = False) -> list[dict]:
    """加载投递遥测（outcome=binance_published*），供形态/配图/拒稿漏斗分析。"""
    if not os.path.exists(METRICS_FILE):
        return []
    cutoff = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict] = []
    with open(METRICS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not str(rec.get("outcome", "")).startswith("binance_published"):
                continue
            if not include_dry and rec.get("dry"):
                continue
            if cutoff is not None:
                ts = _parse_ts(rec.get("ts", ""))
                if ts is None or ts < cutoff:
                    continue
            rows.append(rec)
    return rows


def aggregate(rows: list[dict], price: dict) -> dict:
    agg = defaultdict(lambda: {
        "success": 0, "rejected": 0, "total_tokens": 0,
        "latency_sum": 0.0, "latency_n": 0, "latency_max": 0.0,
    })
    for r in rows:
        p = r.get("provider", "unknown")
        a = agg[p]
        outcome = r.get("outcome")
        if outcome == "llm_success":
            a["success"] += 1
        elif outcome == "llm_rejected":
            a["rejected"] += 1
        tu = r.get("tokens_used")
        if isinstance(tu, int):
            a["total_tokens"] += tu
        lat = r.get("llm_latency_sec")
        if isinstance(lat, (int, float)):
            a["latency_sum"] += lat
            a["latency_n"] += 1
            a["latency_max"] = max(a["latency_max"], lat)
    # 计算成本估算
    out = {}
    for p, a in agg.items():
        rate = price.get(p, {}).get("per_1k", 0.0)
        cost = (a["total_tokens"] / 1000.0) * rate if rate else None
        avg_lat = (a["latency_sum"] / a["latency_n"]) if a["latency_n"] else 0.0
        out[p] = {
            "success": a["success"],
            "rejected": a["rejected"],
            "total_tokens": a["total_tokens"],
            "avg_latency": round(avg_lat, 3),
            "max_latency": round(a["latency_max"], 3),
            "est_cost_usd": (round(cost, 4) if cost is not None else None),
        }
    return out


def render_publish_funnel(published: list[dict], rejected: list[dict]) -> str:
    """运营驾驶舱第二段：内容形态对比 + 配图来源分布 + 拒稿漏斗。"""
    from collections import Counter
    lines = ["\n## 内容形态与配图（投递遥测）"]
    if not published:
        return "\n".join(lines) + "\n\n| _无投递记录_ |\n|---|"
    articles = [r for r in published if r.get("article")]
    shorts = [r for r in published if not r.get("article")]

    def _avg(rows, key):
        vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 1) if vals else 0

    lines.append("\n| 形态 | 篇数 | 平均 tokens | 平均延迟(s) |")
    lines.append("|---|---:|---:|---:|")
    for name, rows in (("📄 长文", articles), ("⚡ 短讯", shorts)):
        if rows:
            lines.append(f"| {name} | {len(rows)} | {_avg(rows, 'tokens_used'):,} | {_avg(rows, 'llm_latency_sec')} |")
    if articles and shorts:
        a_tok, s_tok = _avg(articles, "tokens_used"), _avg(shorts, "tokens_used")
        ratio = f"{a_tok / s_tok:.1f}x" if s_tok else "—"
        lines.append(f"\n长文单篇成本约为短讯的 **{ratio}**（每天 1 篇，观察互动回报再调门槛）")

    tiers = Counter(r.get("image_tier") or ("有图" if r.get("image") else "无图") for r in published)
    lines.append("\n**配图来源分布**: " + " · ".join(f"{k} ×{v}" for k, v in tiers.most_common()))

    if rejected:
        stages = Counter(r.get("stage") or "transport" for r in rejected)
        lines.append("\n**拒稿漏斗（stage 分布）**: " + " · ".join(f"{k} ×{v}" for k, v in stages.most_common()))
        persons = Counter(r.get("persona") for r in published if r.get("persona"))
        if persons:
            lines.append("\n**人设分布（投递）**: " + " · ".join(f"{k} ×{v}" for k, v in persons.most_common()))
    return "\n".join(lines)


def render_markdown(agg: dict) -> str:
    header = ("| Provider | 成功 | 拒单 | 总 Token | 平均延迟(s) | 最大延迟(s) | 估算成本($) |\n"
              "|----------|-----:|-----:|----------:|------------:|------------:|------------:|")
    if not agg:
        return header + "\n| _无数据_ | 0 | 0 | 0 | 0 | 0 | 0 |"
    lines = [header]
    for p in sorted(agg, key=lambda k: agg[k]["total_tokens"], reverse=True):
        a = agg[p]
        cost = f"{a['est_cost_usd']:.4f}" if a["est_cost_usd"] is not None else "—"
        lines.append(
            f"| {p} | {a['success']} | {a['rejected']} | {a['total_tokens']:,} "
            f"| {a['avg_latency']:.3f} | {a['max_latency']:.3f} | {cost} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 成本/延迟对比 + 运营漏斗分析")
    ap.add_argument("--days", type=int, default=None, help="只看最近 N 天")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown 表")
    ap.add_argument("--price", type=str, default=None, help="单价 JSON 路径（覆盖默认示意价）")
    ap.add_argument("--include-dry", action="store_true", help="包含 DRY_RUN 打标的记录（默认排除）")
    args = ap.parse_args()

    price = DEFAULT_PRICE
    if args.price:
        try:
            with open(args.price, "r", encoding="utf-8") as f:
                price = json.load(f)
        except Exception as e:
            print(f"[warn] 读取单价文件失败，沿用默认示意价: {e}", file=sys.stderr)

    rows = load_records(args.days, include_dry=args.include_dry)
    published = load_published(args.days, include_dry=args.include_dry)
    if not rows and not published:
        print("metrics.jsonl 不存在或无 LLM 遥测记录（stage=summarize/campaign_intel）。")
        print("提示：先跑一次带 --llm-live 的真实调用，或本地单元测试会写入示例遥测。")
        return 0

    agg = aggregate(rows, price)
    if args.json:
        print(json.dumps({"providers": agg, "published_n": len(published)},
                         ensure_ascii=False, indent=2))
    else:
        print(f"# LLM 成本 / 延迟对比（LLM 遥测 {len(rows)} 条，投递 {len(published)} 篇）\n")
        print(render_markdown(agg))
        print(render_publish_funnel(published, rows))
        print("\n注：单价为示意值，请按官方价目更新；usage 缺失的记录不计入 token 总量；"
              "DRY_RUN 记录默认排除（--include-dry 查看）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
