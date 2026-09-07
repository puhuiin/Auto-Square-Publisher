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
from typing import Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: 需要 pyyaml（CI 步骤内 pip 安装，本地 pip install pyyaml）")
    sys.exit(2)

GHA_EXPR_RE = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
GHA_PLACEHOLDER = "__GHA_EXPR__"

# 探测用最小脚本：既能验证"这个 bash 真的能执行 -n -c"，又无任何副作用
_PROBE_SCRIPT = "true"

# bash 可用性探测结果缓存（每个 run 块都 spawn 一次进程太贵，探测一次即可）
_BASH_CACHE: dict = {"probed": False, "path": None}


def reset_bash_cache() -> None:
    """清空解析缓存（测试与长驻进程切换环境时用）"""
    _BASH_CACHE.update({"probed": False, "path": None})


def _decode(data: bytes) -> str:
    """宽容解码子进程输出。

    Windows 上被安全策略/权限拒绝的进程常以 UTF-16LE 吐本地化错误（如"拒绝访问。"），
    此前一律 utf-8+replace 解码，得到满屏 '�' 的乱码并被当成"bash 语法错误"，
    排障方向直接被带偏。故先按 BOM/空字节嗅探 UTF-16，再回落 utf-8 / 本地代码页。
    """
    if not data:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16", errors="replace")
        except Exception:
            pass
    if b"\x00" in data:
        try:
            return data.decode("utf-16-le", errors="replace")
        except Exception:
            pass
    for enc in ("utf-8", "mbcs" if os.name == "nt" else "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _bash_candidates():
    """bash 候选路径：显式指定 > PATH > Windows 常见 Git Bash 安装位"""
    env_path = os.getenv("BASH_PATH", "").strip()
    if env_path:
        yield env_path
    on_path = shutil.which("bash")
    if on_path:
        yield on_path
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")):
            for rel in (r"Git\usr\bin\bash.exe", r"Git\bin\bash.exe"):
                yield os.path.join(base, rel)


def _bash_works(path: str) -> bool:
    """真机验证：该 bash 能否执行 `bash -n -c true`。

    只看 `bash --version` 不够——PATH 上可能是 WSL 启动器，或被安全策略/权限
    拦住（--version 放行、执行任意命令被拒），两种情况下 `bash -n` 都会以非 0
    退出并吐本地化错误，被本脚本误读成 workflow 里的 shell 语法错误。
    """
    try:
        r = subprocess.run([path, "-n", "-c", _PROBE_SCRIPT],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def resolve_bash() -> Optional[str]:
    """返回可用的 bash 路径，无可用者返回 None（探测结果进程内缓存）"""
    if _BASH_CACHE["probed"]:
        return _BASH_CACHE["path"]
    _BASH_CACHE["probed"] = True
    for cand in _bash_candidates():
        if cand and os.path.exists(cand) and _bash_works(cand):
            _BASH_CACHE["path"] = cand
            return cand
    _BASH_CACHE["path"] = None
    return None


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
    """本机是否存在"能真正执行 -n -c"的 bash（仅 PATH 里有同名可执行文件不算）"""
    return resolve_bash() is not None


def bash_check(script: str) -> str:
    """bash -n 检查一段脚本，返回 '' 表示通过，否则返回 stderr 摘要。

    用 `bash -n -c` 直接验字符串：不落地临时文件（Windows 上系统临时目录
    的盘符路径会被 bash 反斜杠转义误读），也不走 stdin（本机 GBK 会炸
    非 ASCII，且部分 bash 的 /dev/stdin 读管道受限）。

    无法校验时（无可用 bash / 进程被拒绝）一律返回 ''：宁可漏检，也绝不把
    环境问题伪装成 workflow 的语法错误——那会让维护者去改根本没错的文件。
    """
    exe = resolve_bash()
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "-n", "-c", script],
                           capture_output=True, timeout=30)
    except Exception as e:
        return f"bash 执行失败: {e}"
    if r.returncode == 0:
        return ""
    return _decode(r.stderr).strip()[:300] or "bash -n 未通过"


def check_file(path: str):
    """检查单个 workflow，返回错误行列表（空 = 通过）"""
    errors = []
    try:
        blocks = list(iter_run_blocks(path))
    except Exception as e:
        return [f"{path}: YAML 解析失败: {e}"]
    if not bash_available():
        # 说清"为什么不查"：此前只说"无 bash"，而实际是 PATH 上有 bash 却执行不了，
        # 维护者会误以为装个 Git Bash 就好，反复排查方向错误。
        print(f"WARN: 本机无可用 bash（PATH/已知安装位上的 bash 均无法执行 "
              f"`bash -n -c true`），仅校验 YAML 解析，跳过 {len(blocks)} 个内嵌脚本"
              f"（可用 BASH_PATH 显式指定）")
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
    skipped = "" if bash_available() else "（内嵌脚本未校验：本机无可用 bash）"
    print(f"workflows 校验通过: {len(paths)} 个文件 / {n_blocks} 个内嵌脚本{skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
