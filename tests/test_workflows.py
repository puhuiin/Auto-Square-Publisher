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

# ★★ R799：时钟窗口型报告器 —— 窗口是 `now - hours ~ now` 反推出来的。
#   audit_perf.report(rows, hours, …) 与 daily_report.build_report(rows, hours)
#   都属此类。喂给它们的行**不得**用硬编码绝对日期（见 TestNoWallClockWindowedTests）。
#   ⚠️ metrics_report.quality_scan(rows, window=N) 不在此列：它按**行数**取尾部。
CLOCK_WINDOWED_CALLS = ("report", "build_report")
WORKFLOWS = [
    os.path.join(REPO_ROOT, ".github", "workflows", "auto_post.yml"),
    os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"),
    os.path.join(REPO_ROOT, ".github", "workflows", "video_publish.yml"),
    # ★ R798：R723 新建的 daily_report.yml 此前不在白名单 ⇒ 它是全仓**唯一**
    #   还在用 @v4/@v5 浮标的工作流，而钉在靶上的契约测试看不见它。
    os.path.join(REPO_ROOT, ".github", "workflows", "daily_report.yml"),
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

    def test_r797_webhook_dingtalk_errcode_counts_as_failure(self):
        """★★ R797：Webhook 通道必须验钉钉 errcode（R781/R782 同款 bug 的最后一道）。

        生产实证：WEBHOOK_URL 就是钉钉机器人，token 失效已 6 天
        （{"errcode":300005}）。旧实现只验 HTTP 2xx ⇒ 兜底通报在**主流程
        已挂掉**的最糟时刻报告"已发"而用户什么都没收到。
        """
        mod = _load_fallback()
        dead = _FakeResp(200, b'{"errcode":300005,"errmsg":"token is not exist"}')
        with patch("urllib.request.urlopen", return_value=dead):
            results = mod.send_fallback("t", "m", env={"WEBHOOK_URL": "https://oapi.dingtalk.com/x"})
        self.assertEqual(results, {"Webhook": False},
                         "钉钉 token 失效被判成已发 ⇒ 最后一道通报也静默失效")

    def test_r797_webhook_other_shapes_still_pass(self):
        """★ 边界：非钉钉形态（飞书 code:0 / 非 JSON / 空体）不得被新判据误杀"""
        mod = _load_fallback()
        for body in (b'{"code":0}', b"OK", b""):
            with patch("urllib.request.urlopen", return_value=_FakeResp(200, body)):
                results = mod.send_fallback("t", "m", env={"WEBHOOK_URL": "https://x.example/hook"})
            self.assertEqual(results, {"Webhook": True},
                             f"非钉钉形态 {body!r} 被误判成失败")

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

    def test_no_unimported_typing_aliases(self):
        """★★★ **模块级注解不得依赖未导入的 `typing` 别名**（2026-10-06实测）。

        ★ 事故现场：`_PURE_TICKER_CACHE: Dict[str, tuple] = {}`
          而本模块**没有 import typing**
          ⇒ Python **3.14 静默通过**（本地全绿）
          ⇒ Python **3.11（CI 版本）直接炸 import**：
             NameError: name 'Dict' is not defined
          ⇒ **定时任务连跪到 CI 才发现**（R670 同款，但更隐蔽：
             它躲过了"本地 py_compile"和"本地全量测试"）

        ⇒ 判据：扫所有 `.py` 的**模块级**注解行，
          出现 `Dict[`/`List[`/`Tuple[` 等 `typing` 别名
          而该文件**没有** `import typing` / `from typing import…` ⇒ 失败。
        ⚠️ 必须**扫全仓**（不止改过的文件）——
           那个 `Dict` 是**早前提交**带进来的，一直没被发现。
        """
        import os
        import re
        aliases = ("Dict", "List", "Tuple", "Set", "FrozenSet", "Optional",
                   "Union", "Any", "Callable", "Iterable", "Sequence")
        offenders = []
        for root, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "__pycache__", "drafts", ".workbuddy")]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(root, fn)
                with open(fp, encoding="utf-8") as f:
                    src = f.read()
                if re.search(r"^\s*(from typing import|import typing)",
                             src, re.M):
                    continue
                for i, ln in enumerate(src.split("\n"), 1):
                    st = ln.strip()
                    # 只看**模块级**（无缩进）且带**变量注解**的行
                    if st.startswith(("#", '"', "'")) or ln[:1] in (" ", "\t"):
                        continue
                    for al in aliases:
                        if re.search(r":\s*%s\[" % al, st):
                            offenders.append("%s:%d%s" % (
                                os.path.relpath(fp, REPO_ROOT), i, st[:40]))
                            break
        self.assertFalse(offenders,
                         "★ 模块级注解用了未导入的 typing 别名"
                         "（3.11 CI 会炸 import）:\n  "
                         + "\n   ".join(offenders[:8]))

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


