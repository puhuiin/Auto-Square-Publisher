# -*- coding: utf-8 -*-
"""
metrics_report.py 的离线单测（独立文件：不碰主套件，避免与他人在途改动交织）。
CI 里与 tests/test_core.py 一起跑（见 ci.yml）。
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "metrics_report",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "scripts", "metrics_report.py"))
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)


def _write(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for obj in lines:
            f.write(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))
            f.write("\n")


class TestMetricsReport(unittest.TestCase):
    """混合新老 schema 的遥测行必须全收且不崩"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "metrics.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _sample(self):
        return [
            # 老 schema 投递行（无 model/latency 字段）
            {"ts": "2026-09-06T01:00:00+00:00", "hour_bj": 9, "source": "U.Today",
             "tokens": ["BTC"], "provider": "B.ai", "platforms": ["binance"],
             "image": True, "outcome": "binance_published"},
            # 新 schema 投递行（含成本字段 + 失败诊断字段为空）
            {"ts": "2026-09-06T02:00:00+00:00", "hour_bj": 10, "source": "U.Today",
             "tokens": ["ETH"], "provider": "B.ai", "model": "glm-5",
             "tokens_used": 800, "llm_latency_sec": 12.5, "platforms": ["telegram"],
             "image": False, "outcome": "telegram_delivered"},
            # 拦截行
            {"ts": "2026-09-06T03:00:00+00:00", "hour_bj": 11, "provider": "X",
             "stage": "numbers", "reason": "编造 12.5%", "outcome": "llm_rejected"},
            # 未知 outcome、无 platforms：只能进分布，不得算投递
            {"ts": "2026-09-06T04:00:00+00:00", "hour_bj": 12, "outcome": "mystery_future"},
            "this is not json{{{",
            "[1, 2]",
        ]

    def test_mixed_schema_summary(self):
        _write(self.path, self._sample())
        rows, bad = mr.load_rows(self.path)
        self.assertEqual(len(rows), 4)
        self.assertEqual(bad, 2, "坏行与非对象行都要计数跳过")
        s = mr.summarize(rows)
        self.assertEqual(s["total"], 4)
        self.assertEqual(sum(s["by_provider"].values()), 2)
        self.assertEqual(s["by_hour"][9], 1)
        self.assertEqual(s["by_hour"][10], 1)
        self.assertEqual(s["by_source"]["U.Today"], 2)
        self.assertEqual(s["by_token"]["BTC"], 1)
        self.assertEqual(s["images"], 1)
        self.assertEqual(s["reject_by_stage"]["numbers"], 1)
        self.assertIn("编造 12.5%", dict(s["reject_reasons"]))
        self.assertEqual(s["latency_by_provider"], {"B.ai": 12.5})
        self.assertEqual(s["tokens_by_provider"]["B.ai"]["total"], 800)
        self.assertEqual(s["by_outcome"]["mystery_future"], 1)

    def test_empty_file_renders(self):
        _write(self.path, [])
        rows, bad = mr.load_rows(self.path)
        self.assertEqual((rows, bad), ([], 0))
        out = mr.render_text(mr.summarize(rows))
        self.assertIn("样本: 0 行", out)

    def test_missing_file_exit_2(self):
        self.assertEqual(mr.main([os.path.join(self.tmpdir, "nope.jsonl")]), 2)

    def test_json_mode_is_parseable(self):
        _write(self.path, self._sample())
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            self.assertEqual(mr.main([self.path, "--json"]), 0)
        finally:
            sys.stdout = old
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["total"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
