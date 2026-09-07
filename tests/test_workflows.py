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
from unittest.mock import patch

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


def _load_fallback():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "notify_fallback",
        os.path.join(REPO_ROOT, "scripts", "notify_fallback.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeResp:
    """可重用的 urllib 假响应（status + body 可配，支持 with 语句）"""

    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _router_ok(request, *args, **kwargs):
    url = request.full_url if hasattr(request, "full_url") else str(request)
    if "sctapi.ftqq.com" in url:
        return _FakeResp(200, b'{"code":0,"message":""}')
    if "pushplus" in url:
        return _FakeResp(200, b'{"code":200,"msg":"ok"}')
    if "api.day.app" in url:
        return _FakeResp(200, b'{"code":200,"message":"ok"}')
    if "api.telegram.org" in url:
        return _FakeResp(200, b'{"ok":true,"result":{}}')
    return _FakeResp(204, b"")


FULL_ENV = {
    "SERVERCHAN_KEY": "k1", "PUSHPLUS_TOKEN": "k2", "BARK_KEY": "k3",
    "TELEGRAM_BOT_TOKEN": "k4", "TELEGRAM_CHAT_ID": "k5",
    "WEBHOOK_URL": "https://hooks.example/x",
}
CHANNEL_KEYS = tuple(FULL_ENV)


class TestFallbackNotifier(unittest.TestCase):
    """兜底通报器：纯标准库、各通道独立成败、业务码也要验"""

    def test_all_channels_attempted_and_reported(self):
        mod = _load_fallback()
        with patch("urllib.request.urlopen", side_effect=_router_ok) as mock_open:
            results = mod.send_fallback("t", "m", env=dict(FULL_ENV))
        self.assertEqual(results, {"Server酱": True, "PushPlus": True, "Bark": True,
                                   "Telegram": True, "Webhook": True})
        self.assertEqual(mock_open.call_count, 5)

    def test_single_failure_does_not_block_others(self):
        import urllib.error
        mod = _load_fallback()

        def _flaky(request, *args, **kwargs):
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if "sctapi.ftqq.com" in url:
                raise urllib.error.URLError("dns blip")
            return _router_ok(request)

        with patch("urllib.request.urlopen", side_effect=_flaky):
            results = mod.send_fallback("t", "m", env=dict(FULL_ENV))
        self.assertFalse(results["Server酱"])
        self.assertTrue(all(v for k, v in results.items() if k != "Server酱"))

    def test_business_code_mismatch_counts_as_failure(self):
        mod = _load_fallback()
        bad = _FakeResp(200, b'{"code":1,"message":"bad key"}')
        with patch("urllib.request.urlopen", return_value=bad):
            results = mod.send_fallback("t", "m", env={"SERVERCHAN_KEY": "k"})
        self.assertEqual(results, {"Server酱": False})

    def test_no_channels_configured(self):
        mod = _load_fallback()
        with patch("urllib.request.urlopen") as mock_open:
            self.assertEqual(mod.send_fallback("t", "m", env={}), {})
        mock_open.assert_not_called()

    def test_main_exit_zero_with_fake_env(self):
        mod = _load_fallback()
        saved = {k: os.environ.pop(k, None) for k in CHANNEL_KEYS}
        try:
            with patch.dict(os.environ, {
                    **FULL_ENV,
                    "GITHUB_SERVER_URL": "https://github.com",
                    "GITHUB_REPOSITORY": "a/b", "GITHUB_RUN_ID": "42",
                    "EVENT_NAME": "schedule"}), \
                 patch("urllib.request.urlopen", side_effect=_router_ok):
                self.assertEqual(mod.main([]), 0)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class TestImportHealth(unittest.TestCase):
    """解释器级导入健康：前向引用注解在 3.11 下炸 import，在 3.14 下静默。

    2026-09-05 起一个后定义类写进函数注解，本地 3.14 全绿、线上/CI 全红，
    定时任务连跪到用户贴日志才发现。用子进程真实 import（非 get_type_hints，
    后者在模块加载完后求值，查不出前向引用），在 CI 的 3.11 上就是真刀真枪。
    """

    def _check_import(self, relpath):
        import subprocess
        code = ("import runpy; runpy.run_path(%r, run_name='__not_main__')"
                % relpath.replace("\\", "/"))
        r = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(
            r.returncode, 0,
            f"{relpath} 子进程导入失败:\n{(r.stderr or '')[-800:]}")

    def test_main_imports_clean(self):
        self._check_import("main.py")

    def test_scripts_import_clean(self):
        for rel in ("scripts/validate_workflows.py", "scripts/git_state_merge.py",
                    "scripts/notify_fallback.py", "scripts/metrics_report.py"):
            with self.subTest(script=rel):
                self._check_import(rel)

    def test_future_annotations_present_in_main(self):
        # 治本：注解惰性化后，前向引用在任何版本都安全；此行被删必须红灯
        with open(os.path.join(REPO_ROOT, "main.py"), encoding="utf-8") as f:
            head = "".join(f.readline() for _ in range(60))
        self.assertIn("from __future__ import annotations", head)


class TestFailureNotifyWiring(unittest.TestCase):
    """auto_post 兜底步骤接线：id、failure 条件、密钥透传缺一不可"""

    def _steps(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("未安装 pyyaml（仅 CI 校验需要）")
        path = os.path.join(REPO_ROOT, ".github", "workflows", "auto_post.yml")
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        return doc["jobs"]["run-poster"]["steps"]

    def test_step_ids_present(self):
        steps = self._steps()
        by_id = {s.get("id"): s.get("name", "") for s in steps if isinstance(s, dict)}
        self.assertIn("install", by_id)
        self.assertIn("poster", by_id)

    def test_failure_step_wired(self):
        steps = self._steps()
        cands = [s for s in steps
                 if isinstance(s, dict) and s.get("if") == "failure()"]
        self.assertEqual(len(cands), 1, "有且仅有一个 failure 兜底步骤")
        step = cands[0]
        self.assertIn("notify_fallback.py", step.get("run", ""))
        env = step.get("env", {})
        for k in ("SERVERCHAN_KEY", "PUSHPLUS_TOKEN", "BARK_KEY",
                  "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WEBHOOK_URL",
                  "EVENT_NAME", "INSTALL_RESULT", "POSTER_RESULT"):
            self.assertIn(k, env, f"兜底步骤缺环境变量 {k}")
        self.assertIn("steps.poster.conclusion", env["POSTER_RESULT"])
        self.assertIn("steps.install.conclusion", env["INSTALL_RESULT"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
