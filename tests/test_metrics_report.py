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

import main as m  # noqa: E402  R106：模式同步守卫需要对照防线本体

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

    def test_bom_file_first_row_survives(self):
        import json
        with open(self.path, "w", encoding="utf-8-sig") as f:
            f.write(json.dumps({"ts": "2026-09-06T01:00:00+00:00",
                                "outcome": "binance_published",
                                "platforms": ["binance"]}) + "\n")
        rows, bad = mr.load_rows(self.path)
        self.assertEqual(len(rows), 1, "BOM 不得吃掉首行")
        self.assertEqual(bad, 0)

    def test_nan_inf_rejected_from_aggregates(self):
        import json
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"provider": "P", "tokens_used": float("nan"),
                                "llm_latency_sec": float("inf")}) + "\n")
            f.write(json.dumps({"provider": "P", "tokens_used": 800,
                                "llm_latency_sec": 10.0}) + "\n")
        rows, bad = mr.load_rows(self.path)
        self.assertEqual((len(rows), bad), (2, 0), "NaN 是合法 JSON 字面量，必须读进来再过滤")
        s = mr.summarize(rows)
        self.assertEqual(s["tokens_by_provider"]["P"]["total"], 800)
        self.assertEqual(s["latency_by_provider"]["P"], 10.0)

    def test_token_avg_is_int(self):
        import json
        with open(self.path, "w", encoding="utf-8") as f:
            for n in (800, 1000):
                f.write(json.dumps({"provider": "P", "tokens_used": n}) + "\n")
        rows, _ = mr.load_rows(self.path)
        avg = mr.summarize(rows)["tokens_by_provider"]["P"]["avg"]
        self.assertEqual(avg, 900)
        self.assertIsInstance(avg, int)

    def test_unreadable_path_exit_2_without_traceback(self):
        # 传目录/无权限路径：给人话 exit 2，而不是 traceback
        self.assertEqual(mr.main([self.tmpdir]), 2)

    def test_dry_rows_excluded_from_aggregates(self):
        _write(self.path, [
            {"ts": "2026-09-06T01:00:00+00:00", "provider": "P", "tokens_used": 9999,
             "llm_latency_sec": 99.0, "platforms": ["binance"], "tokens": ["BTC"],
             "outcome": "binance_published"},
            {"ts": "2026-09-06T02:00:00+00:00", "provider": "P", "tokens_used": 1,
             "llm_latency_sec": 0.1, "platforms": ["binance"], "tokens": ["ETH"],
             "outcome": "binance_published", "dry_run": True},
        ])
        rows, bad = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["dry_skipped"], 1)
        self.assertEqual(sum(s["by_provider"].values()), 1)
        self.assertEqual(s["tokens_by_provider"]["P"]["total"], 9999)
        self.assertNotIn("ETH", dict(s["by_token"]))
        self.assertIn("dry-run", mr.render_text(s))

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
        self.assertIn("funnel", doc, "JSON 模式必须带成功率漏斗")

    def test_days_filter_keeps_recent_only(self):
        """R109：--days N 时间窗——全量口径混入数天前的防御前历史会稀释近期信号"""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=5)).isoformat()
        new_ts = (now - timedelta(hours=2)).isoformat()
        _write(self.path, [
            {"ts": old_ts, "outcome": "binance_published", "platforms": ["binance"],
             "final_preview": "五天前的旧帖"},
            {"ts": new_ts, "outcome": "binance_published", "platforms": ["binance"],
             "final_preview": "最近的干净帖"},
            {"outcome": "binance_published", "platforms": ["binance"],
             "final_preview": "无 ts 行（夹具），近期视图下丢弃"},
        ])
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            self.assertEqual(mr.main([self.path, "--days", "2", "--json"]), 0)
        finally:
            sys.stdout = old
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["total"], 1, "只保留 2 天内的 1 行（旧帖与无 ts 行均丢弃）")
        # 质量扫描同样只扫近期行
        self.assertEqual(doc["quality_scan"]["scanned"], 1)

    def test_days_filter_unit_and_edge(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        _write(self.path, [
            {"ts": (now - timedelta(hours=1)).isoformat(), "outcome": "run_summary",
             "candidates": 5, "published": 1},
        ])
        rows, _ = mr.load_rows(self.path)
        # 正常窗口保留
        self.assertEqual(len(mr.filter_days(rows, "2")), 1)
        # 非法/非正数 → None（不过滤，保持全量）
        self.assertIsNone(mr.filter_days(rows, "abc"))
        self.assertIsNone(mr.filter_days(rows, "0"))
        self.assertIsNone(mr.filter_days(rows, None))

    def test_llm_failed_reason_feeds_error_panel(self):
        """R81：llm_failed 的 reason 是错误诊断唯一载体（404/超时全在里面），
        此前 errors 面板只认 error/error_code 字段恒空，报表失真"""
        _write(self.path, [
            {"ts": "2026-09-10T00:00:00Z", "provider": "Preset-b.ai",
             "reason": "Error code: 404 - model gone", "outcome": "llm_failed"},
            {"ts": "2026-09-10T00:01:00Z", "provider": "Preset-b.ai",
             "reason": "Request timed out.", "outcome": "llm_failed"},
            {"ts": "2026-09-10T00:02:00Z", "provider": "X",
             "reason": "空回", "stage": "transport", "outcome": "llm_rejected"},
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["errors"]["Error code: 404 - model gone"], 1)
        self.assertEqual(s["errors"]["Request timed out."], 1)
        # llm_rejected 的 reason 仍走 reject_reasons，不重复进 errors
        self.assertEqual(s["reject_reasons"]["空回"], 1)
        self.assertNotIn("空回", dict(s["errors"]))
        text = mr.render_text(s, rows)
        self.assertIn("404", text)

    def test_explicit_error_field_wins_over_reason(self):
        # error 字段存在时不被 reason 覆盖（显式优先）
        _write(self.path, [
            {"provider": "P", "error": "explicit-err", "reason": "reason-err",
             "outcome": "llm_failed"},
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["errors"]["explicit-err"], 1)
        self.assertNotIn("reason-err", dict(s["errors"]))

    def test_success_rate_funnel(self):
        """发布成功率漏斗：分母 = 投递成功 + llm_rejected/failed/success（去重）"""
        _write(self.path, [
            # 2 篇投递成功（platforms 非空）
            {"platforms": ["binance"], "outcome": "binance_published"},
            {"platforms": ["binance", "telegram"], "outcome": "binance_published",
             "dry_run": True},  # dry 行不算
            # 3 次 LLM 层失败/拒稿（各算一次尝试）
            {"outcome": "llm_rejected", "stage": "quality"},
            {"outcome": "llm_failed", "stage": "transport"},
            {"outcome": "llm_success"},  # 无投递行伴随时也计入尝试
            # 未知 outcome 不进分母
            {"outcome": "mystery_future"},
        ])
        rows, _ = mr.load_rows(self.path)
        f = mr.funnel(rows)
        self.assertEqual(f["delivered"], 1)
        self.assertEqual(f["attempted"], 4)
        self.assertEqual(f["rate"], 0.25)

    def test_funnel_excludes_campaign_intel_rows(self):
        """R100：情报刷新（stage=campaign_intel）不是发帖尝试——混入分母会把
        成功率系统性稀释（生产实测 131 分母混着 15 条情报行）"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published"},
            {"outcome": "llm_rejected", "stage": "quality"},
            # 情报刷新三态：成功/截断拒稿/空回拒稿——全部不得进分母
            {"outcome": "llm_success", "stage": "campaign_intel"},
            {"outcome": "llm_rejected", "stage": "campaign_intel",
             "reason": "输出中找不到 JSON 对象"},
            {"outcome": "llm_rejected", "stage": "campaign_intel",
             "reason": "模型返回空内容"},
        ])
        rows, _ = mr.load_rows(self.path)
        f = mr.funnel(rows)
        self.assertEqual(f["delivered"], 1)
        self.assertEqual(f["attempted"], 2, "情报行不得计入发帖尝试分母")
        self.assertEqual(f["rate"], 0.5)

    def test_delivered_rows_not_double_counted(self):
        # success 行若带投递字段（旧 schema 混写），按投递行计——delivered+1，
        # 不再进尝试分母（denominator 去重）；纯 llm_success 无投递字段才计尝试
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published"},
            {"outcome": "llm_success", "platforms": ["binance"]},  # 带投递字段的 success 行
        ])
        rows, _ = mr.load_rows(self.path)
        f = mr.funnel(rows)
        self.assertEqual(f["delivered"], 2, "两行都有投递字段，都按投递计")
        self.assertEqual(f["attempted"], 2, "带投递字段的行不得同时计入尝试（去重）")

    def test_image_tier_distribution(self):
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "image": True, "image_tier": "card"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "image": True, "image_tier": "raw"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "image": True},  # 旧 schema 无 tier：不进分布也不崩
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(dict(s["image_tiers"]), {"card": 1, "raw": 1})
        text = mr.render_text(s, rows)
        self.assertIn("配图层级", text)

    def test_dry_rows_excluded_from_funnel(self):
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published", "dry_run": True},
            {"outcome": "llm_rejected", "dry_run": True},
        ])
        rows, _ = mr.load_rows(self.path)
        f = mr.funnel(rows)
        self.assertEqual(f["attempted"], 0)
        self.assertIsNone(f["rate"])

    def test_success_rate_line_rendered(self):
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published"},
            {"outcome": "llm_rejected", "stage": "quality"},
        ])
        rows, _ = mr.load_rows(self.path)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("发布成功率: 1/2 篇", text)
        self.assertIn("50.0%", text)

    def test_run_summary_section_aggregates(self):
        """R92：run_summary 行的报表端消费——配额饱和/零候选/跳过分布不再需要
        手写临时脚本回答（R90/R91 分析实录）。R99：零候选与配额/时段外去混淆
        （"没去找"≠"没找到"），trending 快照入报表。"""
        _write(self.path, [
            {"ts": "2026-09-10T11:00:00Z", "outcome": "run_summary",
             "candidates": 44, "published": 0, "unprocessed": 0,
             "skipped_no_token": 40, "skipped_token_limit": 4,
             "trending": "NEAR UNI TAO"},
            {"ts": "2026-09-10T12:00:00Z", "outcome": "run_summary",
             "candidates": 43, "published": 2, "unprocessed": 41},
            {"ts": "2026-09-10T13:00:00Z", "outcome": "run_summary",
             "candidates": 0, "published": 0, "unprocessed": 0,
             "skipped_no_token": 0},  # 真零候选：抓了但没货
            {"ts": "2026-09-10T14:00:00Z", "outcome": "run_summary",
             "candidates": 0, "published": 0, "unprocessed": 0,
             "quota_blocked": True, "sent_24h": 12},
            {"ts": "2026-09-10T15:00:00Z", "outcome": "run_summary",
             "candidates": 0, "published": 0, "unprocessed": 0,
             "active_hours_blocked": True},
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        runs = s["runs"]
        self.assertEqual(runs["n"], 5)
        self.assertEqual(runs["quota_blocked"], 1)
        self.assertEqual(runs["active_hours_blocked"], 1)
        self.assertEqual(runs["zero_candidates"], 1,
                         "只有真去抓了没货的轮才算零候选；配额满/时段外不得混入")
        self.assertEqual(runs["candidates"], 87)
        self.assertEqual(runs["published"], 2)
        self.assertEqual(runs["unprocessed"], 41)
        self.assertEqual(runs["skips"]["no_token"], 40)
        self.assertEqual(runs["skips"]["token_limit"], 4)
        self.assertEqual(runs["last_trending"], "NEAR UNI TAO")
        text = mr.render_text(s, rows)
        self.assertIn("运行摘要（5 轮）", text)
        self.assertIn("配额饱和 1 轮", text)
        self.assertIn("零候选 1 轮", text)
        self.assertIn("累计候选 87 → 发布 2", text)
        self.assertIn("no_token", text)
        self.assertIn("最近热搜 [NEAR UNI TAO]", text)

    def test_run_summary_dry_rows_excluded(self):
        # dry 的 run_summary 行不得进运行聚合（与其它聚合同一隔离纪律）
        _write(self.path, [
            {"outcome": "run_summary", "candidates": 5, "published": 1, "dry_run": True},
            {"outcome": "run_summary", "candidates": 3, "published": 0},
        ])
        rows, _ = mr.load_rows(self.path)
        runs = mr.summarize(rows)["runs"]
        self.assertEqual(runs["n"], 1)
        self.assertEqual(runs["candidates"], 3)

    def test_quota_estimator_surfaced_in_report(self):
        """R113：配额释放估算的报表端消费——运营者看报告即知下一帖何时能发"""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        frees_iso = (now + timedelta(hours=3)).isoformat()
        _write(self.path, [
            {"outcome": "run_summary", "candidates": 0, "published": 0,
             "quota_blocked": True, "sent_24h": 12, "max_daily_posts": 12,
             "next_slot_frees": frees_iso, "next_slot_frees_min": 180},
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        runs = s["runs"]
        self.assertEqual(runs["quota_blocked"], 1)
        self.assertIn("next_slot_frees", runs)
        self.assertEqual(runs["next_slot_frees_min"], 180)
        text = mr.render_text(s, rows)
        self.assertIn("下一配额槽", text)
        self.assertIn("180 分钟", text)

    def test_quota_estimator_absent_when_no_blocked(self):
        # 无配额满行时不渲染释放估算
        _write(self.path, [
            {"outcome": "run_summary", "candidates": 10, "published": 2},
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        text = mr.render_text(s, rows)
        self.assertNotIn("下一配额槽", text)

    def test_trending_frequency_aggregated(self):
        """R117：热搜 token 频次——跨快照统计市场注意力的持续度"""
        _write(self.path, [
            {"outcome": "run_summary", "candidates": 40, "published": 1,
             "trending": "NEAR BTC TAO"},
            {"outcome": "run_summary", "candidates": 42, "published": 0,
             "trending": "NEAR BTC ETH"},
            {"outcome": "run_summary", "candidates": 41, "published": 1,
             "trending": "NEAR XRP"},
            {"outcome": "run_summary", "candidates": 0, "published": 0,
             "quota_blocked": True},  # 无 trending
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        runs = s["runs"]
        self.assertEqual(runs["trend_freq"]["NEAR"], 3,
                         "NEAR 在 3 个快照中出现")
        self.assertEqual(runs["trend_freq"]["BTC"], 2)
        self.assertEqual(runs["trend_freq"]["TAO"], 1)
        text = mr.render_text(s, rows)
        self.assertIn("热搜持续度", text)
        self.assertIn("NEAR×3", text)
        self.assertIn("BTC×2", text)

    def test_quality_scan_counts_violations(self):
        """R105：prompt 级禁令是软约束，合规度必须可度量——此前全靠人工读帖。
        巡检覆盖 final_preview 前 120 字（钩子区）。R101 前历史帖命中 FNG 属预期。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "全网贪婪指数都 69 了，还在喊多。"},  # FNG 锚定
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "先泼盆冷水，烧稳定币跟烧 XRP 是两码事。"},  # 永久禁用装置
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "这波行情值得拭目以待。"},  # AI 腔硬词
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "盘面放量突破，资金持续流入，结构健康。"},  # 干净
        ])
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(q["scanned"], 4)
        self.assertEqual(q["fng_anchor"], 1)
        self.assertEqual(q["banned_device"], 1)
        self.assertEqual(q["ai_flavor"], 1)
        self.assertIn("装置:先泼盆冷水", q["offenders"])
        self.assertIn("AI腔:拭目以待", q["offenders"])
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("内容合规巡检（最近 4 篇", text)
        self.assertIn("3 处命中", text)

    def test_quality_scan_clean_and_dry_excluded(self):
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "资金持续流入，主力建仓迹象明显。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "全网贪婪指数都 69 了", "dry_run": True},  # dry 不算
        ])
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(q["scanned"], 1)
        self.assertEqual(q["fng_anchor"], 0, "dry 行不得进合规扫描")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("全部通过", text)


class TestQualityPatternSync(unittest.TestCase):
    """R106：质量模式双份维护的同步守卫——main.py（防线本体）与 metrics_report
    （合规巡检）各有一份禁用装置/AI 腔/FNG 模式，静默漂移会让巡检度量失真
    （README 防漂移测试的同款思路：双份事实源必须有锁）。"""

    def test_overused_devices_in_sync(self):
        self.assertEqual(tuple(mr._OVERUSED_DEVICES),
                         tuple(m._OVERUSED_OPENING_DEVICES),
                         "永久禁用装置清单两份不一致——改 main 必须同步 metrics_report")

    def test_ai_flavor_hard_in_sync(self):
        self.assertEqual(tuple(mr._AI_FLAVOR_HARD),
                         tuple(m.MultiLLMEngine._AI_FLAVOR_HARD),
                         "AI 腔硬清单两份不一致——改 main 必须同步 metrics_report")

    def test_fng_anchor_pattern_in_sync(self):
        self.assertEqual(mr._FNG_ANCHOR_RE.pattern, m._FNG_ANCHOR_RE.pattern,
                         "FNG 锚定检测模式两份不一致——改 main 必须同步 metrics_report")


if __name__ == "__main__":
    unittest.main(verbosity=2)
