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

CSV 表头（中英均可，至少要有 id 列，其余列缺失即跳过该指标）：
    content_id/帖子ID ｜ 浏览量/views/阅读量 ｜ 点赞/likes ｜ 评论/comments
"""
from __future__ import annotations

import csv
import io
import json
import os
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


def merge_into_jsonl(records: Dict[str, Dict[str, int]]) -> int:
    """把新记录并入 content_stats.jsonl（同 id 各指标取 max，保留已有观测）。
    返回写入的记录条数。"""
    existing: Dict[str, Dict[str, int]] = {}
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
                rec = existing.setdefault(str(cid), {})
                for k in ("views", "likes", "comments"):
                    v = r.get(k)
                    if isinstance(v, int) and v >= 0:
                        rec[k] = max(rec.get(k, 0), v)
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
            f.write(json.dumps({"content_id": cid, "ts": ts, **rec},
                               ensure_ascii=False) + "\n")
    return changed


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else os.getenv(
        "CONTENT_STATS_CSV", DEFAULT_CSV)
    if not os.path.exists(path):
        raise SystemExit(f"找不到 CSV: {path}\n（从币安创作者中心后台导出内容数据，"
                         f"或用第一个参数/ CONTENT_STATS_CSV 指定路径）")
    records = read_csv(path)
    if not records:
        raise SystemExit("CSV 未解析出任何有效行（检查 id 列与数值格式）")
    changed = merge_into_jsonl(records)
    print(f"✅ 导入 {len(records)} 条（更新 {changed} 条）→ {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
