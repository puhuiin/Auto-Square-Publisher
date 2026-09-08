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
  - metrics.jsonl: 行级去重并集
  - drafts/: 快照中的新草稿按 news_id 后缀去重并回（reset --hard 后远端版已恢复）

快照契约（与 .github/workflows/auto_post.yml 的 cp 必须逐字一致，
两边曾错位导致合并读空快照、本地记录静默丢失）：
  <snapshot_dir>/sent_cache.json
  <snapshot_dir>/campaign_intel.json
  <snapshot_dir>/metrics.jsonl
  <snapshot_dir>/local_drafts/   （drafts/ 下相对结构原样拷贝，含日期子目录）

用法（在仓库根目录）：
  python scripts/git_state_merge.py [本地快照目录]   # 默认 /tmp
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CACHE_FILE = "sent_cache.json"
INTEL_FILE = "campaign_intel.json"
METRICS_FILE = "metrics.jsonl"
MAX_CACHE_KEEP = 500
_BREAKER_KEY = "_llm_breaker"


def _gc_expired_breaker(state: dict, now=None) -> dict:
    """熔断过期 GC：cooldown_until 已过的条目不再有调度意义（_is_cooled 已判 False），
    合并时丢弃。否则 _llm_breaker 的键只增不减——success-clear 也会被远端旧值并集
    复活（生产实证：b.ai 成功后其过期条目仍躺在同步结果里），fails 计数无限累积。
    只清"已确定过期"：时间戳解析失败的保留（看不懂的不删）；未过期的远端条目保留
    （并发运行的冷却不能丢，安全方向）。"""
    now = now or datetime.now(timezone.utc)
    out = {}
    for name, info in (state or {}).items():
        if not isinstance(info, dict):
            out[name] = info
            continue
        try:
            until = datetime.fromisoformat(str(info.get("cooldown_until", "")))
        except Exception:
            out[name] = info  # 脏时间戳看不懂，保留
            continue
        if until.tzinfo is None:
            out[name] = info  # naive 时间不擅自解释时区（与主模块 _is_cooled 同策略），保留
            continue
        try:
            expired = now >= until
        except Exception:
            out[name] = info
            continue
        if not expired:
            out[name] = info
    return out


def atomic_write_text(path, text: str) -> None:
    """崩溃安全写盘：同目录 tmp + os.replace（与 main._atomic_write_text 同语义，
    此脚本独立运行不 import 主模块，故小段重复）。
    合并写半截会把已同步的状态损坏后推上远端，比本地崩溃更严重。"""
    tmp = f"{path}.tmp"
    try:
        Path(tmp).write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


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


def _pick_scalar(va, vb):
    """标量择优：可比较取较大；异构类型（int vs str）取 str 化后较大者。

    py3 的 max() 比较异构类型直接抛 TypeError，而本脚本在 workflow 里是
    状态落盘的最后一道防线——它一崩，整轮的 sent_cache / 断路器状态全丢，
    下轮必然重复发帖。故比较必须永不抛异常且结果确定（可复现地选同一侧）。
    """
    try:
        return max(va, vb)
    except TypeError:
        return max((va, vb), key=lambda x: str(x))


def _sort_key(value) -> str:
    """排序键一律字符串化：脏数据里的 None/int 混进时间戳列会 TypeError 崩脚本"""
    return str(value or "")


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
            out[k] = _pick_scalar(va, vb)
    return out


def merge_sent_cache(local_snapshot_path: str, remote_path: str) -> int:
    """并集远端+本地 sent_cache，返回合并后总条数"""
    union = {}
    for item in load_list(remote_path) + load_list(local_snapshot_path):
        if isinstance(item, dict) and item.get("id"):
            union[item["id"]] = item
    merged = sorted(union.values(), key=lambda x: _sort_key(x.get("sent_at")))[-MAX_CACHE_KEEP:]
    atomic_write_text(remote_path, json.dumps(merged, ensure_ascii=False, indent=2))
    return len(merged)


def merge_intel(local_snapshot_path: str, remote_path: str) -> bool:
    """主体取较新，状态键深合并。返回是否有内容。"""
    versions = [v for v in (load_obj(remote_path), load_obj(local_snapshot_path)) if v]
    if not versions:
        return False
    best = dict(max(versions, key=lambda d: _sort_key(d.get("last_updated"))))
    states = [{k: v for k, v in ver.items() if k.startswith("_")} for ver in versions]
    merged_state = merge_state(states[0], states[1] if len(states) > 1 else {})
    if isinstance(merged_state.get(_BREAKER_KEY), dict):
        merged_state[_BREAKER_KEY] = _gc_expired_breaker(merged_state[_BREAKER_KEY])
    best.update(merged_state)
    atomic_write_text(remote_path, json.dumps(best, ensure_ascii=False, indent=2))
    return True


def merge_metrics(local_snapshot_path: str, remote_path: str) -> int:
    """JSONL 行级去重并集（追加型遥测，重复行只保留一份），返回合并后行数"""
    lines = []
    seen = set()
    for path in (remote_path, local_snapshot_path):
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    lines.append(line)
        except Exception:
            continue
    lines.sort()  # ts 开头的 JSON 行排序即时间序
    atomic_write_text(remote_path, ("\n".join(lines) + "\n") if lines else "")
    return len(lines)


DRAFTS_SNAPSHOT_SUBDIR = "local_drafts"
DRAFTS_DIR = "drafts"


def merge_drafts(snapshot_dir: str, drafts_dir: str = DRAFTS_DIR) -> int:
    """把快照中的新草稿并回 drafts/，返回新增份数。

    reset --hard 后远端版草稿已恢复，为防同故事收两份，按文件名 news_id 后缀
    （HHMMSS_<slug>.md 取 _ 后部分，与 OKXDraftExporter._draft_exists 同规则）去重。
    快照保留相对子目录结构（日期文件夹），按原结构归位。
    """
    snap = Path(snapshot_dir) / DRAFTS_SNAPSHOT_SUBDIR
    dest = Path(drafts_dir)
    if not snap.is_dir():
        return 0

    def _slug(name: str) -> str:
        return name.rsplit("_", 1)[-1] if "_" in name else name

    existing_slugs = set()
    if dest.is_dir():
        for p in dest.rglob("*.md"):
            existing_slugs.add(_slug(p.name))

    added = 0
    for src in sorted(snap.rglob("*.md")):
        if _slug(src.name) in existing_slugs:
            continue
        try:
            rel = src.relative_to(snap)
        except ValueError:
            rel = Path(src.name)
        target = dest / rel
        if target.exists():
            existing_slugs.add(_slug(src.name))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
        existing_slugs.add(_slug(src.name))
        added += 1
    return added


def main():
    snapshot_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp")
    n = merge_sent_cache(snapshot_dir / CACHE_FILE, CACHE_FILE)
    print(f"sent_cache.json 合并完成: {n} 条")
    if merge_intel(snapshot_dir / INTEL_FILE, INTEL_FILE):
        print("campaign_intel.json 合并完成 (主体较新 + 状态键深合并)")
    m = merge_metrics(snapshot_dir / METRICS_FILE, METRICS_FILE)
    print(f"metrics.jsonl 合并完成: {m} 行")
    d = merge_drafts(snapshot_dir, DRAFTS_DIR)
    print(f"drafts/ 合并完成: 新增 {d} 份草稿")


if __name__ == "__main__":
    main()
