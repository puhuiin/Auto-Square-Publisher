#!/usr/bin/env python3
"""调度看门狗（R61 事故产物，R71 从 workflow 内嵌 heredoc 抽出为可测试脚本）。

背景：GitHub 调度器曾静默吞掉 4.5 小时的 cron 投递（13 个调度点零投递、
无日志无报警——没跑就没有日志）。本脚本在每次真实运行的开头执行，查
GitHub API 的运行历史（真相源，不依赖任何落盘状态）：上一轮**调度触发**
（R221 起 = schedule / repository_dispatch 任一）距今超过阈值
（默认 50 分钟 = 2 个间隔）即推送报警。

设计约束：
- 只报警不退出非零——看门狗绝不能阻塞发帖主流程；
- gh / API 任何失败都静默 exit 0。

R6 修的三处：
1. **不得阻塞主流程。** 旧实现把 `from main import Notifier` 与发送裸露在
   try 之外，一旦报警通道抛错，workflow 就会跳过后续发帖步骤 → 直接漏发，
   与该脚本自己的"绝不阻塞"约束相反。现在整段包 try，并在 workflow 侧
   同时加了 `continue-on-error`（纵深防御）。
2. **活跃窗口不再让看门狗失明。** cron 是 24/7 的 `7,27,47 * * * *`，
   ACTIVE_HOURS_BEIJING 只是让 main.py 在窗口外静默 exit 0——调度心跳照常
   落点，夜间并不存在"合法停跑"。旧实现一见该变量就整脚本 return，等于
   把每天一大段时间的丢投递检测全关掉。
3. **取"上一轮"的口径。** 本脚本在本次运行内部执行，`gh run list` 的第一条
   就是本次运行。旧实现固定取 schedule 列表的 [1]：本次若非 schedule 事件
   （push / workflow_dispatch）则上一条 schedule 其实是 [0]，age 被多算一整个
   槽位；schedule 不足两条时又直接静默，真正的长时间停摆反而漏报。现在只统计
   **已完成**的运行并取最近一条调度触发（R221：schedule / repository_dispatch）。
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional

WATCHDOG_MAX_AGE_MIN = 50   # 调度触发每 20 分钟一次，2 次连续丢点即触发
NORMAL_SLOT_MIN = 20


def _load_runs() -> list:
    out = subprocess.run(
        ["gh", "run", "list", "--workflow=auto_post.yml", "--limit", "20",
         "--json", "createdAt,event,status"],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout or "[]")


def _parse_ts(raw) -> Optional[datetime]:
    """GitHub 返回的是 UTC ISO 串；解析失败返回 None（调用方按"无法判定"处理）。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _history(runs: list) -> list:
    """排除本次运行自身，返回可比较的历史运行。

    本次运行在列表里是 in_progress，故优先只取 completed；若 gh 版本不返回
    status 字段，退化为"去掉列表头部一条"（头部即本次运行）。"""
    if any("status" in r for r in runs):
        return [r for r in runs if r.get("status") == "completed"]
    return runs[1:] if runs else []


def evaluate(runs: list, now: datetime) -> str:
    """纯函数判定：返回报警消息（空串 = 不报警）。供单元测试直调。"""
    # R221：调度主力已迁到 repository_dispatch（外部回调 20 分钟一发），
    # schedule 被 GitHub 高丢失率降级成偶发（实测 ~20 轮窗口仅 1 发）。
    # 只认 schedule 的旧口径双向失实：dispatch 健康时，schedule 偶发落地
    # 会让随后一个窗口期连续误报（生产实录：schedule 06:05 落一发，
    # 07:43 轮报警"距上一轮 98 分钟"——期间 dispatch 全部准点，纯属假火警）；
    # dispatch 真死时窗口里多半没有 schedule，反而静默漏报（R61 场景回归）。
    # 心跳口径 = 两种调度触发任一；push 刻意不计入——它是运行的结果
    # （缓存提交）而非调度源，不能为调度器健康背书。
    cadence = [r for r in _history(runs)
               if r.get("event") in ("schedule", "repository_dispatch")]
    if not cadence:
        return ""
    prev = _parse_ts(cadence[0].get("createdAt"))
    if prev is None:
        return ""
    age_min = (now - prev).total_seconds() / 60
    if age_min <= WATCHDOG_MAX_AGE_MIN:
        return ""
    missed = int(age_min // NORMAL_SLOT_MIN)
    return (f"本次运行距上一轮调度触发（schedule/repository_dispatch）{age_min:.0f} 分钟"
            f"（正常 {NORMAL_SLOT_MIN} 分钟），中间约 {missed} 个调度点被静默吞掉。"
            f"机器人没跑就没有日志，此报警由本次恢复后的运行代发。"
            f"若反复出现，检查外部定时回调（cron-job.org 等）是否停摆。")


def main() -> None:
    try:
        runs = _load_runs()
    except Exception as e:
        print(f"查询运行历史失败（不影响主流程）: {e}")
        return
    try:
        message = evaluate(runs, datetime.now(timezone.utc))
    except Exception as e:
        print(f"看门狗判定异常（不影响主流程）: {e}")
        return
    if not message:
        print("看门狗正常：上一轮调度触发间隔未超阈值。")
        return
    print(f"🚨 {message}")
    # 延迟导入：复用主程序的报警通道（其自带 12h 同标题节流）。
    # 整段包 try：报警通道自己出问题绝不能让看门狗把发帖主流程一起带走。
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from main import Notifier
        Notifier.send_notification("发帖机器人调度中断恢复", message, is_error=True)
    except Exception as e:
        print(f"报警通道不可用（看门狗不阻塞主流程，仅记录）: {e}")


if __name__ == "__main__":
    main()