class TestNoWallClockWindowedTests(unittest.TestCase):
    """★★ R799：喂给时钟窗口型报告器的用例**不得**写死绝对日期。

    ★ 为什么需要这条守卫（取证全过程写在 tests/test_core.py 的 `_ago` 与
      scripts/audit_perf.py ⑥½ 的注释里）：
      · 2026-10-19T11:00Z，R783 面板的 3 条用例集体变红——它们的行 ts 写死
        2026-10-18T10:00/11:00，正好满 24h 被窗口滑出去。写就时是绿的，
        到点自己变红：**每天固定把 CI 染红**，真信号被噪音淹没。
      · 更隐蔽的一半：R770 的"真没有 lang 字段"用例的行早已老化出 240h 窗口，
        它此刻是在**空窗口**路径上通过的（实测输出与空窗逐字相同）——不红，
        但不再走到它声称的分支，守护静默失效。它连带把面板对空窗口的误报
        （"遥测里没有 lang 字段"）也一起盖住了。

    ★ 判据为什么是 AST 可达性而不是全仓扫时间戳字面量：
      R750 的 ⑥ 段走 quality_scan(window=20)，是**按行数**取尾部、与时间无关
      ⇒ 它对输入日期不敏感，全仓扫描会误报。故：只从**真的调用了窗口型报告器
      的函数**出发，沿 `self.<方法>()` 递归收集该用例触及的代码，再查其中有无
      绝对 ISO 日期字面量。这样 R735 里纯函数 `_exposure_days("2026-10-01", …)`
      这类合法用法不会被牵连（那两个 ts 是显式入参、不经过 now）。
    """

    # 允许清单：理由必须写清，否则守卫会退化成"看到就关"的装饰品
    ALLOW = {
        # key = "<文件>:<类名>.<测试方法名>"（模块级函数写 "<文件>:<函数名>"）：
        #       允许存在的绝对日期调用路径及原因。空 = 当前无豁免。
    }

    # ⚠️ 只要求"以完整日期开头"，不要求后面的时分是**数字**：取证时踩到过——
    #   宽松前的模式要求 `T\d\d:\d\d`，于是 `"2026-10-19T0%d:00:00+00:00" % i`
    #   这种**格式化**写死的日期被漏掉（守卫生效但咬不住，比没有更危险）。
    ISO_LITERAL = re.compile(r"^20\d\d-\d\d-\d\dT")

    @staticmethod
    def _sources(path):
        """把测试文件切成 {(类名|None, 函数名): [源码片段, ...]}。

        ⚠️ 必须按**类**限定，不能只按函数名建表：本仓多个测试类各有同名辅助方法
           （`_rows` / `_dr` / `_ap` …），只按函数名做 dict 会被后一个定义**覆盖**
           前一个 ⇒ 解析某用例的闭包时挂到别人家的辅助函数上：既把无关字面量算
           进来（假阳性），又把真正的字面量顶掉（假阴性）。R799 放宽 ISO 正则后
           首次跑就撞上这个**自身**缺陷——4 条用例被误报，真凶是它们的
           `self._rows` 被 TestR758 那个写死日期的 `_rows` 顶了。
        ★ 值用 list 是因为**类名本身也可能重复**（本文件里
           TestR672ParagraphLengthGuardrail / TestGuardrailInsertionSafety 各定义
           了两次）；有歧义就把候选**全收**（保守并集），宁可多收也不漏——
           漏报比误报危险得多。
        """
        import ast
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        lines = src.splitlines()
        out = {}

        def add(cls, node):
            seg = "\n".join(lines[node.lineno - 1:node.end_lineno])
            out.setdefault((cls, node.name), []).append(seg)

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add(None, node)
            elif isinstance(node, ast.ClassDef):
                # ast.walk ⇒ 方法内的嵌套函数同样归到该类名下（它们也可能持有
                # 喂给窗口报告器的字面量，漏掉即假阴性）。
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        add(node.name, sub)
        return out

    @classmethod
    def _windowed_and_iso(cls, seg):
        """返回 (是否触及窗口型调用, 是否有绝对 ISO 字面量)。"""
        win = bool(re.search(r"\b(?:%s)\s*\(" % "|".join(CLOCK_WINDOWED_CALLS), seg))
        iso = False
        for line in seg.splitlines():
            for lit in re.findall(r'"([^"]*)"', line) + re.findall(r"'([^']*)'", line):
                if cls.ISO_LITERAL.match(lit):
                    iso = True
        return win, iso

    def test_windowed_tests_use_relative_timestamps(self):
        offenders = []
        n_windowed = 0
        for fname in ("test_core.py", "test_metrics_report.py"):
            path = os.path.join(REPO_ROOT, "tests", fname)
            funcs = self._sources(path)
            for key in list(funcs):
                cls, leaf = key
                if not leaf.startswith("test_"):
                    continue
                # 沿 self.<helper>() 递归收集闭包（R783 的窗口调用在 _run 里、
                # 字面量在测试函数里；只看单函数会漏）。解析**限定在同类内**，
                # 同类没有才退回模块级；同名歧义时并集（见 _sources 说明）。
                seen, stack = set(), [key]
                win = iso = False
                while stack:
                    k = stack.pop()
                    if k in seen:
                        continue
                    seen.add(k)
                    for segn in funcs.get(k, []):
                        _w, _i = self._windowed_and_iso(segn)
                        win = win or _w
                        iso = iso or _i
                        for callee in re.findall(r"self\.(\w+)\s*\(", segn):
                            if (cls, callee) in funcs:
                                stack.append((cls, callee))
                            if (None, callee) in funcs:
                                stack.append((None, callee))
                if win:
                    n_windowed += 1
                    label = "%s:%s.%s" % (fname, cls, leaf) if cls else \
                            "%s:%s" % (fname, leaf)
                    if iso and label not in self.ALLOW:
                        offenders.append(label)
        self.assertEqual(
            offenders, [],
            "以下用例把硬编码绝对日期喂给了时钟窗口型报告器（窗口=now-hours~now），"
            "两种后果都必须避免：① 窗口滑走 ⇒ CI 到点自己变红（R780b）；"
            "② 若断言撞巧走的是按行口径（quality_scan），则不红，但报告里的窗口面"
            "已滑成空窗、与断言口径脱节（R770 实测就是这样静默失效的）。"
            "一律改用模块级 _ago(小时数)：%r"
            % (offenders,))
        # 哨兵：若正则/遍历失效，n_windowed 会掉到 0，守卫静默变成空转
        #（R712 家族：守卫失效而不报警，比没有守卫更糟）。此处断言的是
        #「识别到的**窗口型用例数**」而非「违规数」——全部修好后违规数本就该是 0。
        self.assertGreater(n_windowed, 3,
                           "只识别到 %d 条窗口型用例 ⇒ 判据本身已失效" % n_windowed)


