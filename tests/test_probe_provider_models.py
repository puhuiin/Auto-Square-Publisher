# -*- coding: utf-8 -*-
"""scripts/probe_provider_models.py 的离线单测（R617）。

**全程零网络**：所有 _get_json 调用都被 patch 掉。这不只是"测试要快"——
MEMORY.md 约定「测试必须离线可跑」，且本脚本的真实版本会在 CI 里对 12 个站点
发GET。若测试里漏patch 一次，就会在单元测试阶段对真实站点发请求，
既慢又不稳定，还可能消耗站点配额。

覆盖重点：
  - extract_pool 的**抗漂移**能力（这是 L1 缺口的核心：探针覆盖面必须
    跟随 main.py 的 extra_keys，而不是自己维护一份会腐烂的列表）；
  - write_telemetry 的三态与「未知 ≠ 通过」方向；
  - 遥测落盘失败不得抛（旁路组件不得阻塞主流程，MEMORY.md 原则 5）。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location(
    "probe_provider_models",
    os.path.join(_ROOT, "scripts", "probe_provider_models.py"))
ppm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ppm)

# 一份最小 main.py 替身：结构与真实 extra_keys 一致（dict of
# 3-tuple，第 3 元是 `os.getenv(...) or "默认名"`）。
FAKE_MAIN = '''
def build():
    extra_keys = {
        "alpha": ("KEY", "https://alpha.example/v1",
                  os.getenv("ALPHA_MODEL", "").strip() or "alpha-default"),
        "beta": ("KEY", "https://beta.example/v1",
                 os.getenv("BETA_MODEL", "").strip() or "beta-default"),
    }
    return extra_keys
'''


class TestExtractPool(unittest.TestCase):
    """extract_pool：从 main.py 静态提取 provider 池（R617 L1 核心）"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.main_path = os.path.join(self.tmpdir, "main.py")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, src):
        with open(self.main_path, "w", encoding="utf-8") as f:
            f.write(src)
        return self.main_path

    def test_extracts_url_and_default_model(self):
        pool = ppm.extract_pool(self._write(FAKE_MAIN))
        self.assertEqual([p["site"] for p in pool], ["alpha", "beta"])
        self.assertEqual(pool[0]["base"], "https://alpha.example/v1")
        self.assertEqual(pool[0]["default"], "alpha-default")

    def test_new_site_auto_included_no_manual_list(self):
        """L1 缺口守卫：往 extra_keys 加一站，探针覆盖面自动+1。

        R615 的探针把站点硬编码在自己文件里，只列2 站，于是**历史 3 次僵尸名
        事件有 2 次发生在盲区**（tokenrouter / xkiro）。这条测试把"新增站点
        会被自动纳入"钉死，防止有人把站点列表搬回本文件。
        """
        src = FAKE_MAIN.replace(
            '"beta": ("KEY", "https://beta.example/v1",\n'
            '                 os.getenv("BETA_MODEL", "").strip() or "beta-default"),',
            '"beta": ("KEY", "https://beta.example/v1",\n'
            '                 os.getenv("BETA_MODEL", "").strip() or "beta-default"),\n'
            '        "gamma": ("KEY", "https://gamma.example/v1",\n'
            '                   os.getenv("GAMMA_MODEL", "").strip() or "gamma-default"),')
        self.assertIn("gamma", src, "夹具自检：注入失败则测试失去意义")
        pool = ppm.extract_pool(self._write(src))
        self.assertEqual([p["site"] for p in pool], ["alpha", "beta", "gamma"])

    def test_real_main_pool_has_eleven_sites(self):
        """对真实 main.py 的回归：池内站点数与关键站名。

        这是"覆盖面真的对齐了"的事实断言。若有人从 main.py 删站，这里会提醒
        同步更新期望值——**显式的失败好过静默的失明**。

        R626：12 → **11**，b.ai 因无免费额度被弃用（末次成功投递 09-21，
        已 11 天零产出）。这条断言正是它该响的地方——**删站时若不更新这里，
        就是"显式的失败"在替我报警**，而不是默默少覆盖一个站。
        """
        pool = ppm.extract_pool(os.path.join(_ROOT, "main.py"))
        sites = {p["site"] for p in pool}
        self.assertGreaterEqual(len(pool), 11,
                                f"实际解析到 {len(pool)} 站，期望 ≥11：{sorted(sites)}")
        for must in ("openrouter", "google", "xkiro", "tokenrouter",
                     "inferera", "stepfun"):
            self.assertIn(must, sites, f"{must} 站未纳入探针覆盖")
        # R626：b.ai 必须已退出池——若它回来，说明有人恢复了条目却没配套
        # 改workflow（反向不一致），或"偷偷恢复"绕过了弃用决策。
        self.assertNotIn("b.ai", sites, "b.ai 已弃用，不该在探针覆盖里")

    def test_syntax_error_fails_loudly(self):
        """main.py 语法错误必须抛异常，**不能静默返回空池**。

        静默空池会让探针报"共 0 站/ 全部通过"，把代码损坏伪装成健康——
        这正是最危险的失明形态。
        """
        with self.assertRaises(SyntaxError):
            ppm.extract_pool(self._write("def broken(:\n  pass"))

    def test_no_extra_keys_returns_empty(self):
        """没有 extra_keys 时返回空列表（由上层判 probe_ok=False）"""
        pool = ppm.extract_pool(self._write("x = 1\n"))
        self.assertEqual(pool, [])

    def test_unparseable_model_expr_yields_none_not_guess(self):
        """模型名表达式取不到静态常量时返回 None，**绝不猜**。

        猜名 = 制造僵尸名 = R263 的成因。None 会让上层标"需人工核对"（未知），
        而猜出来的名字会被当成真值去比对目录，得出假的"已死"。
        """
        src = '''
extra_keys = {
    "alpha": ("KEY", "https://alpha.example/v1", some_runtime_lookup()),
}
'''
        pool = ppm.extract_pool(self._write(src))
        self.assertEqual(len(pool), 1)
        self.assertIsNone(pool[0]["default"])
        self.assertEqual(pool[0]["base"], "https://alpha.example/v1")


