#!/usr/bin/env python3
"""调度看门狗（R61 事故产物，R71 从 workflow 内嵌 heredoc 抽出为可测试脚本）。

背景：GitHub 调度器曾静默吞掉 4.5 小时的 cron 投递（13 个调度点零投递、
无日志无报警——没跑就没有日志）。本脚本在每次真实运行的开头执行，查
GitHub API 的运行历史（真相源，不依赖任何落盘状态）：上一轮 schedule
运行距今超过阈值（默认 50 分钟 = 2 个 cron 间隔）即推送报警。

设计约束：
- 只报警不退出非零——看门狗绝不能阻塞发帖主流程；
- 活跃窗口模式（ACTIVE_HOURS_BEIJING 非空）跳过：夜间合法停跑会误报；
- gh / API 任何失败都静默 exit 0。
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

WATCHDOG_MAX_AGE_MIN = 50   # cron 每 20 分钟一次，2 次连续丢点即触发
NORMAL_SLOT_MIN = 20


def _load_runs() -> list:
    out = subprocess.run(
        ["gh", "run", "list", "--workflow=auto_post.yml", "--limit", "10",
         "--json", "createdAt,event"],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout or "[]")


def evaluate(runs: list, now: datetime) -> str:
    """纯函数判定：返回报警消息（空串 = 不报警）。供单元测试直调。"""
    sched = [r for r in runs if r.get("event") == "schedule"]
    if len(sched) < 2:
        return ""
    prev = datetime.fromisoformat(sched[1]["createdAt"].replace("Z", "+00:00"))
    age_min = (now - prev).total_seconds() / 60
    if age_min <= WATCHDOG_MAX_AGE_MIN:
        return ""
    missed = int(age_min // NORMAL_SLOT_MIN)
    return (f"本次运行距上一轮 schedule 运行 {age_min:.0f} 分钟（正常 {NORMAL_SLOT_MIN} 分钟），"
            f"中间约 {missed} 轮 cron 被 GitHub 调度器静默吞掉。机器人没跑就没有日志，"
            f"此报警由本次恢复后的运行代发。若反复出现，建议改用外部 cron "
            f"（cron-job.org 等）定时回调 workflow_dispatch 触发。")


def main() -> None:
    if os.getenv("ACTIVE_HOURS_BEIJING", "").strip():
        print("活跃窗口模式已配置，看门狗跳过（仅 24h 模式启用）。")
        return
    try:
        runs = _load_runs()
    except Exception as e:
        print(f"查询运行历史失败（不影响主流程）: {e}")
        return
    if len([r for r in runs if r.get("event") == "schedule"]) < 2:
        print("schedule 历史不足两条，跳过看门狗判断。")
        return
    message = evaluate(runs, datetime.now(timezone.utc))
    if not message:
        prev = [r for r in runs if r.get("event") == "schedule"][1]["createdAt"]
        print(f"看门狗正常：上一轮 schedule 运行 {prev}，间隔未超阈值。")
        return
    print(f"🚨 {message}")
    # 延迟导入：复用主程序的报警通道（其自带 12h 同标题节流）
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from main import Notifier
    Notifier.send_notification("发帖机器人调度中断恢复", message, is_error=True)


if __name__ == "__main__":
    main()
