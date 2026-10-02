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

    def test_mirror_delivery_outcomes_included(self):
        """R211：副平台-only 投递回执 outcome=*_delivered，旧过滤只认
        binance_published* → okx/telegram 发帖 LLM 成本在面板不可见。"""
        self.assertTrue(ca._posting_row({"outcome": "okx_draft_delivered",
                                         "provider": "Preset-b.ai",
                                         "stage": "summarize"}))
        self.assertTrue(ca._posting_row({"outcome": "okx_draft+telegram_delivered",
                                         "provider": "Preset-b.ai",
                                         "stage": "summarize"}))
        self.assertTrue(ca._posting_row({"outcome": "telegram_delivered_cache_failed",
                                         "provider": "Preset-b.ai",
                                         "stage": "summarize"}))
        self.assertTrue(ca.is_delivery_outcome("binance_published"))
        self.assertTrue(ca.is_delivery_outcome("okx_draft_delivered"))
        self.assertFalse(ca.is_delivery_outcome("already_delivered"))
        self.assertFalse(ca.is_delivery_outcome("run_summary"))

    def test_mirror_delivered_counts_as_success(self):
        agg = ca.aggregate([
            {"outcome": "okx_draft_delivered", "provider": "Preset-b.ai",
             "stage": "summarize", "tokens_used": 4000, "llm_latency_sec": 35.0},
        ], {})
        self.assertEqual(agg["Preset-b.ai"]["success"], 1)
        self.assertEqual(agg["Preset-b.ai"]["total_tokens"], 4000)


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