class TestModelIds(unittest.TestCase):
    """_model_ids：兼容 OpenAI(data[]) 与 Google(models[]) 两种目录形状"""

    def test_openai_shape(self):
        ids = ppm._model_ids({"data": [{"id": "a:free"}, {"id": "b"}]})
        self.assertEqual(ids, {"a:free", "b"})

    def test_google_shape_strips_prefix(self):
        ids = ppm._model_ids(
            {"models": [{"name": "models/gemini-3-flash-preview"}]})
        self.assertEqual(ids, {"gemini-3-flash-preview"})

    def test_malformed_payload_does_not_crash(self):
        self.assertEqual(ppm._model_ids(None), set())
        self.assertEqual(ppm._model_ids({}), set())
        self.assertEqual(ppm._model_ids({"data": [None, 3, {"id": None}]}), set())


class TestCheckSite(unittest.TestCase):
    """check_site：ok 的三态（True 存活 / False 僵尸名 / None 未知）"""

    def setUp(self):
        self._old = dict(os.environ)
        for k in list(os.environ):
            if k.endswith("_API_KEY"):
                os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)

    def test_alive_default(self):
        with unittest.mock.patch.object(
                ppm, "_get_json",
                return_value={"data": [{"id": "keep-me"}, {"id": "x"}]}):
            r = ppm.check_site(
                {"site": "s", "base": "https://s.example/v1", "default": "keep-me"})
        self.assertIs(r["ok"], True)
        self.assertEqual(r["total"], 2)

    def test_zombie_default_detected(self):
        with unittest.mock.patch.object(
                ppm, "_get_json", return_value={"data": [{"id": "other"}]}):
            r = ppm.check_site(
                {"site": "s", "base": "https://s.example/v1", "default": "dead-name"})
        self.assertIs(r["ok"], False, "不在目录里= 僵尸名，必须报 False")

    def test_missing_key_is_unknown_not_pass(self):
        """无 key → ok=None（未知）。**绝不能是True**（R613 同一方向）。"""
        r = ppm.check_site(
            {"site": "zai", "base": "https://z.ai/v4", "default": "glm-4.7-flash"})
        self.assertIsNone(r["ok"])
        self.assertIn("ZAI_API_KEY", r["note"])

    def test_missing_key_note_blames_workflow_when_in_ci(self):
        """R619：缺 key 的提示必须区分「本机无凭据」与「workflow 漏注入」。

        CI 里若出现「未设 X」，那是配置缺口（该通道静默失效），必须一眼可辨；
        笼统说「跳过」会让人以为一切正常。
        """
        # 注意：必须用 AUTH_MODE 里真实存在的站，否则 check_site 会走
        # 「无认证直接请求」分支——那会真发网络请求，违反"测试离线可跑"
        # （MEMORY.md 硬约束），且本机到多数站不通 → URLError 而非预期断言。
        os.environ.pop("ZAI_API_KEY", None)
        r = ppm.check_site(
            {"site": "zai", "base": "https://z.ai/v4", "default": "glm-4.7-flash"})
        self.assertIn("workflow 漏注入", r["note"])

    def test_key_present_appends_query_and_checks(self):
        os.environ["ZAI_API_KEY"] = "secret"
        seen = {}

        def _fake(url, bearer=None):
            seen["url"] = url
            seen["bearer"] = bearer
            return {"data": [{"id": "glm-4.7-flash"}]}

        with unittest.mock.patch.object(ppm, "_get_json", _fake):
            r = ppm.check_site(
                {"site": "zai", "base": "https://z.ai/v4", "default": "glm-4.7-flash"})
        self.assertIs(r["ok"], True)
        self.assertIn("key=secret", seen["url"])
        self.assertIsNone(seen["bearer"], "query 模式不得同时发 Bearer")
        self.assertNotIn("secret", json.dumps(r), "key 不得回显到结论里")

    def test_bearer_mode_sends_authorization_header(self):
        """R619：bearer 模式的站必须走 Authorization 头，而不是 ?key=。

        R617 一律用 ?key= 拼，导致 tokenrouter / b.ai / stepfun 这些
        吃 Bearer 的站在 CI 里全部报401——「我猜错了认证方式」被误报成
        「key 坏了」，会把人引去重置一个其实没坏的 key。
        """
        os.environ["TOKENROUTER_API_KEY"] = "tr-secret"
        seen = {}

        def _fake(url, bearer=None):
            seen["url"] = seen["bearer"] = bearer
            return {"data": [{"id": "coding-kimi-k3-free"}]}

        with unittest.mock.patch.object(ppm, "_get_json", _fake):
            r = ppm.check_site(
                {"site": "tokenrouter", "base": "https://api.tokenrouter.io/v1",
                 "default": "coding-kimi-k3-free"})
        self.assertIs(r["ok"], True)
        self.assertEqual(seen["bearer"], "tr-secret")
        self.assertNotIn("key=", seen["url"], "bearer 模式不应把 key 拼进 query")

    def test_auth_fallback_recovers_on_alternative_mode(self):
        """R619 核心：一种认证方式被拒（401/403）时换另一种再试并判为成功。

        不做这个降级，就会把「认证方式猜错」与「key 失效」混为一谈——
        而这两者的处置动作完全不同（改代码 vs 重置凭据）。

        mock 前置须与 AUTH_MODE 一致：tokenrouter 配的是 **bearer**，
        所以"首次被拒"必须发生在 bearer 分支上（`calls[0][1]` 非 None），
        否则测的是另��条分支。
        """
        os.environ["TOKENROUTER_API_KEY"] = "tr-key"
        self.assertEqual(ppm.AUTH_MODE["tokenrouter"][1], "bearer",
                         "前提：tokenrouter 当前配为 bearer")
        calls = []

        def _fake(url, bearer=None):
            calls.append((url, bearer))
            if bearer is not None:                 # 首次 bearer 被拒
                raise urllib.error.HTTPError(url, 401, "unauthorized", None, None)
            return {"data": [{"id": "nemotron-3-nano-omni"}]}  # 换 query 成功

        with unittest.mock.patch.object(ppm, "_get_json", _fake):
            r = ppm.check_site(
                {"site": "tokenrouter", "base": "https://tr.example/v1",
                 "default": "nemotron-3-nano-omni"})
        self.assertIs(r["ok"], True, "换方式后成功就该判存活，不能报未核实")
        self.assertEqual(len(calls), 2, "应恰好试两次")
        self.assertIsNotNone(calls[0][1], "首次应走 AUTH_MODE 指定的方式")
        self.assertIsNone(calls[1][1], "降级后应改走 query")
        self.assertIn("key=", calls[1][0], "query 方式要把 key 拼进 url")
        self.assertIn("成功", r["note"])

    def test_auth_failure_both_modes_raises_and_yields_unknown(self):
        """两种方式都被拒 → 异常上抛，main()捕获成未核实（未知≠通过）。"""
        os.environ["TOKENROUTER_API_KEY"] = "tr-key"

        def _fake(url, bearer=None):
            raise urllib.error.HTTPError(url, 401, "unauthorized", None, None)

        entry = {"site": "tokenrouter", "base": "https://tr.example/v1",
                 "default": "nemotron-3-nano-omni"}
        with unittest.mock.patch.object(ppm, "_get_json", _fake):
            # main() 侧的 except 把它降级为 ok=None；此处只验它确实抛了
            with self.assertRaises(urllib.error.HTTPError):
                ppm.check_site(entry)

    def test_auth_fallback_not_attempted_on_non_auth_error(self):
        """非401/403（如 404 端点不存在）不做降级重试。

        404 是集成问题不是认证问题，换方式重试只会掩盖真因。
        """
        os.environ["ZAI_API_KEY"] = "z-key"

        def _fake(url, bearer=None):
            raise urllib.error.HTTPError(url, 404, "not found", None, None)

        with unittest.mock.patch.object(ppm, "_get_json", _fake):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                ppm.check_site({"site": "zai", "base": "https://z.ai/v4",
                                "default": "glm-4.7-flash"})
        self.assertEqual(cm.exception.code, 404)

    def test_unparseable_entry_is_unknown(self):
        r = ppm.check_site({"site": "x", "base": None, "default": None})
        self.assertIsNone(r["ok"])
        self.assertIn("人工核对", r["note"])

    def test_successful_check_note_is_not_warning_text(self):
        """R619：成功核对的 note 是正常信息，不得带⚠️ 语义词。

        main() 渲染时 note 走ℹ️ 行（此前是 ⚠️ 且会continue 跳过存活判定）。
        这里锁住 note 内容不出现"跳过/失败"这类会把成功说成失败的措辞。
        """
        os.environ["TOKENROUTER_API_KEY"] = "k"
        with unittest.mock.patch.object(
                ppm, "_get_json",
                return_value={"data": [{"id": "coding-kimi-k3-free"}]}):
            r = ppm.check_site({"site": "tokenrouter",
                                "base": "https://api.tokenrouter.io/v1",
                                "default": "coding-kimi-k3-free"})
        self.assertIs(r["ok"], True)
        self.assertIn("成功", r["note"])


