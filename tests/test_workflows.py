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
    os.path.join(REPO_ROOT, ".github", "workflows", "video_publish.yml"),
]
PIN_RE = re.compile(r"@[0-9a-f]{40}\b")
# R161：读写 sent_cache 等运行状态的"运行时"工作流（ci.yml 是回归测试用途，检出
# 事件 SHA 恰当，不在其列）
RUNTIME_WORKFLOWS = [
    os.path.join(REPO_ROOT, ".github", "workflows", "auto_post.yml"),
    os.path.join(REPO_ROOT, ".github", "workflows", "video_publish.yml"),
]


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


class TestRuntimeWorkflowCheckoutRef(unittest.TestCase):
    """R161：运行时工作流的 checkout 必须显式 ref: main。

    生产事故（2026-09-14 01:33/01:36 SHIB 同 news_id 双发实录）：并发组
    （cancel-in-progress: false）只串行化执行，不串行化状态基线——排队的
    dispatch 运行按事件创建时刻的旧 SHA 检出，前一 schedule 运行刚推送的
    sent_cache 记录对它不可见，is_cached 查空 → 同条目重复发布。检出
    main 当前 tip 保证状态基线包含前一运行的最终推送。"""

    def _checkout_steps(self, path):
        try:
            import yaml
        except ImportError:
            self.skipTest("未安装 pyyaml（仅 CI 校验需要）")
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        steps = []
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if str(step.get("uses", "")).startswith("actions/checkout"):
                    steps.append(step)
        return steps

    def test_runtime_workflows_checkout_main_tip(self):
        for path in RUNTIME_WORKFLOWS:
            name = os.path.basename(path)
            steps = self._checkout_steps(path)
            self.assertTrue(steps, f"{name} 缺少 checkout 步骤")
            for step in steps:
                self.assertEqual(
                    (step.get("with") or {}).get("ref"), "main",
                    f"{name} checkout 必须显式 ref: main（排队运行按事件 SHA "
                    f"检出会读到前一运行推送前的旧状态，R161 重复发布事故根因）")

    def test_ci_checkout_stays_on_event_sha(self):
        steps = self._checkout_steps(
            os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"))
        self.assertTrue(steps, "ci.yml 缺少 checkout 步骤")
        for step in steps:
            self.assertNotEqual(
                (step.get("with") or {}).get("ref"), "main",
                "ci.yml 应检出事件 SHA（测试被推送的那个提交），"
                "运行时 ref:main 约束不适用于回归测试工作流")


class TestCiLlmLiveSmokeStep(unittest.TestCase):
    """R184b：llm-live job 自 e987493 引入后**从未成功过**——2026-09-14 首次
    schedule 触发即红，且日志里连 healthcheck 输出都没有（`set -e` 下命令替换
    返回非零直接中断脚本，只剩 "exit code 1"）。两个独立缺陷：① CI 不持有
    SQUARE_API_KEY，healthcheck 的必填检查恒 ✗；② 未吞退出码，诊断信息全丢。"""

    def _llm_live_run_block(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("未安装 pyyaml（仅 CI 校验需要）")
        path = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        steps = (doc.get("jobs") or {}).get("llm-live", {}).get("steps") or []
        for step in steps:
            if "healthcheck" in str(step.get("run") or ""):
                return step
        self.fail("ci.yml 的 llm-live job 里找不到 healthcheck 步骤")

    def test_smoke_swallows_exit_code_to_keep_diagnostics(self):
        step = self._llm_live_run_block()
        run = step["run"]
        self.assertIn("|| true", run,
                      "命令替换必须吞掉 healthcheck 的非零退出码，"
                      "否则 set -e 中断脚本、输出丢失（首次运行即踩）")

    def test_smoke_does_not_require_square_key(self):
        step = self._llm_live_run_block()
        env = step.get("env") or {}
        self.assertEqual(env.get("PUBLISH_PLATFORMS"), "okx_draft",
                         "llm-live 只验 LLM 通道且 CI 无 SQUARE_API_KEY，"
                         "必须用 okx_draft 让该检查走 ⊘ 分支而非 ✗")

    def test_healthcheck_marks_square_key_optional_when_binance_off(self):
        """行为侧锁：PUBLISH_PLATFORMS 不含 binance 时，缺 Key 不得计为故障。"""
        import importlib
        spec = importlib.util.spec_from_file_location(
            "_hc_mod", os.path.join(REPO_ROOT, "main.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.PUBLISH_PLATFORMS = ["okx_draft"]
        printed = []
        with patch.object(mod, "_safe_print", side_effect=lambda *a: printed.append(" ".join(map(str, a)))), \
             patch.object(mod, "MultiLLMEngine", side_effect=RuntimeError("skip")), \
             patch.object(mod, "MarketDataProvider") as mock_mdp, \
             patch.object(mod, "NewsFetcher"), \
             patch.object(mod, "Notifier"), \
             patch.object(mod.SymbolValidator, "get_valid_symbols",
                          return_value={f"T{i}" for i in range(200)}):
            # 行情/FNG 全 mock：本测试只关心 SQUARE_API_KEY 的 ⊘ 分支，不走网络
            mock_mdp.get_fear_and_greed.return_value = "50/100"
            mock_mdp.get_trending_symbols.return_value = []
            with self.assertRaises(SystemExit) as cm:
                mod.run_healthcheck()
        text = "\n".join(printed)
        self.assertIn("SQUARE_API_KEY", text)
        self.assertIn("无需配置", text)
        self.assertNotIn("未配置，发帖必需", text)
        # 故障仅来自被 mock 掉的引擎构建，不含 SQUARE_API_KEY
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("有 1 项故障", text)


class TestCiRunsWholeSuite(unittest.TestCase):
    """R185：CI 必须跑完整测试目录，不能逐个列举文件名。

    tests/test_cost_analysis.py 自 R120 加入（12 个测试）起从未被 CI 执行——
    同期 cost_analysis 面板连爆两个口径 bug（R120 stage 过滤、R167 dry 字段
    漂移），都是靠生产遥测反查而非 CI 拦下。列举式清单新增文件即静默漏跑。"""

    def _ci_test_run(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("未安装 pyyaml（仅 CI 校验需要）")
        path = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        steps = (doc.get("jobs") or {}).get("test", {}).get("steps") or []
        for step in steps:
            if "回归测试" in str(step.get("name") or ""):
                return str(step.get("run") or "")
        self.fail("ci.yml 的 test job 里找不到回归测试步骤")

    def test_ci_discovers_all_test_files(self):
        run = self._ci_test_run()
        self.assertIn("unittest discover", run,
                      "CI 必须用 discover 跑整个 tests/，逐个列举会在新增文件时漏跑")

    def test_ci_does_not_pipe_away_exit_code(self):
        """`cmd | tail` 的退出码取自 tail——测试失败会被静默吞成绿灯。"""
        run = self._ci_test_run()
        self.assertNotIn("| tail", run)
        self.assertNotIn("| head", run)

    def test_every_test_file_is_discoverable(self):
        """tests/ 下每个 test_*.py 都必须能被 discover 找到（文件名契约）。"""
        tests_dir = os.path.join(REPO_ROOT, "tests")
        names = [n for n in os.listdir(tests_dir)
                 if n.startswith("test_") and n.endswith(".py")]
        self.assertGreaterEqual(len(names), 4,
                                f"测试文件数异常偏少: {names}")
        for n in names:
            self.assertRegex(n, r"^test_[a-z0-9_]+\.py$",
                             f"{n} 不符合 discover 的 test_*.py 命名契约")

    def test_compile_step_uses_globs(self):
        """py_compile 列举式清单同样会漏新文件（R185 漏了 cost_analysis 两个）。"""
        try:
            import yaml
        except ImportError:
            self.skipTest("未安装 pyyaml（仅 CI 校验需要）")
        path = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        steps = (doc.get("jobs") or {}).get("test", {}).get("steps") or []
        compile_runs = [str(s.get("run") or "") for s in steps
                        if "编译" in str(s.get("name") or "")]
        self.assertTrue(compile_runs, "找不到语法编译检查步骤")
        run = compile_runs[0]
        self.assertIn("tests/*.py", run, "测试文件必须用通配而非逐个列举")
        self.assertIn("scripts/*.py", run, "脚本必须用通配而非逐个列举")


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


class TestReadmeEnvCoverage(unittest.TestCase):
    """R102 文档防漂移：main.py 读取的每个用户可配置环境变量都必须在 README
    出现——40+ 轮迭代里 MAX_POSTS_PER_RUN/LOG_LEVEL/通知渠道/模型覆盖等
    16 个变量静默失文档。新增 env 读取时本测试强制同步文档。"""

    def test_all_user_facing_env_vars_documented(self):
        import re
        code_path = os.path.join(REPO_ROOT, "main.py")
        with open(code_path, encoding="utf-8") as f:
            code = f.read()
        used = set()
        for pat in (r'os\.getenv\(\s*"([A-Z][A-Z0-9_]+)"',
                    r'_env_int\(\s*"([A-Z][A-Z0-9_]+)"',
                    r'_env_float\(\s*"([A-Z][A-Z0-9_]+)"'):
            used |= set(re.findall(pat, code))
        # CI 内部注入（Actions 运行器提供，非用户配置面）
        internal = {v for v in used if v.startswith("GITHUB_")}
        user_facing = used - internal
        self.assertTrue(user_facing, "正则失效：至少应识别出运行参数")
        readme_path = os.path.join(REPO_ROOT, "README.md")
        with open(readme_path, encoding="utf-8") as f:
            readme = f.read()
        missing = sorted(v for v in user_facing if v not in readme)
        self.assertEqual(missing, [],
                         f"以下环境变量已实现但 README 未文档化: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
