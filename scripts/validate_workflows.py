#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Workflow 自检器（供 .github/workflows/ci.yml 调用，本地也可跑）。

CI 在 workflow 文件变更时会被触发，但默认只跑单测：YAML 手滑写错、
内嵌 run 脚本少个 fi，要等到定时任务运行时才爆炸。本脚本把这两类问题
提前到 CI 阶段：
  1. 两个 workflow 文件必须能被 YAML 解析；
  2. 每个 run: 块必须通过 `bash -n` 语法检查。GitHub 表达式 `${{ }}`
     先掩码为占位符（bash 解析器不保证认它），只验 shell 结构本身。

用法：
  pip install "pyyaml>=6,<7"   # 仅 CI/校验需要，不进 requirements prod 依赖
  python scripts/validate_workflows.py [workflow_glob...]
"""
import glob
import os
import re
import shutil
import subprocess
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: 需要 pyyaml（CI 步骤内 pip 安装，本地 pip install pyyaml）")
    sys.exit(2)

GHA_EXPR_RE = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
GHA_PLACEHOLDER = "__GHA_EXPR__"


def mask_expressions(text: str) -> str:
    """把 ${{ ... }} 替换为无害占位符，保留 shell 结构可检查性"""
    return GHA_EXPR_RE.sub(GHA_PLACEHOLDER, text)


def iter_run_blocks(workflow_path: str):
    """产出 (job_id, step_index, step_name, masked_script)，YAML 非法时抛异常"""
    with open(workflow_path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    jobs = (doc or {}).get("jobs") or {}
    for job_id, job in jobs.items():
        for idx, step in enumerate((job or {}).get("steps") or []):
            step = step or {}
            if step.get("run"):
                yield job_id, idx, step.get("name", ""), mask_expressions(step["run"])


def bash_available() -> bool:
    return shutil.which("bash") is not None


def bash_check(script: str) -> str:
    """bash -n 检查一段脚本，返回 '' 表示通过，否则返回 stderr 摘要。
    用 `bash -n -c` 直接验字符串：不落地临时文件（Windows 上系统临时目录
    的盘符路径会被 bash 反斜杠转义误读），也不走 stdin（本机 GBK 会炸
    非 ASCII，且部分 bash 的 /dev/stdin 读管道受限）。"""
    r = subprocess.run(["bash", "-n", "-c", script],
                       capture_output=True, timeout=30)
    if r.returncode != 0:
        return r.stderr.decode("utf-8", errors="replace").strip()[:300] or "bash -n 未通过"
    return ""


def check_file(path: str):
    """检查单个 workflow，返回错误行列表（空 = 通过）"""
    errors = []
    try:
        blocks = list(iter_run_blocks(path))
    except Exception as e:
        return [f"{path}: YAML 解析失败: {e}"]
    if not bash_available():
        print("WARN: 本机无 bash，仅校验 YAML 解析，跳过内嵌脚本检查")
        return errors
    for job_id, idx, name, script in blocks:
        err = bash_check(script)
        if err:
            errors.append(f"{path} job={job_id} step#{idx}({name}): bash 语法错误: {err}")
    return errors


def main(argv=None) -> int:
    patterns = argv[1:] if argv and len(argv) > 1 else [".github/workflows/*.yml"]
    paths = sorted({p for pat in patterns for p in glob.glob(pat)})
    if not paths:
        print(f"ERROR: 未匹配到任何 workflow 文件: {patterns}")
        return 2
    errors = []
    n_blocks = 0
    for path in paths:
        try:
            n_blocks += len(list(iter_run_blocks(path)))
        except Exception:
            pass
        errors.extend(check_file(path))
    if errors:
        print("\n".join(errors))
        return 1
    print(f"workflows 校验通过: {len(paths)} 个文件 / {n_blocks} 个内嵌脚本")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