class TestWriteTelemetry(unittest.TestCase):
    """write_telemetry：结论落盘（R617 L2 核心——结论必须有出口）"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "metrics.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_all_alive_writes_pass(self):
        ppm.write_telemetry(
            [{"site": "a", "ok": True}, {"site": "b", "ok": True}],
            self.path)
        rec = self._read()[0]
        self.assertEqual(rec["outcome"], "provider_probe")
        self.assertIs(rec["probe_ok"], True)
        self.assertEqual(rec["sites_ok"], 2)
        self.assertEqual(rec["sites_total"], 2)
        # 无未核实站时不得写 unknown_sites 空串（避免"空=有未知"的误读）
        self.assertNotIn("unknown_sites", rec)
        self.assertNotIn("probe_error", rec)

    def test_unknown_sites_recorded_not_swallowed(self):
        """9 站未核实 → 必须落unknown_sites + probe_error。

        这是 L1/L2 的交点：未核实站就是未来的僵尸名候选，**丢了就等于没查**。
        """
        res = [{"site": "a", "ok": True}] + \
              [{"site": f"u{i}", "ok": None} for i in range(9)]
        ppm.write_telemetry(res, self.path)
        rec = self._read()[0]
        self.assertEqual(rec["sites_unknown"], 9)
        self.assertIn("u0", rec["unknown_sites"])
        self.assertEqual(len(rec["unknown_sites"].split(",")), 9)
        # 机制正常（有明确 unknown 记录）→ probe_ok 仍为 True，
        # 但覆盖率不足由报表层用 sites_ok/sites_total 呈现。
        self.assertIs(rec["probe_ok"], True)

    def test_empty_pool_is_probe_failure(self):
        """池解析为空 → probe_ok=False + probe_error（结论不可信）"""
        ppm.write_telemetry([], self.path)
        rec = self._read()[0]
        self.assertIs(rec["probe_ok"], False)
        self.assertIn("池解析为空", rec["probe_error"])

    def test_write_failure_does_not_raise(self):
        """遥测落盘失败只warn 不抛——旁路组件不得阻塞主流程（原则 5）。

        若这里抛异常，workflow 里探针的非零退出会让`|| echo` 之外的路径
        语义变复杂，且探针本不该因为写不进遥测就放弃检查结论。
        """
        bad = os.path.join(self.tmpdir, "no_such_dir", "m.jsonl")
        ppm.write_telemetry([{"site": "a", "ok": True}], bad)  # 不抛即通过

    def test_zombie_recorded_independently_of_unknown(self):
        """R618 核心：僵尸名与未核实必须**两组独立字段**。

        生产实锤（2026-10-02 首轮 CI）：探针抓到 aihubmix 默认名
        coding-glm-5.3-flash-free 已从 417 模型目录消失，而同轮 8 站因缺
        key 未核实。首版只有 unknown_sites，读侧用单一 probe_ok 判据渲染
        "探针未完成"，把这个真僵尸名整条吞掉。**已确证的事实不该被
        "另一批未知"稀释。**
        """
        res = [{"site": "aihubmix", "ok": False},
               {"site": "openrouter", "ok": True}] + \
              [{"site": f"u{i}", "ok": None} for i in range(8)]
        ppm.write_telemetry(res, self.path)
        rec = self._read()[0]
        self.assertEqual(rec["zombie_count"], 1)
        self.assertEqual(rec["zombie_sites"], "aihubmix")
        # 两组字段并存互不覆盖
        self.assertEqual(len(rec["unknown_sites"].split(",")), 8)
        self.assertEqual(rec["sites_ok"], 1)
        # 有僵尸时 probe_ok 仍为 False（有站未判定完），但僵尸事实独立可读
        self.assertIs(rec["probe_ok"], False)

    def test_no_zombie_omits_zombie_fields(self):
        """无僵尸时不写 zombie_* 字段（缺失即 None，原则 4）。

        写空串/0 会让读侧无法区分"没有僵尸"与"字段没写"，进而可能渲染出
        "僵尸名 0 站"这种噪音行。
        """
        ppm.write_telemetry([{"site": "a", "ok": True}], self.path)
        rec = self._read()[0]
        self.assertNotIn("zombie_count", rec)
        self.assertNotIn("zombie_sites", rec)

    def test_appends_not_truncates(self):
        ppm.write_telemetry([{"site": "a", "ok": True}], self.path)
        ppm.write_telemetry([{"site": "a", "ok": True}], self.path)
        self.assertEqual(len(self._read()), 2, "必须追加，写坏历史遥测会毁掉全库")


class TestR626ModelsUrlOverride(unittest.TestCase):
    """R626：**目录端点不等于 base_url** —— OpenAI 兼容的 chat 端点没有 /models。

    实证（生产，非假设）：Google AI Studio 的 base_url 在 main.py 里是
        https://generativelanguage.googleapis.com/v1beta/openai
    它是 Gemini 的 **OpenAI 兼容层**（R615 选它正是为了零适配层），只提供
    chat/completions；模型目录在**原生**端点
        https://generativelanguage.googleapis.com/v1beta/models
    探针按 `{base}/models` 拼 → `.../v1beta/openai/models` → **HTTP 404**，
    于是 Google 从 10:02配key 起连续 18 轮探针都被标进 unknown_sites。

    **误报比不报更坏（R619 纪律）**：日志里"已检测到 GOOGLE_API_KEY"与
    "未核实：目录不可达 404"并排出现，读者只会去重置一个没坏的 key，
    而真问题（探针 URL 构造错了）在原地。R619 已把404 归类为"端点问题而非
    认证问题"，本条把它真正修掉。
    """

    def setUp(self):
        self._old = dict(os.environ)
        for k in list(os.environ):
            if k.endswith("_API_KEY"):
                os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)

    def test_google_uses_native_models_endpoint(self):
        """google 必须走原生 /v1beta/models，不能拼 openai 兼容层"""
        os.environ["GOOGLE_API_KEY"] = "fake-key-for-test"
        seen = {}

        def _fake(url, bearer=None):
            seen["url"] = url
            return {"models": [{"name": "models/gemini-3-flash-preview"}]}

        with unittest.mock.patch.object(ppm, "_get_json", side_effect=_fake):
            r = ppm.check_site({
                "site": "google",
                "base": "https://generativelanguage.googleapis.com/v1beta/openai",
                "default": "gemini-3-flash-preview"})
        self.assertNotIn("/openai/models", seen["url"],
                         "又把 OpenAI 兼容层当目录端点了——这正是 R626 的缺陷")
        self.assertIn("/v1beta/models", seen["url"])
        self.assertIs(r["ok"], True, "默认名在原生目录里，应判存活")

    def test_override_does_not_affect_other_sites(self):
        """override 只作用于显式列出的站，其余仍走 {base}/models 拼接。

        否则"给 Google 打补丁"会意外改变全部站点的行为——而那些站是好的。
        断言用 `startswith` 而非相等：query 认证站会在末尾拼 `?key=`（R619
        实测 zai 吃?key=），**那是认证行为不是目录端点行为**，本用例只关心
        目录路径没被 override 动过。
        """
        os.environ["ZAI_API_KEY"] = "fake-key-for-test"
        seen = {}
        with unittest.mock.patch.object(
                ppm, "_get_json",
                side_effect=lambda url, bearer=None: (
                    seen.__setitem__("url", url),
                    {"data": [{"id": "glm-4.7-flash"}]})[1]):
            ppm.check_site({"site": "zai", "base": "https://api.z.ai/v4",
                            "default": "glm-4.7-flash"})
        self.assertTrue(seen["url"].startswith("https://api.z.ai/v4/models"),
                        f"非 override 站的目录 URL 被改变了: {seen['url']}")

    def test_bai_removed_from_auth_mode(self):
        """R626：b.ai 已弃用，AUTH_MODE 里的悬空条目必须同步删除。

        覆盖面来自 extra_keys 的 AST 解析，不会再遍历到 AUTH_MODE——留着
        等于给未来的读者一个"这站还在链上"的错误信号。
        """
        self.assertNotIn("b.ai", ppm.AUTH_MODE,
                         "b.ai 已弃用，AUTH_MODE 里有悬空配置")

    def test_google_still_declares_query_auth(self):
        """override 只换 URL，**认证方式不变**（?key= 实测可用）。

        防止后续维护时把两者混起来改——R619 明确说过"认证方式不能猜"。
        """
        self.assertIn("google", ppm.AUTH_MODE)
        self.assertEqual(ppm.AUTH_MODE["google"][1], "query")


class TestR695AuthGapVsNoKey(unittest.TestCase):
    """R695：把「未登记 AUTH_MODE」拆成**处置完全相反**的两类。

    生产实锤：`no_auth_count=5` 已**恒定 3 天 242 次不变**。恒定不变的告警
    等于噪声——3 天没人动、没人查，因为看不出该做什么。

    而这 5 站里只有 **openrouter 是「有 secret + 主流程真实在用」**的
    （`gh secret list` 有 OPENROUTER_API_KEY，且 extra_keys 里是主力条目），
    其余 4 站（xkiro/aihubmix/inferera/bluesminds）**连 key 都没有**。
    混在一个字段里报出来 ⇒ openrouter 真出问题时，排障会先怀疑那4 个
    不相关的站 ⇒ 排障方向被稀释（R620"排序键恒等"同型：字段在，答不了问题）。

    拆分口径按「**本机有没有拿到 key**」而不是「在不在池里」：
      auth_gap_*：有 key + 未登记 ⇒ 探针配置缺条目（**代码问题**）
      nokey_*   ：无 key + 未登记 ⇒ 用户还没配（**用户侧待办**）
    """

    def _rec(self, results, env=None):
        tmp = tempfile.mkdtemp()
        old_env = {k: os.environ.get(k) for k in (env or {})}
        for k, v in (env or {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        old_file = ppm.METRICS_FILE
        ppm.METRICS_FILE = os.path.join(tmp, "m.jsonl")
        try:
            # ⚠️ 第二个位置参数是 metrics_file（路径），不是 elapsed
            # （我第一版把 1.0 传成了路径 ⇒ TypeError 被"落盘失败"吞掉 ⇒
            #   断言拿到的是"文件不存在"，报的是错误的原因）。
            ppm.write_telemetry(results, metrics_file=ppm.METRICS_FILE, elapsed_sec=1.0)
            with open(ppm.METRICS_FILE, encoding="utf-8") as f:
                return json.loads(f.read().strip())
        finally:
            ppm.METRICS_FILE = old_file
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_auth_gap_and_nokey_are_separated(self):
        """有 key 的未登记站必须进 auth_gap，无 key 的进 nokey，两者不可混。"""
        results = [
            {"site": "openrouter", "ok": True},    # 未登记，CI 里有 key
            {"site": "xkiro", "ok": True},         # 未登记，且无 key
        ]
        rec = self._rec(results, env={"OPENROUTER_API_KEY": "sk-x", "XKIRO_API_KEY": None})
        self.assertIn("openrouter", str(rec.get("auth_gap_sites", "")))
        self.assertNotIn("openrouter", str(rec.get("nokey_sites", "")))
        self.assertIn("xkiro", str(rec.get("nokey_sites", "")))
        self.assertNotIn("xkiro", str(rec.get("auth_gap_sites", "")))

    def test_legacy_no_auth_field_kept(self):
        """旧字段必须保留——报表/历史序列还在读它，删了会破已有数据。"""
        results = [{"site": "openrouter", "ok": True}]
        rec = self._rec(results, env={"OPENROUTER_API_KEY": None})
        self.assertIn("openrouter", str(rec.get("no_auth_sites", "")))
        self.assertEqual(rec.get("no_auth_count"), 1)

    def test_auth_probe_reason_recorded_when_no_key(self):
        """★ 无 key 时必须写清"为什么没实测"。

        否则 auth_probe_results 整条缺失 ⇒ 排障看到 openrouter 没结论，
        只能靠猜（R612：程序在用 ≠ 人在看；没原因的原因等于没原因）。
        """
        results = [{"site": "openrouter", "ok": True,
                    "auth_probe": "no_key:OPENROUTER_API_KEY"}]
        rec = self._rec(results)
        self.assertIn("openrouter=no_key:OPENROUTER_API_KEY",
                      str(rec.get("auth_probe_results", "")))

    def test_key_env_extracted_from_real_main_py(self):
        """★ SITE_KEY_ENV 必须从**真实 main.py** 的 AST 提取，不是手抄表。

        生产实锤（R693 同族）：手抄表会随 main.py 改 env 名而静默过期，
        而过期的名字让 `os.getenv` 恒返空 ⇒ **永远判成 nokey**，
        auth_gap 永远是 0——缺口被掩盖，正是本次要修的东西。
        """
        self.assertTrue(ppm.SITE_KEY_ENV, "SITE_KEY_ENV 为空")
        missing = [s for s, e in ppm.SITE_KEY_ENV.items() if not e]
        self.assertEqual(missing, [], f"这些站没取到 key env 名：{missing}")
        self.assertEqual(ppm.SITE_KEY_ENV.get("openrouter"), "OPENROUTER_API_KEY")

    def test_strip_call_is_unwrapped_correctly(self):
        """★ 锁定 `.strip()` 剥壳（我第一版写错过：判据多写了 `and key_node.args`）。

        真实形状 `os.getenv("X", "").strip()` 的**外层 .strip() 是零参数调用**，
        `args=[]`。若判据要求外层有 args，剥壳会被跳过 ⇒ key_env 恒 None ⇒
        11 站全被判成"取不到 env 名"。本测试用真实 main.py 兜住。
        """
        real = ppm.extract_pool(os.path.join(_ROOT, "main.py"))
        bad = [e["site"] for e in real if not e.get("key_env")]
        self.assertEqual(bad, [], f"真实 main.py 里这些站没提到 env 名：{bad}")


class TestR695ProbeAuthMode(unittest.TestCase):
    """R695：`_probe_auth_mode` 只能**回报事实**，不得替站点猜认证方式。"""

    def _run(self, behavior):
        def fake(url, key, mode):
            r = behavior.get(mode)
            if r == "ok":
                return {"data": [{"id": "m"}]}
            if r == "401":
                raise urllib.error.HTTPError(url, 401, "denied", {}, None)
            if r == "500":
                raise urllib.error.HTTPError(url, 500, "boom", {}, None)
            raise RuntimeError("network")
        old = ppm._try_auth
        ppm._try_auth = fake
        try:
            return ppm._probe_auth_mode("http://x", "k")
        finally:
            ppm._try_auth = old

    def test_four_verdicts(self):
        self.assertEqual(self._run({"bearer": "ok", "query": "401"}), "bearer_ok")
        self.assertEqual(self._run({"bearer": "401", "query": "ok"}), "query_ok")
        self.assertEqual(self._run({"bearer": "ok", "query": "ok"}), "both_ok_public")
        self.assertEqual(self._run({"bearer": "401", "query": "401"}),
                         "both_rejected:bearer,query")
        self.assertEqual(self._run({"bearer": "500", "query": "401"}),
                         "error:inconclusive")

    def test_public_directory_never_reported_as_a_mode(self):
        """★ 目录公开时**必须**说"无法判定"，不能报成某一种方式。

        这是 R619（认证方式不能猜）的核心守卫：若把 `both_ok_public`
        简化成 "bearer_ok"，下一个人就会把猜测填进 AUTH_MODE，
        而**错误的配置比留空更危险**（它会持续产出错误结论）。
        """
        out = self._run({"bearer": "ok", "query": "ok"})
        self.assertIn("public", out)
        self.assertNotIn("bearer_ok", out)
        self.assertNotIn("query_ok", out)

    def test_non_auth_http_error_is_not_a_verdict(self):
        """500 与 401 语义不同：前者不是"该方式不被接受"，不能计入判据。"""
        out = self._run({"bearer": "500", "query": "500"})
        self.assertEqual(out, "error:inconclusive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