class TestDailyReportSecretWiring(unittest.TestCase):
    """R798：R781 的兜底通道必须在部署侧真的接上，否则是死代码。

    ★ 发现路径（读生产产物，不是读代码）：10-18 12:39Z 的 Daily Report 运行
      日志里只有**一次**发送、只出现一行 `{"errcode":300005,...}`，且
      conclusion=success。对照 daily_report.yml 才看出：步骤只导出
      DINGTALK_WEBHOOK_URL ⇒ daily_report.py 的 `if fallback` 恒假 ⇒ 兜底从未
      执行，收尾那句"主通道与兜底通道均失败"是它没测过的结论。
    本测试把"部署侧接线"钉死：契约测试此前只验脚本逻辑
    （test_metrics_report 的 R781 用例自己塞了 WEBHOOK_URL 环境变量），
    验不出生产没透传——**用例能给能力，部署不给**，这类缺口只有查 workflow
    文本才能封。
    """

    def _send_step(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("未安装 pyyaml（仅 CI 校验需要）")
        path = os.path.join(REPO_ROOT, ".github", "workflows", "daily_report.yml")
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        steps = doc["jobs"]["report"]["steps"]
        cands = [s for s in steps if isinstance(s, dict)
                 and "daily_report.py --hours" in (s.get("run") or "")]
        self.assertEqual(len(cands), 1, "应恰有一个实发步骤")
        return cands[0]

    def test_send_step_exports_both_channels(self):
        env = self._send_step().get("env", {}) or {}
        self.assertIn("DINGTALK_WEBHOOK_URL", env, "主通道 secret 必须透传")
        self.assertIn("WEBHOOK_URL", env,
                      "★ 不给 WEBHOOK_URL ⇒ R781 的兜底通道在生产上是死代码")

    def test_fallback_secret_is_a_secret_not_a_literal(self):
        env = self._send_step().get("env", {}) or {}
        for k in ("DINGTALK_WEBHOOK_URL", "WEBHOOK_URL"):
            v = str(env.get(k, ""))
            self.assertIn("secrets.", v, f"{k} 必须走 secret（R619 教训）")
            self.assertNotIn("http", v, f"{k} 不得写明文 URL")


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


class TestWatchdogCannotBlockPosting(unittest.TestCase):
    """R6：看门狗只报警，绝不能阻塞发帖主流程"""

    def _text(self):
        path = os.path.join(REPO_ROOT, ".github", "workflows", "auto_post.yml")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_watchdog_step_continue_on_error(self):
        text = self._text()
        # 锚定真正执行脚本的那一行（注释里也出现过文件名，不能用它定位）
        idx = text.index("run: python scripts/schedule_watchdog.py")
        block = text[max(0, idx - 1500):idx]
        self.assertIn("continue-on-error: true", block,
                      "看门狗步骤必须 continue-on-error，否则脚本一抛错就会跳过发帖步骤")

    def test_posting_step_not_gated_on_watchdog(self):
        text = self._text()
        idx = text.index("python main.py")
        block = text[max(0, idx - 1200):idx]
        self.assertNotIn("steps.watchdog", block, "发帖步骤不应依赖看门狗步骤的结果")


class TestStateResultWiredToFallbackNotify(unittest.TestCase):
    """R7：兜底通报需要知道"状态回写步骤"的结果，否则会把
    "帖子已发但状态没落盘"误报成"什么都没发生"（排障方向被带偏）"""

    def _text(self, name):
        with open(os.path.join(REPO_ROOT, ".github", "workflows", name), encoding="utf-8") as f:
            return f.read()

    def test_auto_post_state_step_has_id_and_is_reported(self):
        text = self._text("auto_post.yml")
        self.assertIn("id: state", text, "回写状态步骤需要 id 才能取 conclusion")
        self.assertIn("STATE_RESULT: ${{ steps.state.conclusion }}", text)

    def test_video_workflow_reports_publish_and_state(self):
        text = self._text("video_publish.yml")
        self.assertIn("id: state", text)
        self.assertIn("STATE_RESULT: ${{ steps.state.conclusion }}", text)
        self.assertIn("POSTER_RESULT: ${{ steps.publish.conclusion }}", text)


class TestVideoWorkflowContracts(unittest.TestCase):
    """R6：视频 workflow 的并发 / 权限 / 传参契约"""

    def _text(self):
        path = os.path.join(REPO_ROOT, ".github", "workflows", "video_publish.yml")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_concurrency_declared(self):
        self.assertIn("concurrency:", self._text(), "手动触发可被连点，必须串行化")

    def test_contents_write_for_state_commit(self):
        self.assertIn("contents: write", self._text(),
                      "需要回写 sent_cache.json，权限必须是 write")

    def test_no_eval_in_run_blocks(self):
        for line in self._text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn("eval ", stripped,
                             f"不得用 eval 执行拼接的命令: {stripped}")

    def test_dispatch_inputs_not_interpolated_into_shell(self):
        """dispatch 输入必须经 env 传入：直接插值进 shell 会被引号/$(...) 破坏"""
        run_blocks = re.findall(r"run: \|\n((?: {10,}.*\n)+)", self._text())
        self.assertTrue(run_blocks, "没找到多行 run 块，测试可能已失效")
        for block in run_blocks:
            self.assertNotIn("github.event.inputs", block,
                             f"dispatch 输入不得直接插值进 shell 脚本:\n{block}")

    def test_state_commit_step_present(self):
        text = self._text()
        self.assertIn("git_state_merge.py", text,
                      "视频帖记录必须提交回仓库，否则双发守卫恒不命中")
        self.assertIn("sent_cache.json", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
