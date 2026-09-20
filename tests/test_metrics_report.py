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

    def test_reject_previews_collected_and_capped(self):
        """R163：质量拒稿的 content_preview/finish_reason 进报表，窗口只留最近 5 条"""
        rows = [
            {"ts": f"2026-09-14T01:0{i}:00+00:00", "outcome": "llm_rejected",
             "stage": "quality", "provider": "Preset-openrouter",
             "reason": f"内容过短 ({i + 10} 字符)",
             "content_preview": f"stub-{i} " + "x" * 100,
             "finish_reason": "stop"}
            for i in range(7)
        ]
        # 无快照的旧拒稿行不得撑爆列表
        rows.append({"ts": "2026-09-14T02:00:00+00:00", "outcome": "llm_rejected",
                     "stage": "quality", "provider": "Preset-b.ai",
                     "reason": "无快照旧行"})
        s = mr.summarize(rows)
        self.assertEqual(len(s["reject_previews"]), 5)
        self.assertTrue(s["reject_previews"][-1]["preview"].startswith("stub-6"))
        self.assertEqual(len(s["reject_previews"][-1]["preview"]), 80)
        self.assertEqual(s["reject_previews"][-1]["finish_reason"], "stop")
        out = mr.render_text(s)
        self.assertIn("拒稿快照", out)
        self.assertIn("finish=stop", out)

    def test_intel_degraded_counted_and_rendered(self):
        """R173：R171 写侧 intel_degraded 必须进报表——过期情报注入可聚合"""
        rows = [
            {"ts": "2026-09-14T13:00:00+00:00", "outcome": "binance_published",
             "provider": "Preset-b.ai", "tokens": ["ADA"], "intel_degraded": True,
             "platforms": ["binance"], "widget_count": 2, "tag_count": 3},
            {"ts": "2026-09-14T13:10:00+00:00", "outcome": "binance_published",
             "provider": "Preset-b.ai", "tokens": ["BTC"], "intel_degraded": False,
             "platforms": ["binance"], "widget_count": 2, "tag_count": 3},
            # 历史行无字段：不进分母
            {"ts": "2026-09-14T10:00:00+00:00", "outcome": "binance_published",
             "provider": "Preset-openrouter", "tokens": ["XRP"],
             "platforms": ["binance"], "widget_count": 2, "tag_count": 3},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["intel_degraded_posts"], 1)
        self.assertEqual(s["intel_fresh_posts"], 1)
        out = mr.render_text(s)
        self.assertIn("情报注入", out)
        self.assertIn("降级(过期) 1", out)
        self.assertIn("新鲜 1", out)

    def test_intel_age_hours_aggregated(self):
        """R181：情报陈旧小时数进报表——bool 之外还要能量化多旧"""
        rows = [
            {"ts": "2026-09-14T13:00:00+00:00", "outcome": "binance_published",
             "provider": "Preset-b.ai", "tokens": ["ADA"], "intel_degraded": True,
             "intel_age_hours": 14.0, "platforms": ["binance"],
             "widget_count": 2, "tag_count": 3},
            {"ts": "2026-09-14T14:40:00+00:00", "outcome": "binance_published",
             "provider": "Preset-b.ai", "tokens": ["LINK"], "intel_degraded": True,
             "intel_age_hours": 16.0, "platforms": ["binance"],
             "widget_count": 2, "tag_count": 3},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["intel_age_hours"], [14.0, 16.0])
        out = mr.render_text(s)
        self.assertIn("陈旧均值 15.0h", out)
        self.assertIn("最长 16.0h", out)

    def test_intel_cooldown_skip_counted(self):
        """R175：退避跳过必须可见——区分「配额早退没刷」vs「想刷被 2h 冷却挡」"""
        rows = [
            {"ts": "2026-09-14T13:20:00+00:00", "outcome": "intel_cooldown_skip",
             "stage": "campaign_intel", "reason": "backoff_until=2026-09-14T15:02:06"},
            {"ts": "2026-09-14T13:40:00+00:00", "outcome": "intel_cooldown_skip",
             "stage": "campaign_intel", "reason": "backoff_until=2026-09-14T15:02:06"},
            {"ts": "2026-09-14T13:00:00+00:00", "outcome": "llm_rejected",
             "stage": "quality", "provider": "Preset-openrouter", "reason": "x"},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["intel_cooldown_skips"], 2)
        # 不得污染拒稿面板
        self.assertEqual(sum(s["reject_by_stage"].values()), 1)
        out = mr.render_text(s)
        self.assertIn("失败退避跳过 2 轮", out)

    def test_run_summary_stage_timings_aggregated(self):
        """R177：sleep/llm 分段进报表——370s 总耗时的大头是拟人 pacing"""
        rows = [
            {"ts": "2026-09-14T13:09:00+00:00", "outcome": "run_summary",
             "candidates": 40, "published": 1, "unprocessed": 39,
             "run_elapsed_sec": 370.0, "sleep_elapsed_sec": 240.0,
             "llm_elapsed_sec": 38.0},
            {"ts": "2026-09-14T13:29:00+00:00", "outcome": "run_summary",
             "candidates": 40, "published": 1, "unprocessed": 39,
             "run_elapsed_sec": 360.0, "sleep_elapsed_sec": 120.0,
             "llm_elapsed_sec": 30.0},
            # 历史行无分段字段：不进分段聚合
            {"ts": "2026-09-14T12:00:00+00:00", "outcome": "run_summary",
             "candidates": 0, "published": 0, "quota_blocked": True,
             "run_elapsed_sec": 0.0},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["avg_sleep_sec"], 180.0)
        self.assertEqual(s["runs"]["max_sleep_sec"], 240.0)
        self.assertEqual(s["runs"]["avg_llm_sec"], 34.0)
        out = mr.render_text(s)
        self.assertIn("耗时构成", out)
        self.assertIn("拟人间隔", out)

    def test_quota_wait_stage_aggregated(self):
        """R184：配额边界追赶等待进分段——生产 18:06/18:29 轮 run_elapsed
        197.5s/361.0s 里有 150.3s/270.3s 无法归因（R177 只覆盖 LLM/配图/发布），
        实为 R154 的等待 sleep；单列后总账可对平，零值行不进均值。"""
        rows = [
            {"ts": "2026-09-14T18:06:00+00:00", "outcome": "run_summary",
             "candidates": 43, "published": 1, "unprocessed": 42,
             "run_elapsed_sec": 197.5, "llm_elapsed_sec": 42.4,
             "quota_wait_elapsed_sec": 150.0},
            {"ts": "2026-09-14T18:29:00+00:00", "outcome": "run_summary",
             "candidates": 43, "published": 1, "unprocessed": 42,
             "run_elapsed_sec": 361.0, "llm_elapsed_sec": 86.3,
             "quota_wait_elapsed_sec": 270.0},
            # 未追赶的轮次带零值：不进均值（否则把均值稀释）
            {"ts": "2026-09-14T20:44:00+00:00", "outcome": "run_summary",
             "candidates": 45, "published": 1, "unprocessed": 44,
             "run_elapsed_sec": 55.9, "llm_elapsed_sec": 51.1,
             "quota_wait_elapsed_sec": 0.0},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["avg_quota_wait_sec"], 210.0)
        out = mr.render_text(s)
        self.assertIn("配额追赶等待 210.0s", out)

    def test_stage_pipeline_segments_aggregated(self):
        """R280：抓取/配图/发布分段进报表——R177 起随 run_summary 落盘但读侧
        从未消费（全史三分段零读取面），总耗时逼近回调节奏时"哪一段在吃钟"
        没有出口。抓取段对 R9 deadline（300s）负责；配图段含转码+S3 上传
        （R110；发布段是币安侧延迟代理。零值行不进均值。"""
        rows = [
            {"ts": "2026-09-14T13:09:00+00:00", "outcome": "run_summary",
             "candidates": 40, "published": 1, "unprocessed": 39,
             "run_elapsed_sec": 370.0, "fetch_elapsed_sec": 47.5,
             "image_elapsed_sec": 32.0, "publish_elapsed_sec": 4.2},
            {"ts": "2026-09-14T13:29:00+00:00", "outcome": "run_summary",
             "candidates": 40, "published": 1, "unprocessed": 39,
             "run_elapsed_sec": 360.0, "fetch_elapsed_sec": 88.5,
             "image_elapsed_sec": 28.0, "publish_elapsed_sec": 6.8},
            # 抓取段为零的轮次不进均值（否则稀释）
            {"ts": "2026-09-14T20:44:00+00:00", "outcome": "run_summary",
             "candidates": 45, "published": 1, "unprocessed": 44,
             "run_elapsed_sec": 55.9, "fetch_elapsed_sec": 0.0,
             "image_elapsed_sec": 0.0, "publish_elapsed_sec": 0.0},
            # 历史行无这三个字段：不进分段聚合
            {"ts": "2026-09-14T12:00:00+00:00", "outcome": "run_summary",
             "candidates": 0, "published": 0, "quota_blocked": True,
             "run_elapsed_sec": 0.0},
        ]
        s = mr.summarize(rows)
        runs = s["runs"]
        self.assertEqual(runs["avg_fetch_sec"], 68.0)
        self.assertEqual(runs["max_fetch_sec"], 88.5)
        self.assertEqual(runs["n_fetch_sec"], 2)
        self.assertEqual(runs["avg_image_sec"], 30.0)
        self.assertEqual(runs["max_image_sec"], 32.0)
        self.assertEqual(runs["n_image_sec"], 2)
        self.assertEqual(runs["avg_publish_sec"], 5.5)
        self.assertEqual(runs["max_publish_sec"], 6.8)
        self.assertEqual(runs["n_publish_sec"], 2)
        out = mr.render_text(s)
        self.assertIn("耗时构成", out)
        self.assertIn("抓取 68.0s", out)
        self.assertIn("配图 30.0s", out)
        self.assertIn("发布 5.5s", out)
        # 样本少于总轮数必须标出——否则读成覆盖全部轮次
        self.assertIn("(样本 2 轮)", out)

    def test_pipeline_segments_not_in_runs_dict(self):
        """R280：三段原始序列不得进 s["runs"] 明细（易失 JSON 混进 run 摘要）"""
        rows = [
            {"ts": "2026-09-14T13:09:00+00:00", "outcome": "run_summary",
             "candidates": 40, "published": 1, "run_elapsed_sec": 370.0,
             "fetch_elapsed_sec": 47.5, "image_elapsed_sec": 32.0,
             "publish_elapsed_sec": 4.2},
        ]
        s = mr.summarize(rows)
        for k in ("fetch_elapsed", "image_elapsed", "publish_elapsed"):
            self.assertNotIn(k, s["runs"])

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

    def test_funnel_dedupes_failover_story(self):
        """R119：故事口径——同一故事 failover 落多行（b.ai 429 拒 + openrouter
        发布）只算一次尝试。生产实证：行口径 30.5% vs 故事口径 66.7%。"""
        _write(self.path, [
            {"ts": "2026-09-10T13:03:00", "title": "Vitalik pushes plan",
             "outcome": "llm_rejected", "stage": "transport", "reason": "429"},
            {"ts": "2026-09-10T13:05:00", "title": "Vitalik pushes plan",
             "platforms": ["binance"], "outcome": "binance_published"},
        ])
        rows, _ = mr.load_rows(self.path)
        f = mr.funnel(rows)
        self.assertEqual(f["attempted"], 1, "同日同题两行 = 一个故事一次尝试")
        self.assertEqual(f["delivered"], 1)
        self.assertEqual(f["rate"], 1.0)
        self.assertEqual(f["failover_rescued"], 1, "先拒后发 = failover 救回")

    def test_funnel_rejected_then_failed_same_story(self):
        """R119：拒稿行 + 外层 failed 行同题同日也去重（12:48Z 实录双记）。"""
        _write(self.path, [
            {"ts": "2026-09-10T12:48:33", "title": "Story A",
             "outcome": "llm_rejected", "stage": "quality", "reason": "内容过短"},
            {"ts": "2026-09-10T12:48:33", "title": "Story A",
             "outcome": "llm_failed", "reason": "质量门: 内容过短"},
        ])
        rows, _ = mr.load_rows(self.path)
        f = mr.funnel(rows)
        self.assertEqual(f["attempted"], 1)
        self.assertEqual(f["delivered"], 0)
        self.assertEqual(f["rate"], 0.0)
        self.assertEqual(f["failover_rescued"], 0)

    def test_funnel_same_title_different_dates(self):
        """R119：跨日同题是不同故事（旧闻隔天重试/同名事件续报）。"""
        _write(self.path, [
            {"ts": "2026-09-10T13:05:00", "title": "Same title",
             "platforms": ["binance"], "outcome": "binance_published"},
            {"ts": "2026-09-11T09:10:00", "title": "Same title",
             "outcome": "llm_failed", "reason": "超时"},
        ])
        rows, _ = mr.load_rows(self.path)
        f = mr.funnel(rows)
        self.assertEqual(f["attempted"], 2)
        self.assertEqual(f["delivered"], 1)
        self.assertEqual(f["rate"], 0.5)

    def test_funnel_failover_rescued_multi_story(self):
        """R119：failover_rescued 跨故事聚合。"""
        _write(self.path, [
            {"ts": "2026-09-10T12:43:00", "title": "Story X",
             "outcome": "llm_rejected", "stage": "transport", "reason": "429"},
            {"ts": "2026-09-10T12:46:00", "title": "Story X",
             "platforms": ["binance"], "outcome": "binance_published"},
            {"ts": "2026-09-10T13:03:00", "title": "Story Y",
             "outcome": "llm_rejected", "stage": "transport", "reason": "429"},
            {"ts": "2026-09-10T13:05:00", "title": "Story Y",
             "platforms": ["binance"], "outcome": "binance_published"},
            {"ts": "2026-09-10T14:00:00", "title": "Story Z",
             "platforms": ["binance"], "outcome": "binance_published"},
        ])
        rows, _ = mr.load_rows(self.path)
        f = mr.funnel(rows)
        self.assertEqual(f["attempted"], 3)
        self.assertEqual(f["delivered"], 3)
        self.assertEqual(f["failover_rescued"], 2)

    def test_funnel_renders_story_semantics(self):
        """R119：报表行注明故事口径与 failover 救回数。"""
        _write(self.path, [
            {"ts": "2026-09-10T12:43:00", "title": "Story X",
             "outcome": "llm_rejected", "stage": "transport", "reason": "429"},
            {"ts": "2026-09-10T12:46:00", "title": "Story X",
             "platforms": ["binance"], "outcome": "binance_published"},
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        text = mr.render_text(s, rows)
        self.assertIn("故事口径", text)
        self.assertIn("failover 救回 1 篇", text)

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

    def test_hot_topics_surfaced_in_report(self):
        """R190：hot_topics 已入生产 run_summary（13:04/13:19/13:35Z），
        报表端必须消费——R92 同轮纪律。无字段的历史行不进 hits 分母。"""
        _write(self.path, [
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "hot_topics": "Java 27 Released | An e-ink frame that hears birds"},
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "hot_topics": "OpenAI buys smartphone camera maker Glass Imaging"},
            {"outcome": "run_summary", "candidates": 10, "published": 1},
        ])
        rows, _ = mr.load_rows(self.path)
        runs = mr.summarize(rows)["runs"]
        self.assertEqual(runs["hot_topic_hits"], 2)
        self.assertIn("Glass Imaging", runs["last_hot_topics"])
        text = mr.render_text(s := mr.summarize(rows), rows)
        self.assertIn("热点钩子", text)
        self.assertIn("2 轮有供给", text)

    def test_boost_hits_aggregated(self):
        """R193：四路信号加权命中数——供给快照看不到是否真打中候选"""
        _write(self.path, [
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "campaign_boost_hits": 3, "trend_boost_hits": 1, "hot_boost_hits": 2},
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "campaign_boost_hits": 1, "hot_boost_hits": 1},
            {"outcome": "run_summary", "candidates": 10, "published": 1},
        ])
        rows, _ = mr.load_rows(self.path)
        runs = mr.summarize(rows)["runs"]
        self.assertEqual(runs["boost_hits"]["campaign"], 4)
        self.assertEqual(runs["boost_hits"]["trend"], 1)
        self.assertEqual(runs["boost_hits"]["hot"], 3)
        self.assertEqual(runs["boost_runs"], 2)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("加权命中", text)
        self.assertIn("活动 4", text)
        self.assertIn("热点 3", text)

    def test_quota_intel_age_aggregated(self):
        """R196：饱和轮情报陈旧度——R195 写侧已有，报表必须从 quota_blocked 聚合"""
        rows = [
            {"outcome": "run_summary", "quota_blocked": True, "intel_age_hours": 16.0,
             "intel_degraded": True},
            {"outcome": "run_summary", "quota_blocked": True, "intel_age_hours": 2.0,
             "intel_degraded": False},
            # 发帖轮的 age 不得混入饱和轮统计
            {"outcome": "binance_published", "provider": "Preset-b.ai",
             "platforms": ["binance"], "intel_age_hours": 99.0, "intel_degraded": True,
             "widget_count": 1, "tag_count": 3},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["avg_quota_intel_age_h"], 9.0)
        self.assertEqual(s["runs"]["max_quota_intel_age_h"], 16.0)
        self.assertEqual(s["runs"]["quota_intel_degraded"], 1)
        text = mr.render_text(s, rows)
        self.assertIn("饱和轮情报", text)
        self.assertIn("降级 1 轮", text)

    def test_last_intel_refresh_ts_surfaced(self):
        """R199：最近成功刷新时刻——对照 12h 过期窗，判断饱和轮是否在喂陈旧情报"""
        rows = [
            {"outcome": "llm_success", "stage": "campaign_intel",
             "ts": "2026-09-14T22:00:00+00:00", "provider": "Preset-b.ai"},
            {"outcome": "llm_success", "stage": "campaign_intel",
             "ts": "2026-09-15T15:48:19+00:00", "provider": "Preset-b.ai"},
            {"outcome": "llm_rejected", "stage": "campaign_intel",
             "ts": "2026-09-15T16:00:00+00:00", "provider": "Preset-b.ai"},
            {"outcome": "llm_success", "stage": "summarize",
             "ts": "2026-09-15T17:00:00+00:00", "provider": "Preset-b.ai"},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["last_intel_refresh_ts"], "2026-09-15T15:48:19+00:00")
        text = mr.render_text(s, rows)
        self.assertIn("最近情报刷新", text)
        self.assertIn("2026-09-15T15:48:19", text)

    def test_campaign_off_pool_surfaced(self):
        """R201：off-pool 活动币进报表——Alpha 上新竞赛标的可见性"""
        rows = [
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "campaign_off_pool": "PIEVERSE 牛来"},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["last_campaign_off_pool"], "PIEVERSE 牛来")
        text = mr.render_text(s, rows)
        self.assertIn("off-pool", text)
        self.assertIn("PIEVERSE", text)

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

    def test_fng_anchor_hits_listed_in_offenders(self):
        """R122：FNG 命中此前只计数不落 offenders 明细——报表报 16 处命中但
        breakdown 只见装置/AI腔，FNG 锚点命中无处可查（生产实录）。命中必须
        按具体匹配短语分组进明细。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "贪婪指数都 69 了，还在喊多。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "情绪还挂在 69 的贪婪区，接盘热情高涨。"},
        ])
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(q["fng_anchor"], 2)
        self.assertTrue(any(k.startswith("FNG锚:") for k in q["offenders"]),
                        "FNG 命中必须出现在 offenders 明细里")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("FNG锚:", text)

    def test_quality_scan_fng_ban_compliance_counts(self):
        """R162：fng_ban_active 直录后的禁令咬合度量——armed 篇中避开/违反
        分计；未武装帖引用属呼吸周期合法区间；历史帖（无状态字段）不进分母。
        violation>0 即模型无视禁令，是执法升级（拒稿重写）的实证依据。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "资金流向转变，主力悄然换仓。", "fng_ban_active": True},   # 武装+避开
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "全网贪婪指数都 69 了，还在喊多。", "fng_ban_active": True},  # 武装+违反
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "情绪还挂在 69 的贪婪区，接盘热情高涨。", "fng_ban_active": False},  # 未武装+引用（合法）
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "盘面放量突破，结构健康。"},  # 历史帖无字段：不进分母
        ])
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(q["scanned"], 4)
        self.assertEqual(q["fng_ban_armed"], 2)
        self.assertEqual(q["fng_avoided"], 1)
        self.assertEqual(q["fng_violation"], 1)
        self.assertEqual(q["fng_anchor"], 2, "违反篇 + 未武装引用篇都计入锚定总数")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("FNG 禁令咬合", text)
        self.assertIn("武装 2 篇中避开 1 / 违反 1", text)

    def test_quality_scan_fng_ban_zero_violation_renders_clean(self):
        """禁令咬合全避开时也输出度量行（呼吸周期的正向验证面），无 ⚠️ 标记。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "链上数据摆在这，巨鲸动向说话。", "fng_ban_active": True},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "时间节点临近，波动率收敛。", "fng_ban_active": True},
        ])
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(q["fng_ban_armed"], 2)
        self.assertEqual(q["fng_avoided"], 2)
        self.assertEqual(q["fng_violation"], 0)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("武装 2 篇中避开 2 / 违反 0", text)
        self.assertNotIn("⚠️", text.split("FNG 禁令咬合")[1].split("\n")[0])

    def test_zero_widget_posts_surfaced(self):
        """R123：全文零有效挂件 = Write2Earn 生命线失守——保底机制被绕过的
        直接信号，报表必须显性告警。预览区无 $ 不算（可能是截断伪影）。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "widget_count": 2, "final_preview": "x"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "widget_count": 0, "final_preview": "y"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "z"},  # 旧 schema 无字段：不计入
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["zero_widget_posts"], 1, "仅 widget_count==0 的行计入")
        text = mr.render_text(s, rows)
        self.assertIn("全文零有效挂件 1/3 篇", text)

    def test_zero_tag_posts_surfaced(self):
        """R125：零标签帖 = #Write2Earn 返佣归因丢失——标签全在正文尾部，
        预览区不可见，必须靠回执的 tag_count 度量。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "tag_count": 3, "campaign_tag_count": 1, "final_preview": "x"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "tag_count": 0, "campaign_tag_count": 0, "final_preview": "y"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "z"},  # 旧 schema：不计入
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["zero_tag_posts"], 1)
        text = mr.render_text(s, rows)
        self.assertIn("全文零标签 1/3 篇", text)
        self.assertIn("返佣归因丢失", text)

    def test_campaign_tag_zero_with_fresh_intel_surfaced(self):
        """R284：活动标签是返佣归因第 3 席（创作激励活动入口），tag_count>0
        只证明保底双标签在——活动标签静默丢失时零可见。情报新鲜却零活动标签
        = _inject_campaign_tag 疑似回归，必须显性告警。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "tag_count": 3, "campaign_tag_count": 1, "intel_degraded": False},
            {"platforms": ["binance"], "outcome": "binance_published",
             "tag_count": 3, "campaign_tag_count": 0, "intel_degraded": False},
            # intel 降级时的零活动标签是合法语境（无供给），不得告警
            {"platforms": ["binance"], "outcome": "binance_published",
             "tag_count": 3, "campaign_tag_count": 0, "intel_degraded": True},
            {"platforms": ["binance"], "outcome": "binance_published",
             "tag_count": 3, "campaign_tag_count": 0},  # 无 intel 字段：同样告警
            {"platforms": ["binance"], "outcome": "binance_published",
             "tag_count": 3},  # 旧 schema 无 campaign_tag_count：不进分母
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["campaign_tag_evaluated"], 4, "仅带字段的行进分母")
        self.assertEqual(s["campaign_tag_covered"], 1)
        self.assertEqual(s["campaign_tag_zero_fresh"], 2,
                         "降级语境的零覆盖不算疑似回归")
        text = mr.render_text(s, rows)
        self.assertIn("情报新鲜但无活动标签 2/4 篇", text)
        self.assertIn("需排查", text)

    def test_run_elapsed_aggregated(self):
        """R126：单轮耗时——20 分钟外部回调节奏下的堆积预警指标。
        平均/最长聚合进 runs 段，最长逼近 1200s 时渲染告警。"""
        _write(self.path, [
            {"outcome": "run_summary", "run_elapsed_sec": 95.2},
            {"outcome": "run_summary", "run_elapsed_sec": 143.8},
            {"outcome": "run_summary"},  # 旧 schema 无字段：不进样本
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["avg_elapsed_sec"], 119.5)
        self.assertEqual(s["runs"]["max_elapsed_sec"], 143.8)
        text = mr.render_text(s, rows)
        self.assertIn("单轮耗时: 平均 119.5s / 最长 143.8s", text)
        self.assertNotIn("逼近回调节奏", text)

    def test_run_elapsed_near_cadence_warns(self):
        _write(self.path, [{"outcome": "run_summary", "run_elapsed_sec": 1180.0}])
        rows, _ = mr.load_rows(self.path)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("逼近回调节奏", text)

    def test_ending_style_distribution(self):
        """R130：结尾套路分布——验证 ShuffleBag 生产轮换均匀性的观测面。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "ending_style": "极简站队", "final_preview": "x"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "ending_style": "极简站队", "final_preview": "y"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "ending_style": "仓位表白", "final_preview": "z"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "w"},  # 旧 schema：不进分布
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(dict(s["by_ending"]), {"极简站队": 2, "仓位表白": 1})
        text = mr.render_text(s, rows)
        self.assertIn("结尾套路分布", text)
        self.assertIn("极简站队 ×2", text)


class TestOpenerFingerprintRadar(unittest.TestCase):
    """R124：开场指纹雷达——把 R104/R121 的人工发现过程产品化，共享前缀
    ≥3/10 预警。只预警不禁令（自动禁令有误杀风险）。"""

    @staticmethod
    def _rows(opener_texts):
        return [{"outcome": "binance_published", "final_preview": t}
                for t in opener_texts]

    def test_cluster_detected(self):
        rows = self._rows(["刚刚,$SHIB 筹码变化。后续。",
                           "刚刚 Solana 链上爆量。后续。",
                           "刚刚 $BTC 跌破关口。后续。",
                           "Bitwise 把 ETF 关了。后续。",
                           "1500万枚 RLUSD 烧了。后续。"])
        fp = mr.opener_fingerprint(rows)
        self.assertEqual(fp["scanned"], 5)
        self.assertEqual(fp["alerts"].get("刚刚"), 3)

    def test_no_alert_when_diverse(self):
        rows = self._rows(["Bitwise 把 ETF 关了。", "1500万枚 RLUSD 烧了。",
                           "量子攻击成本被砍。", "2691% 爆仓比。", "3610 亿枚被吞。"])
        fp = mr.opener_fingerprint(rows)
        self.assertEqual(fp["alerts"], {})

    def test_article_headers_skipped(self):
        """长文分节头不是开场句（与 main._recent_openers R121 同语义）。"""
        rows = self._rows(["一、发生了什么\n\nBitwise 把 ETF 关了。后续。",
                           "一、发生了什么\n\n1500万枚 RLUSD 烧了。后续。",
                           "一、发生了什么\n\n量子攻击成本被砍。后续。"])
        fp = mr.opener_fingerprint(rows)
        self.assertEqual(fp["alerts"], {}, "分节头不得被当成开场句聚簇")

    def test_four_char_cluster_suppresses_two_char_subcluster(self):
        # 同簇只报最长前缀："先泼盆冷"×3 也意味着"先泼"×3，只报前者
        rows = self._rows(["先泼盆冷水,贪婪 66。", "先泼盆冷水,别追高。",
                           "先泼盆冷水,稳住。", "Bitwise 关 ETF。", "RLUSD 烧了。"])
        fp = mr.opener_fingerprint(rows)
        self.assertIn("先泼盆冷", fp["alerts"])
        self.assertNotIn("先泼", fp["alerts"], "被 4 字簇包含的 2 字簇不重复报")

    def test_entity_name_prefix_not_a_fingerprint(self):
        """R131：2 字簇要求词边界——Bitwise/BitGo/Bitcoin 共享的"Bi"只是
        不同实体的词前半（生产实录：雷达误报"Bi…"×3），不得聚簇报警。"""
        rows = self._rows(["Bitwise 把 ETF 关了。", "BitGo 钱包被端。",
                           "Bitcoin 突破关口。", "RLUSD 烧了。", "量子攻击。"])
        self.assertEqual(mr.opener_fingerprint(rows)["alerts"], {})

    def test_cjk_follower_counts_as_boundary(self):
        # "刚刚看涨"的"看"是 CJK——非 ASCII 字母数字即词边界，正常聚簇
        rows = self._rows(["刚刚看涨情绪升温。", "刚刚跌破关键位。",
                           "刚刚放量突破。", "其他 A。", "其他 B。"])
        self.assertEqual(mr.opener_fingerprint(rows)["alerts"].get("刚刚"), 3)

    def test_render_line_present(self):
        rows = self._rows(["刚刚 A。", "刚刚 B。", "刚刚 C。", "其他 D。"])
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("开场指纹预警", text)
        self.assertIn("刚刚", text)


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

    def test_title_leadins_in_sync(self):
        """R286：长文标题禁用领词表是 main._GENERIC_LEADINS 的第二份事实源
        （R282 把'刚出'提进静态表后，标题侧必须同刻跟上，否则标题漏防）。"""
        self.assertEqual(tuple(mr._TITLE_LEADINS), tuple(m._GENERIC_LEADINS),
                         "标题领词表与正文领词表不一致——改 main 必须同步 metrics_report")


class TestArticleTitleHookCensus(unittest.TestCase):
    """R286：长文标题眼钩普查——article_title 落盘 129 条零消费面的缺口闭合"""

    def setUp(self):
        import tempfile, shutil
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "metrics.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _row(self, title, **kw):
        d = {"platforms": ["binance"], "outcome": "binance_published",
             "article_title": title, "final_preview": "x"}
        d.update(kw)
        return d

    def test_hook_census_and_leadin_alert(self):
        """数字/$挂件/疑问三类眼钩各自计数；命中禁用领词的标题单独告警"""
        _write(self.path, [
            self._row("20天狂买1.07亿美元，Bitwise悄悄吸筹$SOL"),
            self._row("$SHIB掌门失联4个月，改个资料就想搞事？"),
            self._row("BTC $82000 Battle"),
            self._row("刚出炉：Fed升息落地，$BTC守住7.65万"),
            self._row("突发，某交易所又出事了", article=False),  # 短讯也可能带标题
            self._row(""),  # 空标题（短讯常态）不进分母
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(len(s["article_titles"]), 5)
        self.assertEqual(s["title_hooks"]["数字钩子"], 4)   # 除"BTC $82000 Battle"外都有数字
        self.assertEqual(s["title_hooks"]["$挂件"], 4)
        self.assertEqual(s["title_hooks"]["疑问钩子"], 1)
        self.assertEqual(len(s["title_leadin_hits"]), 2, "刚出/突发两个领词命中")
        text = mr.render_text(s, rows)
        self.assertIn("长文标题（5 篇", text)
        self.assertIn("数字钩子 4/5", text)
        self.assertIn("标题命中禁用领词 2/5", text)
        self.assertIn("更显眼的指纹位", text)

    def test_no_titles_renders_nothing(self):
        """全是短讯（标题为空）时整块不渲染（零噪音）"""
        _write(self.path, [self._row(""), self._row(None)])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["article_titles"], [])
        self.assertNotIn("长文标题", mr.render_text(s, rows))


class TestPersonaAndImpactDistribution(unittest.TestCase):
    """R288：人设分布/近期集中度 + 热度分分布（R284 对账清单剩余两项）"""

    def setUp(self):
        import tempfile, shutil
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "metrics.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _row(self, persona, imp):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "final_preview": "x", "persona": persona, "impact_score": imp}

    def test_distribution_and_concentration_alert(self):
        """人设分布按池计数；近 12 窗内单人设过半才告警（阈值 = 50%）"""
        rows = [self._row("数据拆解派", 32) for _ in range(6)]
        rows += [self._row("毒舌老韭菜", 22) for _ in range(4)]
        rows += [self._row("吃瓜叙事党", 18) for _ in range(2)]
        _write(self.path, rows)
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        self.assertEqual(s["by_persona"]["数据拆解派"], 6)
        self.assertEqual(len(s["persona_seq"]), 12)
        text = mr.render_text(s, loaded)
        self.assertIn("🎭 人设分布: 数据拆解派 ×6 · 毒舌老韭菜 ×4 · 吃瓜叙事党 ×2", text)
        self.assertIn("⚠️ 近 12 帖人设集中: 数据拆解派 6/12", text)
        self.assertIn("R287 跨运行预热后仍扎堆需排查", text)

    def test_balanced_window_silent(self):
        """均衡窗口不告警（零噪音）；分布行仍渲染。老段集中+近窗均衡的区分样本：
        集中度告警只许看近 12 帖——若退化成全史窗口，老段的扎堆会误报。"""
        old = [self._row("数据拆解派", 20) for _ in range(6)]      # 老段扎堆
        recent = [self._row(p, 20 + i) for i, p in enumerate(
            ["毒舌老韭菜", "吃瓜叙事党", "数据拆解派"] * 4)]       # 近 12 帖均衡
        _write(self.path, old + recent)
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        text = mr.render_text(s, loaded)
        self.assertIn("🎭 人设分布", text)
        self.assertNotIn("人设集中", text, "近窗均衡不得被老段扎堆误报")

    def test_impact_score_distribution(self):
        """热度分中位/P90/≥30 放行档计数——门槛校准基线"""
        _write(self.path, [self._row("毒舌老韭菜", v) for v in
                           [10, 12, 15, 20, 22, 25, 28, 30, 33, 40]])
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        self.assertEqual(len(s["impact_scores"]), 10)
        text = mr.render_text(s, loaded)
        self.assertIn("🔥 热度分:", text)
        self.assertIn("中位 25", text)
        self.assertIn("P90 40", text)
        self.assertIn("≥30 放行档 3/10 篇", text)

    def test_legacy_rows_without_fields_silent(self):
        """旧 schema 无 persona/impact_score：两块都不渲染"""
        _write(self.path, [{"platforms": ["binance"],
                            "outcome": "binance_published", "final_preview": "x"}])
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        text = mr.render_text(s, loaded)
        self.assertNotIn("🎭 人设分布", text)
        self.assertNotIn("🔥 热度分", text)


class TestFunnelCountsPublishFailures(unittest.TestCase):
    """R6：publish_failed 必须进分母——否则"发布全挂"会被报表显示成高成功率"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "metrics.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_publish_failed_counts_as_attempt(self):
        _write(self.path, [
            {"title": "A", "platforms": ["binance"], "outcome": "binance_published"},
            {"title": "B", "platforms": [], "outcome": "publish_failed", "stage": "publish"},
        ])
        rows, _ = mr.load_rows(self.path)
        f = mr.funnel(rows)
        self.assertEqual(f["delivered"], 1)
        self.assertEqual(f["attempted"], 2,
                         "投递失败的故事被算进分母，成功率才不会被虚高")
        self.assertEqual(f["rate"], 0.5)

    def test_all_publishes_failed_is_not_100_percent(self):
        """只有 publish_failed 行时成功率必须是 0，而不是"没有尝试"或 100%"""
        _write(self.path, [
            {"title": "A", "platforms": [], "outcome": "publish_failed", "stage": "publish"},
            {"title": "B", "platforms": [], "outcome": "publish_failed", "stage": "publish"},
        ])
        rows, _ = mr.load_rows(self.path)
        f = mr.funnel(rows)
        self.assertEqual(f["delivered"], 0)
        self.assertEqual(f["attempted"], 2)
        self.assertEqual(f["rate"], 0.0)

    def test_publish_failed_then_delivered_counts_as_rescued(self):
        _write(self.path, [
            {"title": "A", "ts": "2026-09-15T01:00:00+00:00",
             "platforms": [], "outcome": "publish_failed", "stage": "publish"},
            {"title": "A", "ts": "2026-09-15T01:20:00+00:00",
             "platforms": ["binance"], "outcome": "binance_published"},
        ])
        rows, _ = mr.load_rows(self.path)
        f = mr.funnel(rows)
        self.assertEqual(f["delivered"], 1)
        self.assertEqual(f["attempted"], 1, "同题同日按一个故事计")
        self.assertEqual(f["failover_rescued"], 1, "失败后重投成功 = 被救回")


class TestNextSlotFreesReadSide(unittest.TestCase):
    """R6：next_slot_frees 由任何 run_summary 行提供（发帖轮也写），不能只在配额行里找"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "metrics.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_taken_from_posting_round_too(self):
        _write(self.path, [
            {"outcome": "run_summary", "quota_blocked": True,
             "next_slot_frees": "2026-09-15T10:00:00+00:00", "next_slot_frees_min": 300},
            # 后续正常发帖轮也写了该字段（R129 写侧扩展）→ 报表应取更新的这条
            {"outcome": "run_summary", "quota_blocked": False,
             "next_slot_frees": "2026-09-15T09:00:00+00:00", "next_slot_frees_min": 60},
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["next_slot_frees"], "2026-09-15T09:00:00+00:00")
        self.assertEqual(s["runs"]["next_slot_frees_min"], 60)

    def test_quota_blocked_count_unchanged(self):
        _write(self.path, [
            {"outcome": "run_summary", "quota_blocked": True, "next_slot_frees_min": 300},
            {"outcome": "run_summary", "quota_blocked": False, "next_slot_frees_min": 60},
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["quota_blocked"], 1)

    def test_malformed_value_ignored(self):
        _write(self.path, [
            {"outcome": "run_summary", "next_slot_frees_min": "not-a-number"},
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertNotIn("next_slot_frees_min", s["runs"])


class TestSegmentSampleCounts(unittest.TestCase):
    """R6：分段耗时均值必须带样本量，否则会读出"分段和 > 总量"的假象"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "metrics.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_sample_counts_exposed_and_rendered(self):
        rows = [{"outcome": "run_summary", "run_elapsed_sec": 30} for _ in range(4)]
        rows.append({"outcome": "run_summary", "run_elapsed_sec": 40, "llm_elapsed_sec": 52})
        _write(self.path, rows)
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        self.assertEqual(s["runs"]["n_elapsed"], 5)
        self.assertEqual(s["runs"]["n_llm_sec"], 1)
        text = mr.render_text(s, loaded)
        self.assertIn("样本 1 轮", text)


class TestContentStatsImport(unittest.TestCase):
    """R285：浏览/互动数据导入（CSV → content_stats.jsonl）"""

    def setUp(self):
        import tempfile, shutil
        self.tmpdir = tempfile.mkdtemp()
        self.csv = os.path.join(self.tmpdir, "content_stats.csv")
        self.out = os.path.join(self.tmpdir, "content_stats.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _import(self):
        spec = importlib.util.spec_from_file_location(
            "import_content_stats",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "scripts", "import_content_stats.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_chinese_headers_parsed_and_deduped_max(self):
        """后台导出是中文表头；同 id 多行（每周重复导出）取 max——浏览量单调递增。
        千分位逗号在合法 CSV 里必须带引号（无引号的 "1,000" 会把列错位，那是
        导出器配置问题，导入器按列号取数不猜）。"""
        with open(self.csv, "w", encoding="utf-8") as f:
            f.write("帖子ID,浏览量,点赞,评论\n")
            f.write('111,"1,000",12,3\n')    # 带引号的千分位：字段内逗号必须吃掉
            f.write("111,800,12,3\n")        # 二次导出：更高读数
            f.write("222,350,5,1\n")
            f.write(",999,1,1\n")            # 无 id 行跳过
        mod = self._import()
        recs = mod.read_csv(self.csv)
        self.assertEqual(recs, {"111": {"views": 1000, "likes": 12, "comments": 3},
                                "222": {"views": 350, "likes": 5, "comments": 1}},
                         "同 id 取 max 观测，千分位逗号吃掉")

    def test_merge_keeps_prior_observations_and_upgrades(self):
        """merge 进 jsonl：已有观测不得被更低的新读数覆盖，新帖子追加"""
        with open(self.out, "w", encoding="utf-8") as f:
            f.write(json.dumps({"content_id": "111", "views": 5000,
                                "likes": 30, "comments": 9}) + "\n")
        mod = self._import()
        mod.OUT_PATH = self.out
        changed = mod.merge_into_jsonl({"111": {"views": 4000, "likes": 31},
                                        "222": {"views": 10}})
        rows = [json.loads(l) for l in open(self.out, encoding="utf-8")]
        by_id = {r["content_id"]: r for r in rows}
        self.assertEqual(by_id["111"]["views"], 5000, "浏览是单调递增量，取 max")
        self.assertEqual(by_id["111"]["likes"], 31, "其他指标同样取 max")
        self.assertEqual(by_id["222"]["views"], 10)
        self.assertEqual(changed, 2)


class TestContentStatsReportJoin(unittest.TestCase):
    """R285：报表侧 content_id × content_stats 的 join 与三维归因"""

    def setUp(self):
        import tempfile, shutil
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "metrics.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _rows(self):
        return [
            {"platforms": ["binance"], "outcome": "binance_published",
             "content_id": "c1", "hour_bj": 21, "article": False, "source": "U.Today"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "content_id": "c2", "hour_bj": 22, "article": False, "source": "U.Today"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "content_id": "c3", "hour_bj": 2, "article": True, "source": "CryptoSlate"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "content_id": "c4", "hour_bj": 9, "article": False, "source": "CryptoSlate"},
            # 18 点边界两侧各钉一样本：上游分桶阈值若被改动，下面断言立刻红
            {"platforms": ["binance"], "outcome": "binance_published",
             "content_id": "c5", "hour_bj": 15, "article": False, "source": "Decrypt"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "content_id": "c6", "hour_bj": 19, "article": True, "source": "Decrypt"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "content_id": None, "hour_bj": 21, "article": False, "source": "U.Today"},
        ]

    def _stats_file(self, stats):
        p = os.path.join(self.tmpdir, "content_stats.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for cid, rec in stats.items():
                f.write(json.dumps({"content_id": cid, **rec},
                                   ensure_ascii=False) + "\n")
        return p

    def test_join_and_three_dimension_buckets(self):
        """有浏览数据的帖才进面板；时段/体裁/来源各自分桶算均浏览"""
        stats = {"c1": {"views": 300, "likes": 5, "comments": 2},
                 "c2": {"views": 500, "likes": 7, "comments": 4},
                 "c3": {"views": 900, "likes": 20, "comments": 11},
                 "c4": {"views": 100, "likes": 1, "comments": 0},
                 "c5": {"views": 200, "likes": 3, "comments": 1},
                 "c6": {"views": 150, "likes": 4, "comments": 2}}
        sp = self._stats_file(stats)
        orig = mr._STATS_CACHE.copy()
        try:
            mr._STATS_CACHE.update({"loaded": True, "data": mr.load_content_stats(sp)})
            _write(self.path, self._rows())
            rows, _ = mr.load_rows(self.path)
            rows = mr.summarize(rows)
        finally:
            mr._STATS_CACHE.update(orig)
        self.assertEqual(rows["stats_posts"], 6, "content_id 为 None 的行不进分母")
        self.assertEqual(rows["stats_views_total"], 2150)
        self.assertEqual(rows["stats_by_hourbucket"]["晚间18-24"], [300, 500, 150])
        self.assertEqual(rows["stats_by_hourbucket"]["凌晨0-6"], [900])
        self.assertEqual(rows["stats_by_hourbucket"]["下午12-18"], [200])
        self.assertEqual(rows["stats_by_hourbucket"]["上午6-12"], [100])
        self.assertEqual(rows["stats_by_genre"]["长文"], [900, 150])
        self.assertEqual(sorted(rows["stats_by_source"]["U.Today"]), [300, 500])
        text = mr.render_text(rows, self._rows())
        self.assertIn("内容数据（6 篇有记录）", text)
        self.assertIn("均浏览 358", text)
        self.assertIn("时段均浏览", text)
        self.assertIn("体裁均浏览", text)
        self.assertIn("来源均浏览", text)

    def test_no_stats_file_renders_nothing(self):
        """没有 content_stats.jsonl 时整块面板不渲染（零噪音）"""
        orig = mr._STATS_CACHE.copy()
        try:
            mr._STATS_CACHE.update({"loaded": True, "data": {}})
            rows = mr.summarize(self._rows())
        finally:
            mr._STATS_CACHE.update(orig)
        self.assertEqual(rows["stats_posts"], 0)
        self.assertNotIn("内容数据", mr.render_text(rows, self._rows()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
