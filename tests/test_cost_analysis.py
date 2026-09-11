# -*- coding: utf-8 -*-
"""cost_analysis 成本面板口径测试（R120）。

旧实现按 stage∈(summarize, campaign_intel) 过滤 LLM 遥测，但投递成功行不带
stage、拒稿行 stage 是 quality/transport 等真实标签——面板只剩情报刷新调用，
生产 3 天窗口显示的"成功 4/拒单 4"全是情报操作，真实发帖成本（~11 万 token）
完全不可见。R120 改按 outcome 判定，本文件锁定新语义。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import cost_analysis as ca  # noqa: E402


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class TestPostingRowPredicate(unittest.TestCase):
    """_posting_row：发帖 LLM 行判定（成本面板准入）"""

    def test_published_row_without_stage_included(self):
        # 投递成功行不带 stage——旧 stage 过滤把它们全部漏掉（失真根源）
        self.assertTrue(ca._posting_row({"outcome": "binance_published",
                                         "provider": "Preset-b.ai"}))
        self.assertTrue(ca._posting_row({"outcome": "binance_published_cache_failed",
                                         "provider": "Preset-b.ai"}))

    def test_posting_rejections_included(self):
        self.assertTrue(ca._posting_row({"outcome": "llm_rejected",
                                         "stage": "quality"}))
        self.assertTrue(ca._posting_row({"outcome": "llm_rejected",
                                         "stage": "transport"}))
        self.assertTrue(ca._posting_row({"outcome": "llm_failed"}))

    def test_intel_and_run_summary_excluded(self):
        # 情报刷新是运营性调用，单独聚合（load_intel_records），不进发帖面板
        self.assertFalse(ca._posting_row({"outcome": "llm_success",
                                          "stage": "campaign_intel"}))
        self.assertFalse(ca._posting_row({"outcome": "llm_rejected",
                                          "stage": "campaign_intel"}))
        self.assertFalse(ca._posting_row({"outcome": "run_summary"}))
        self.assertFalse(ca._posting_row({"outcome": "mystery_future"}))


class TestAggregateSemantics(unittest.TestCase):
    """aggregate：投递成功行计入成功列 + 无提供商行排除"""

    def test_published_rows_count_as_success(self):
        agg = ca.aggregate([
            {"outcome": "binance_published", "provider": "Preset-b.ai",
             "tokens_used": 3000, "llm_latency_sec": 40.0},
            {"outcome": "llm_rejected", "provider": "Preset-b.ai",
             "stage": "transport", "reason": "429"},
        ], {})
        self.assertEqual(agg["Preset-b.ai"]["success"], 1)
        self.assertEqual(agg["Preset-b.ai"]["rejected"], 1)
        self.assertEqual(agg["Preset-b.ai"]["total_tokens"], 3000)

    def test_providerless_rows_excluded_from_table(self):
        # no_provider 快败（provider="-"）与外层 llm_failed（无 provider 字段）
        # 零 token 零延迟，进表只会产出 0 填充噪声行
        agg = ca.aggregate([
            {"outcome": "llm_rejected", "provider": "-", "stage": "no_provider"},
            {"outcome": "llm_failed", "reason": "质量门"},
            {"outcome": "binance_published", "provider": "Preset-openrouter",
             "tokens_used": 2000},
        ], {})
        self.assertEqual(set(agg), {"Preset-openrouter"})

    def test_llm_failed_with_provider_counted_rejected(self):
        # transport 失败行带 provider——真实失败的调用，计入拒单列
        agg = ca.aggregate([
            {"outcome": "llm_failed", "provider": "Preset-b.ai",
             "reason": "Request timed out.", "llm_latency_sec": 90.0},
        ], {})
        self.assertEqual(agg["Preset-b.ai"]["rejected"], 1)
        self.assertEqual(agg["Preset-b.ai"]["max_latency"], 90.0)


class TestIntelSeparation(unittest.TestCase):
    """情报刷新独立聚合：真实支出可见，但不混入发帖成本语义"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "metrics.jsonl")
        self.orig = ca.METRICS_FILE
        ca.METRICS_FILE = self.path

    def tearDown(self):
        ca.METRICS_FILE = self.orig
        self.tmp.cleanup()

    def test_load_intel_records_only_intel(self):
        _write(self.path, [
            {"outcome": "llm_success", "stage": "campaign_intel", "tokens_used": 2500},
            {"outcome": "llm_rejected", "stage": "campaign_intel"},
            {"outcome": "binance_published", "provider": "Preset-b.ai"},
            {"outcome": "llm_success", "stage": "summarize"},
        ])
        rows = ca.load_intel_records(None)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.get("stage") == "campaign_intel" for r in rows))

    def test_render_intel_spend_format(self):
        line = ca.render_intel_spend([
            {"tokens_used": 2500, "llm_latency_sec": 20.0},
            {"tokens_used": 976, "llm_latency_sec": 39.0},
        ])
        self.assertIn("情报刷新", line)
        self.assertIn("2 次", line)
        self.assertIn("3,476 token", line)
        self.assertIn("29.5s", line)

    def test_render_intel_spend_empty(self):
        self.assertEqual(ca.render_intel_spend([]), "")


if __name__ == "__main__":
    unittest.main()
