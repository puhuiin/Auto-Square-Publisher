# -*- coding: utf-8 -*-
"""
Workflow 契约测试（独立文件）：Action pin 锁定 + dependabot 有效 + 注解可求值。

独立成文件只因 tests/test_core.py 正有他人在途大改动，避免交织；
内容上与主套件同级，CI 里一起跑（见 ci.yml）。
"""
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = [
    os.path.join(REPO_ROOT, ".github", "workflows", "auto_post.yml"),
    os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"),
]
PIN_RE = re.compile(r"@[0-9a-f]{40}\b")


def _uses_value(line):
    m = re.search(r"uses:\s*(\S+)", line)
    return m.group(1) if m else ""


class TestActionPinning(unittest.TestCase):
    """所有 uses: 必须锁定到 commit SHA（防投毒；dependabot 负责跟进版本）"""

    def test_pin_regex(self):
        self.assertTrue(PIN_RE.search(
            "uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0"))
        for bad in ("uses: actions/checkout@v4",
                    "uses: actions/checkout@main",
                    "uses: actions/checkout@v4.4.0"):
            self.assertIsNone(PIN_RE.search(bad), bad)

    def test_all_uses_pinned_to_sha(self):
        checked, floating = 0, []
        for path in WORKFLOWS:
            with open(path, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if "uses:" not in line:
                        continue
                    val = _uses_value(line)
                    if not val or val.startswith(".") or val.startswith("/"):
                        continue  # 本地 action 无需 pin
                    checked += 1
                    if not PIN_RE.search(line):
                        floating.append(f"{os.path.basename(path)}:{i}: {line.strip()}")
        self.assertGreater(checked, 0, "没找到任何 uses: 行，测试本身可能已失效")
        self.assertEqual(floating, [], "浮动引用必须锁定到 commit SHA: %r" % (floating,))

    def test_dependabot_config_valid(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("未安装 pyyaml（仅 CI 校验需要）")
        path = os.path.join(REPO_ROOT, ".github", "dependabot.yml")
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        self.assertIsInstance(doc, dict)
        ecosystems = {u.get("package-ecosystem") for u in doc.get("updates", [])}
        self.assertIn("github-actions", ecosystems)
        self.assertIn("pip", ecosystems)


class TestAnnotationResolvable(unittest.TestCase):
    """tripwire：注解必须在 CI 的 Python 3.11 下也可求值。

    本地 3.14 对注解惰性求值，会掩盖缺失的 typing import——validator 曾因此
    在 3.11 上 import 即 NameError，而 py_compile 照样通过。用 get_type_hints
    强制求值，把版本差异变成确定性红灯。"""

    def _load_validator(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("未安装 pyyaml（validator 自身要求）")
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "validate_workflows_tripwire",
            os.path.join(REPO_ROOT, "scripts", "validate_workflows.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_validator_annotations_resolve(self):
        import inspect
        import typing
        mod = self._load_validator()
        fns = [(n, o) for n, o in vars(mod).items() if inspect.isfunction(o)]
        self.assertTrue(fns, "validator 里一个函数都没找到，测试本身可能已失效")
        bad = []
        for name, fn in fns:
            try:
                typing.get_type_hints(fn)
            except Exception as e:
                bad.append(f"{name}: {e}")
        self.assertEqual(bad, [], "注解含未定义名（3.11 下 import 即炸）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