class TestDryIsolation(unittest.TestCase):
    """R167：DRY 隔离必须同时认 dry_run（R81 写侧）与 legacy dry（R66）。

    append_metrics 自 R81 起统一写 dry_run=True；cost_analysis 三处 loader
    仍只读 rec.get(\"dry\")——CI 手动 dry_run 产出的沙盒行会原样灌进成本面板。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "metrics.jsonl")
        self.orig = ca.METRICS_FILE
        ca.METRICS_FILE = self.path

    def tearDown(self):
        ca.METRICS_FILE = self.orig
        self.tmp.cleanup()

    def test_dry_run_field_excluded_by_default(self):
        _write(self.path, [
            {"outcome": "binance_published", "provider": "Sandbox",
             "tokens_used": 9999, "llm_latency_sec": 99.0, "dry_run": True},
            {"outcome": "binance_published", "provider": "Prod",
             "tokens_used": 100, "llm_latency_sec": 10.0},
            {"outcome": "llm_rejected", "provider": "Sandbox", "stage": "quality",
             "dry_run": True},
            {"outcome": "llm_success", "stage": "campaign_intel",
             "provider": "Sandbox", "tokens_used": 50, "dry_run": True},
        ])
        self.assertEqual([r["provider"] for r in ca.load_records(None)], ["Prod"])
        self.assertEqual(ca.load_intel_records(None), [])
        self.assertEqual([r["provider"] for r in ca.load_published(None)], ["Prod"])

    def test_legacy_dry_field_still_excluded(self):
        _write(self.path, [
            {"outcome": "binance_published", "provider": "OldDry",
             "tokens_used": 1, "dry": True},
            {"outcome": "binance_published", "provider": "Prod", "tokens_used": 2},
        ])
        self.assertEqual([r["provider"] for r in ca.load_records(None)], ["Prod"])

    def test_include_dry_brings_them_back(self):
        _write(self.path, [
            {"outcome": "binance_published", "provider": "Sandbox",
             "tokens_used": 9, "dry_run": True},
        ])
        self.assertEqual(len(ca.load_records(None, include_dry=True)), 1)
        self.assertEqual(len(ca.load_published(None, include_dry=True)), 1)

class TestR631FailedNotCountedAsTransport(unittest.TestCase):
    """R631：**llm_failed 不得默认归入 transport 桶**。

    旧实现 `r.get("stage") or "transport"` 把两种**完全不同**的事件合成一桶：
      transport = 调用成功但返回错误/超时（可降性：换通道/调预算）
      llm_failed = 调用本身未成功（通道/网络/账户问题）
    生产实录：真实 transport 67 + llm_failed 35 = 旧表显示的"transport ×102"，
    **让它看起来是最大拒因**，而真实含义是"102 次调用没拿到可用内容"。
    两者的处置动作不同，混桶后无法引导决策。

    纪律同源：默认桶标签是**猜测**。R621「并列主因不猜」、R628「分母必须显式」
    都是同一类问题——**不该用一个看起来合理的默认值掩盖"这里本来没数据"**。
    """

    def _rows(self):
        return [
            {"ts": "2026-10-02T12:00:00+00:00", "outcome": "llm_rejected",
             "stage": "transport", "provider": "Preset-a", "model": "m",
             "tokens_used": 100, "llm_latency_sec": 10},
            {"ts": "2026-10-02T12:00:00+00:00", "outcome": "llm_failed",
             "provider": "Preset-a", "model": "m",
             "reason": "Request timed out.", "tokens_used": 50,
             "llm_latency_sec": 25},
            {"ts": "2026-10-02T12:00:00+00:00", "outcome": "llm_rejected",
             "stage": "quality", "provider": "Preset-a", "model": "m",
             "tokens_used": 200, "llm_latency_sec": 15},
        ]

    def _render(self):
        rows = self._rows()
        # 真实 API 是 render_publish_funnel(published, rejected)——
        # 拒稿漏斗在 published 为空时也要渲染（先断言这一点，别让夹具掩盖）。
        return ca.render_publish_funnel([], [r for r in rows
                                             if r.get("outcome") in
                                             ("llm_rejected", "llm_failed")])

    def test_llm_failed_not_merged_into_transport(self):
        """两个桶必须分开，且调用失败单列"""
        text = self._render()
        line = next(ln for ln in text.splitlines() if "拒稿漏斗" in ln)
        self.assertIn("调用失败", line,
                      "llm_failed 仍被并入某个 stage 桶，未单独显形")
        # transport 应只计真实的 1 条，而不是 2 条
        self.assertIn("transport ×1", line,
                      f"transport 桶被 llm_failed 污染: {line}")

    def test_explains_the_distinction(self):
        """必须说明「无 stage = 调用本身失败」，否则读者仍会把两者当同一类"""
        text = self._render()
        line = next(ln for ln in text.splitlines() if "拒稿漏斗" in ln)
        self.assertIn("与 transport 拒稿不同", line)

    def test_funnel_survives_zero_delivery(self):
        """**零投递窗口下拒稿漏斗仍须渲染**（R631 第二处修复）。

        旧实现 `if not published: return ...` 会让整段漏斗跟着消失，而
        「全部候选被拒、零投递」恰恰是**最需要看拒稿原因**的窗口——
        那是唯一可行动的信息。这是「沉默不是通过」（R617）的形态：
        不是漏报，是整块覆盖不到。
        """
        rows = [r for r in self._rows()
                if r.get("outcome") in ("llm_rejected", "llm_failed")]
        text = ca.render_publish_funnel([], rows)
        self.assertIn("拒稿漏斗", text,
                      "零投递时拒稿漏斗整段消失——恰是最需要它的窗口")
        self.assertIn("零投递", text, "应显式说明形态/配图不可算的原因")

    def test_truly_empty_renders_placeholder(self):
        """零投递且零拒稿才走占位分支（静默仍有兜底、不抛异常）"""
        text = ca.render_publish_funnel([], [])
        self.assertIn("无投递记录", text)

    def test_source_has_no_default_stage_coercion(self):
        """守卫源码：`or "transport"` 这种默认归并不得再出现。

        源码级断言而非输出级：默认值可能换别的标签（如 "unknown"），
        禁掉的是**「缺标签就猜一个」这个模式**本身。
        """
        src = open(ca.__file__, encoding="utf-8").read()
        self.assertNotIn('or "transport"', src,
                         "又出现默认归并——缺 stage 时不该猜标签")


if __name__ == "__main__":
    unittest.main()
