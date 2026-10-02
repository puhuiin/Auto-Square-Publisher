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

    def test_real_main_pool_has_twelve_sites(self):
        """对真实 main.py 的回归：池内站点数与关键站名。

        这是"覆盖面真的对齐了"的事实断言。若有人从 main.py 删站，这里会提醒
        同步更新期望值——**显式的失败好过静默的失明**。
        """
        pool = ppm.extract_pool(os.path.join(_ROOT, "main.py"))
        sites = {p["site"] for p in pool}
        self.assertGreaterEqual(len(pool), 12,
                                f"实际解析到 {len(pool)} 站，期望 ≥12：{sorted(sites)}")
        for must in ("openrouter", "google", "xkiro", "tokenrouter",
                     "inferera", "stepfun"):
            self.assertIn(must, sites, f"{must} 站未纳入探针覆盖")

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
        for k in list(ppm.QUERY_KEY_SITES.values()):
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

    def test_key_present_appends_query_and_checks(self):
        os.environ["ZAI_API_KEY"] = "secret"
        seen = {}

        def _fake(url):
            seen["url"] = url
            return {"data": [{"id": "glm-4.7-flash"}]}

        with unittest.mock.patch.object(ppm, "_get_json", _fake):
            r = ppm.check_site(
                {"site": "zai", "base": "https://z.ai/v4", "default": "glm-4.7-flash"})
        self.assertIs(r["ok"], True)
        self.assertIn("key=secret", seen["url"])
        self.assertNotIn("secret", json.dumps(r), "key 不得回显到结论里")

    def test_unparseable_entry_is_unknown(self):
        r = ppm.check_site({"site": "x", "base": None, "default": None})
        self.assertIsNone(r["ok"])
        self.assertIn("人工核对", r["note"])


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
