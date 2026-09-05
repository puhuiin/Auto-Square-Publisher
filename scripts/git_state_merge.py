#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Git 状态同步合并器（GPIO: 用于 GitHub Actions workflow 的 push 前预处理）。

用途：
  每次 workflow 运行结束前，把本地产出的 sent_cache.json / campaign_intel.json 与
  远端最新版本做并集合并，保证任何一方的已发记录与运行时状态都不丢。
  消融交互式 git rebase 冲突，同步流水线 100% 不会 hang。

合并规则：
  - sent_cache.json: 按 id 并集，按 sent_at 排序，截断到最新 500 条
  - campaign_intel.json: 主体键保留 last_updated 较新的一份；"_" 前缀的运行时状态键
    (断路器/源停放/报警节流/兜底图缓存) 递归深合并，标量按时间戳较大者优先

用法（在仓库根目录）：
  python scripts/git_state_merge.py [本地快照目录]   # 默认 /tmp
"""
import json
import sys
from pathlib import Path

CACHE_FILE = "sent_cache.json"
INTEL_FILE = "campaign_intel.json"
MAX_CACHE_KEEP = 500


def load_list(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("sent_ids", [])
    except Exception:
        pass
    return []


def load_obj(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def merge_state(a: dict, b: dict) -> dict:
    """递归深合并：dict 递归，None 让位给非 None，标量按"较大者优先"（ISO 时间戳字典序==时间序）"""
    out = {}
    for k in set(a) | set(b):
        va, vb = a.get(k), b.get(k)
        if isinstance(va, dict) and isinstance(vb, dict):
            out[k] = merge_state(va, vb)
        elif va is None:
            out[k] = vb
        elif vb is None:
            out[k] = va
        elif isinstance(va, list) or isinstance(vb, list):
            out[k] = va if (va is not None and (not isinstance(vb, list) or len(va) >= len(vb or []))) else vb
        else:
            out[k] = max(va, vb)
    return out


def merge_sent_cache(local_snapshot_path: str, remote_path: str) -> int:
    """并集远端+本地 sent_cache，返回合并后总条数"""
    union = {}
    for item in load_list(remote_path) + load_list(local_snapshot_path):
        if isinstance(item, dict) and item.get("id"):
            union[item["id"]] = item
    merged = sorted(union.values(), key=lambda x: x.get("sent_at", ""))[-MAX_CACHE_KEEP:]
    Path(remote_path).write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(merged)


def merge_intel(local_snapshot_path: str, remote_path: str) -> bool:
    """主体取较新，状态键深合并。返回是否有内容。"""
    versions = [v for v in (load_obj(remote_path), load_obj(local_snapshot_path)) if v]
    if not versions:
        return False
    best = dict(max(versions, key=lambda d: d.get("last_updated", "")))
    states = [{k: v for k, v in ver.items() if k.startswith("_")} for ver in versions]
    merged_state = merge_state(states[0], states[1] if len(states) > 1 else {})
    best.update(merged_state)
    Path(remote_path).write_text(json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def main():
    snapshot_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp")
    n = merge_sent_cache(snapshot_dir / CACHE_FILE, CACHE_FILE)
    print(f"sent_cache.json 合并完成: {n} 条")
    if merge_intel(snapshot_dir / INTEL_FILE, INTEL_FILE):
        print("campaign_intel.json 合并完成 (主体较新 + 状态键深合并)")


if __name__ == "__main__":
    main()
