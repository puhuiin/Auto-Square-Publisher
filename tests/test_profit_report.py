# -*- coding: utf-8 -*-
import os
import unittest
from datetime import datetime, timezone


def _load():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "profit_report.py")
    spec = importlib.util.spec_from_file_location("profit_report_r808", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestProfitReport(unittest.TestCase):
    """R808：盈利分析脚本的核心口径（按发布天数归一、只认已发布 summarize 行）。"""

    def test_views_per_day_normal_and_min_age(self):
        pr = _load()
        self.assertEqual(pr.views_per_day(10, 2), 5.0)
        # 刚发布（age=0）不得除零/爆表：按 MIN_AGE_DAYS=0.25 计
        self.assertEqual(pr.views_per_day(10, 0), 40.0)

    def test_index_only_published_summarize(self):
        pr = _load()
        rows = [
            {"stage": "summarize", "outcome": "binance_published", "content_id": "1"},
            {"stage": "summarize", "outcome": "llm_rejected", "content_id": "2"},
            {"stage": "transport", "outcome": "binance_published", "content_id": "3"},
            {"stage": "summarize", "outcome": "binance_published"},  # 无 id
        ]
        self.assertEqual(set(pr.index_published(rows)), {"1"})

    def test_join_computes_age_and_skips_unmatched(self):
        pr = _load()
        export = datetime(2026, 10, 18, tzinfo=timezone.utc)
        stats = [
            {"content_id": "1", "ts": "2026-10-16T00:00:00Z", "views": 10},
            {"content_id": "x", "ts": "2026-10-16T00:00:00Z", "views": 99},
        ]
        idx = {"1": {"lang": "zh-CN"}}
        out = pr.join_stats(stats, idx, export)
        self.assertEqual(len(out), 1, "未关联的 stats 行必须丢弃")
        self.assertAlmostEqual(out[0]["age_days"], 2.0)
        self.assertAlmostEqual(out[0]["vpd"], 5.0)

    def test_token_category(self):
        pr = _load()
        self.assertEqual(pr.token_category("btc"), "BTC/ETH")
        self.assertEqual(pr.token_category("ETH"), "BTC/ETH")
        self.assertEqual(pr.token_category("sol"), "大市值")
        self.assertEqual(pr.token_category("XYZ"), "其他")
        self.assertEqual(pr.token_category(None), "其他")

    def test_summarize_filters_min_n_and_sorts_by_median(self):
        pr = _load()
        recs = ([{"k": "a", "vpd": 1.0}, {"k": "a", "vpd": 2.0}, {"k": "a", "vpd": 3.0}]
                + [{"k": "b", "vpd": 10.0}, {"k": "b", "vpd": 12.0}, {"k": "b", "vpd": 11.0}]
                + [{"k": "c", "vpd": 99.0}])  # n=1，应被 min_n 过滤
        rows = pr.summarize(recs, lambda r: r["k"], min_n=2)
        self.assertEqual([r[0] for r in rows], ["b", "a"])
        self.assertEqual(rows[0][1], 3)
        self.assertAlmostEqual(rows[0][2], 11.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
