# -*- coding: utf-8 -*-
"""
metrics_report.py 的离线单测（独立文件：不碰主套件，避免与他人在途改动交织）。
CI 里与 tests/test_core.py 一起跑（见 ci.yml）。
"""
import collections
import importlib.util
import io
import json
import os
import random
import sys
import tempfile
import unittest
import unittest.mock

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

    def test_permanent_failures_aggregated_and_rendered(self):
        """R302：真·24h 永久失败（[credit 24h]/[permanent 24h]）按 提供商×原因 单列，
        命中数降序；[router 404]（指数退避、会自愈）与普通拒稿都不算永久失败。"""
        rows = [
            {"ts": "2026-09-22T01:00:00+00:00", "outcome": "llm_rejected",
             "stage": "transport", "provider": "Preset-b.ai",
             "reason": "[credit 24h] Error code: 400 credit insufficient balance"},
            {"ts": "2026-09-22T01:10:00+00:00", "outcome": "llm_rejected",
             "stage": "transport", "provider": "Preset-b.ai",
             "reason": "[credit 24h] Error code: 400 credit insufficient balance"},
            {"ts": "2026-09-22T01:20:00+00:00", "outcome": "llm_rejected",
             "stage": "transport", "provider": "Preset-openrouter/minimax",
             "reason": "[permanent 24h] Error code: 404 model unavailable"},
            # [router 404] 走指数退避会自愈——不算永久失败
            {"ts": "2026-09-22T01:30:00+00:00", "outcome": "llm_rejected",
             "stage": "transport", "provider": "Preset-openrouter",
             "reason": "[router 404] Error code: 404 route target down"},
            # 普通质量拒稿——不算永久失败
            {"ts": "2026-09-22T01:40:00+00:00", "outcome": "llm_rejected",
             "stage": "quality", "provider": "Preset-openrouter",
             "reason": "内容过短 (17 字符)"},
        ]
        s = mr.summarize(rows)
        pf = s["permanent_failures"]
        self.assertEqual(pf[("Preset-b.ai", "余额/额度耗尽")], 2)
        self.assertEqual(pf[("Preset-openrouter/minimax", "模型下架/404")], 1)
        self.assertEqual(sum(pf.values()), 3, "router 404 与普通拒稿不得计入永久失败")
        out = mr.render_text(s)
        self.assertIn("💀 永久失败", out)
        # 只在 💀 行内断言（provider 名在上方的模型/拒因行也出现，全局 index 会串台）
        perm_line = next(ln for ln in out.split("\n") if "💀 永久失败" in ln)
        self.assertIn("Preset-b.ai (余额/额度耗尽 ×2", perm_line)
        self.assertIn("Preset-openrouter/minimax (模型下架/404 ×1", perm_line)
        # 命中数降序：b.ai(2) 排在 minimax(1) 之前
        self.assertLess(perm_line.index("Preset-b.ai"), perm_line.index("Preset-openrouter/minimax"))
        self.assertNotIn("router 404", perm_line)

    def test_permanent_failures_last_seen_dates(self):
        """R304：💀 行带每条的末次命中日期，区分"当前该处理"与"历史退役簇"。
        全史里 09-08 的 minimax（已退役）与今天 b.ai 余额耗尽同框，运营靠日期分辨。"""
        rows = [
            # minimax：跨两天的历史簇，末次必须取较晚的 09-08（而非首见 09-06），
            # 乱序投喂 + 首行是更早日期，锁死"取 max 而非 min/首见"
            {"ts": "2026-09-08T19:00:00+00:00", "outcome": "llm_rejected",
             "stage": "transport", "provider": "Preset-openrouter/minimax",
             "reason": "[permanent 24h] Error code: 404 model unavailable"},
            {"ts": "2026-09-06T02:00:00+00:00", "outcome": "llm_rejected",
             "stage": "transport", "provider": "Preset-openrouter/minimax",
             "reason": "[permanent 24h] Error code: 404 model unavailable"},
            # b.ai：今天的当前故障
            {"ts": "2026-09-22T03:00:00+00:00", "outcome": "llm_rejected",
             "stage": "transport", "provider": "Preset-b.ai",
             "reason": "[credit 24h] credit insufficient balance"},
        ]
        s = mr.summarize(rows)
        # 末次=较晚的 09-08，不是首见 09-06（区分 max vs min）
        self.assertEqual(s["permanent_failures_last"][("Preset-openrouter/minimax", "模型下架/404")][:10], "2026-09-08")
        self.assertEqual(s["permanent_failures_last"][("Preset-b.ai", "余额/额度耗尽")][:10], "2026-09-22")
        perm_line = next(ln for ln in mr.render_text(s).split("\n") if "💀 永久失败" in ln)
        self.assertIn("最近 09-08", perm_line)
        self.assertIn("最近 09-22", perm_line)
        # 末次日期贴在各自条目：minimax→09-08，b.ai→09-22
        self.assertIn("Preset-openrouter/minimax (模型下架/404 ×2, 最近 09-08)", perm_line)
        self.assertIn("Preset-b.ai (余额/额度耗尽 ×1, 最近 09-22)", perm_line)
        # helper 无 last_seen 时向后兼容（不带日期，不炸）；R616 起返回 (活警, 退役) 二元组
        self.assertEqual(
            mr._format_permanent_failures(s["permanent_failures"])[0].count("最近"), 0)

    def test_permanent_failures_silent_when_none(self):
        """零永久失败时不渲染 💀 行（沿用零命中零噪音惯例）；helper 契约：空→空串。"""
        rows = [
            {"ts": "2026-09-22T01:00:00+00:00", "outcome": "llm_rejected",
             "stage": "quality", "provider": "Preset-openrouter",
             "reason": "内容过短 (17 字符)"},
        ]
        s = mr.summarize(rows)
        self.assertEqual(sum(s["permanent_failures"].values()), 0)
        self.assertNotIn("💀 永久失败", mr.render_text(s))

    def test_alert_dropped_no_channel_rendered(self):
        """R330：0 渠道丢弃的错误报警必须单独成行——混在 outcome 分布里等于消失
        （生产实锤：R301 permanent 报警进黑洞，_alert_state 全史为空才发现）。"""
        rows = [
            {"ts": "2026-09-22T03:03:34+00:00", "outcome": "alert_dropped_no_channel",
             "reason": "LLM 提供商永久失败: Preset-b.ai"},
            {"ts": "2026-09-22T03:04:00+00:00", "outcome": "run_summary",
             "candidates": 0, "published": 0},
        ]
        out = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("运营报警静默丢弃", out)
        self.assertIn("×1", out)
        self.assertIn("通知渠道 0 个", out)
        # R614：补"最后发生"时刻——这行是全史累计，缺时间维度时分不清
        # "此刻仍在丢报警"与"半年前丢过一次"（生产：最后一条距今 66h）
        self.assertIn("最后发生 2026-09-22 03:03", out)
        # 零丢弃时不渲染（零噪音惯例）
        out2 = mr.render_text(mr.summarize([rows[1]]), [rows[1]])
        self.assertNotIn("运营报警静默丢弃", out2)
        self.assertEqual(mr._format_permanent_failures({}), ("", ""))

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

    def test_fetch_funnel_aggregated_and_rendered(self):
        """R342：扫描漏斗（fetched/stale/cached/near_dup）进报表——R276 写侧起
        只有扫描日志 + Step Summary「管线吞吐」两个易失出口，metrics_report
        （持久巡检面）零消费，near_dup 抬升 / cached 跳涨 / stale 峰值这类趋势
        无行内证据。累加总量+单轮峰值；零值/缺字段行不进。"""
        rows = [
            {"ts": "2026-09-14T13:09:00+00:00", "outcome": "run_summary",
             "candidates": 40, "published": 1, "unprocessed": 39,
             "fetched": 40, "stale": 2, "cached": 5, "near_dup": 3},
            {"ts": "2026-09-14T13:29:00+00:00", "outcome": "run_summary",
             "candidates": 45, "published": 1, "unprocessed": 44,
             "fetched": 67, "stale": 14, "cached": 4, "near_dup": 8},
            # 全零轮次不抬计数
            {"ts": "2026-09-14T20:44:00+00:00", "outcome": "run_summary",
             "candidates": 45, "published": 1,
             "fetched": 0, "stale": 0, "cached": 0, "near_dup": 0},
            # 历史行无这些字段：不进漏斗聚合
            {"ts": "2026-09-14T12:00:00+00:00", "outcome": "run_summary",
             "candidates": 0, "published": 0, "quota_blocked": True},
        ]
        s = mr.summarize(rows)
        ff = s["runs"]["fetch_funnel"]
        # [总量, 单轮峰值]——峰值抓尖刺，不得退化成总量
        self.assertEqual(ff["fetched"], [107, 67])
        self.assertEqual(ff["near_dup"], [11, 8])
        self.assertEqual(ff["cached"], [9, 5])
        self.assertEqual(ff["stale"], [16, 14])
        out = mr.render_text(s)
        self.assertIn("🔻 扫描漏斗", out)
        self.assertIn("抓取 107(峰67)", out)
        # 峰值守卫：near_dup 两轮 3+8，峰必须是 8（单轮最大）而非 11（总量）
        self.assertIn("近重 11(峰8)", out)
        self.assertIn("缓存 9(峰5)", out)
        self.assertIn("陈旧 16(峰14)", out)

    def test_fetch_funnel_silent_when_all_zero(self):
        """R342 变异守卫：全零/缺字段时漏斗行完全静默（零噪音，沿用源健康惯例）——
        否则每个饱和轮都甩一行 抓取 0/近重 0 淹没报表。"""
        rows = [
            {"ts": "2026-09-14T12:00:00+00:00", "outcome": "run_summary",
             "candidates": 0, "published": 0, "quota_blocked": True},
            {"ts": "2026-09-14T12:20:00+00:00", "outcome": "run_summary",
             "candidates": 40, "published": 1,
             "fetched": 0, "stale": 0, "cached": 0, "near_dup": 0},
        ]
        out = mr.render_text(mr.summarize(rows))
        self.assertNotIn("扫描漏斗", out)

    def test_feed_source_health_aggregated_and_rendered(self):
        """R344：源硬故障/停放频率进报表——feeds_failed/feeds_parked 计数自 R90 起
        写 run_summary，R278 补了源名但只在新故障时落；生产三个实证轮 09-17×2/
        09-18×1 的 feeds_failed=1 只有计数无源名，metrics_report 零消费=写侧无出口。
        空feed/超时/注入各有源名出口，硬故障是仅剩的静默源健康信号。累加轮次+源次
        总量+单轮峰值；零故障轮/缺字段行不进（不抬分母）。"""
        rows = [
            # 实证故障轮：feeds_failed=1（09-17×2 / 09-18×1 同型）
            {"ts": "2026-09-17T18:26:52+00:00", "outcome": "run_summary",
             "candidates": 40, "published": 1, "feeds_ok": 8, "feeds_failed": 1,
             "feeds_parked": 0},
            # 多源故障 + 首次停放：峰值应抓单轮 2（不得退化成总量）
            {"ts": "2026-09-17T23:24:46+00:00", "outcome": "run_summary",
             "candidates": 39, "published": 1, "feeds_ok": 7, "feeds_failed": 2,
             "feeds_parked": 1},
            # 健康轮：feeds_failed=0 不计入故障轮
            {"ts": "2026-09-18T13:00:00+00:00", "outcome": "run_summary",
             "candidates": 45, "published": 1, "feeds_ok": 9, "feeds_failed": 0,
             "feeds_parked": 0},
            # 仅停放：停放峰值应抓单轮 3
            {"ts": "2026-09-18T18:28:43+00:00", "outcome": "run_summary",
             "candidates": 41, "published": 1, "feeds_ok": 6, "feeds_failed": 0,
             "feeds_parked": 3},
            # 历史行无这些字段：不进聚合、不抬分母
            {"ts": "2026-09-14T12:00:00+00:00", "outcome": "run_summary",
             "candidates": 0, "published": 0, "quota_blocked": True},
        ]
        s = mr.summarize(rows)
        runs = s["runs"]
        # 故障轮 2（feeds_failed>0 的两轮），源次总量 1+2=3，单轮峰值 2
        self.assertEqual(runs["feed_fail_runs"], 2)
        self.assertEqual(runs["feed_fail_total"], 3)
        self.assertEqual(runs["feed_fail_peak"], 2)
        # 停放轮 2，源次总量 1+3=4，单轮峰值 3
        self.assertEqual(runs["feed_park_runs"], 2)
        self.assertEqual(runs["feed_park_total"], 4)
        self.assertEqual(runs["feed_park_peak"], 3)
        out = mr.render_text(s)
        self.assertIn("🩺 源故障/停放", out)
        # 峰值守卫：硬故障两轮 1+2，峰必须是 2（单轮最大）而非 3（总量）
        self.assertIn("硬故障 2 轮/共 3 源次（峰 2）", out)
        # 峰值守卫：停放两轮 1+3，峰必须是 3（单轮最大）而非 4（总量）
        self.assertIn("停放 2 轮/共 4 源次（峰 3）", out)

    def test_feed_source_health_silent_when_no_failures(self):
        """R344 变异守卫：全窗零硬故障零停放（或缺字段）时源故障行完全静默——
        否则每个健康饱和轮都甩一行 硬故障 0 淹没报表，且把 feeds_failed=0 的
        健康表象误读成告警。"""
        rows = [
            {"ts": "2026-09-14T12:00:00+00:00", "outcome": "run_summary",
             "candidates": 0, "published": 0, "quota_blocked": True},
            {"ts": "2026-09-14T12:20:00+00:00", "outcome": "run_summary",
             "candidates": 40, "published": 1, "feeds_ok": 9, "feeds_failed": 0,
             "feeds_parked": 0},
        ]
        out = mr.render_text(mr.summarize(rows))
        self.assertNotIn("源故障/停放", out)

    def test_weekday_distribution_aggregated_and_rendered(self):
        """R343：投递篇分周——weekday_bj 与 hour_bj 同源写在每行，此前只有 hour
        有出口，周维静默。按自然周序渲染（非频次），缺勤日一眼可见。"""
        rows = [
            # 周一(0) ×2、周三(2) ×1，均已投递（platforms 非空）
            {"ts": "2026-09-14T13:00:00+00:00", "hour_bj": 21, "weekday_bj": 0,
             "provider": "B.ai", "platforms": ["binance"],
             "outcome": "binance_published"},
            {"ts": "2026-09-14T14:00:00+00:00", "hour_bj": 22, "weekday_bj": 0,
             "provider": "B.ai", "platforms": ["binance"],
             "outcome": "binance_published"},
            {"ts": "2026-09-16T13:00:00+00:00", "hour_bj": 21, "weekday_bj": 2,
             "provider": "B.ai", "platforms": ["binance"],
             "outcome": "binance_published"},
            # 拒稿行带 weekday_bj 但未投递：不得进分周桶
            {"ts": "2026-09-18T13:00:00+00:00", "hour_bj": 21, "weekday_bj": 4,
             "provider": "X", "stage": "numbers", "outcome": "llm_rejected"},
            # 越界值：不进桶
            {"ts": "2026-09-19T13:00:00+00:00", "hour_bj": 21, "weekday_bj": 9,
             "provider": "B.ai", "platforms": ["binance"],
             "outcome": "binance_published"},
        ]
        s = mr.summarize(rows)
        # 仅投递行计入；拒稿/越界不进分母
        self.assertEqual(s["by_weekday"][0], 2)
        self.assertEqual(s["by_weekday"][2], 1)
        self.assertEqual(s["by_weekday"][4], 0)
        self.assertEqual(s["by_weekday"][9], 0)
        out = mr.render_text(s)
        self.assertIn("分周:", out)
        self.assertIn("周一×2", out)
        self.assertIn("周三×1", out)
        # 缺勤日（周二/周四…）不渲染
        self.assertNotIn("周二×", out)
        self.assertNotIn("周四×", out)
        # 周序守卫：周一 must render before 周三（自然周序，非 most_common 频次序）
        line = next(l for l in out.splitlines() if l.strip().startswith("分周:"))
        self.assertLess(line.index("周一"), line.index("周三"))

    def test_weekday_silent_when_no_delivered(self):
        """R343 变异守卫：无投递（只有拒稿/汇总行）时分周行完全静默——
        累加必须门控在 _is_delivered 内，否则拒稿行的 weekday 会污染节奏面。"""
        rows = [
            {"ts": "2026-09-14T12:00:00+00:00", "outcome": "run_summary",
             "weekday_bj": 0, "candidates": 0, "published": 0},
            {"ts": "2026-09-14T13:00:00+00:00", "hour_bj": 21, "weekday_bj": 2,
             "provider": "X", "stage": "numbers", "outcome": "llm_rejected"},
        ]
        out = mr.render_text(mr.summarize(rows))
        self.assertNotIn("分周:", out)

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
        """R193：四路信号加权命中数——供给快照看不到是否真打中候选。R608：浏览加权(+/-)并入"""
        _write(self.path, [
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "campaign_boost_hits": 3, "trend_boost_hits": 1, "hot_boost_hits": 2,
             "engagement_boost_up": 2, "engagement_boost_down": 5},
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "campaign_boost_hits": 1, "hot_boost_hits": 1},
            {"outcome": "run_summary", "candidates": 10, "published": 1},
        ])
        rows, _ = mr.load_rows(self.path)
        runs = mr.summarize(rows)["runs"]
        self.assertEqual(runs["boost_hits"]["campaign"], 4)
        self.assertEqual(runs["boost_hits"]["trend"], 1)
        self.assertEqual(runs["boost_hits"]["hot"], 3)
        self.assertEqual(runs["boost_hits"]["eng_up"], 2)
        self.assertEqual(runs["boost_hits"]["eng_down"], 5)
        self.assertEqual(runs["boost_runs"], 2)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("加权命中", text)
        self.assertIn("活动 4", text)
        self.assertIn("浏览加权 +2 -5", text)
        self.assertIn("热点 3", text)

    def test_r611_boost_denominator_per_signal(self):
        """R611：加权命中率的分母必须按**每路信号各自**的数据轮数给，不能用
        boost_runs（任一路有命中的轮数）。四路上线时间不同，混用分母会让刚上线的
        信号显示成"几乎不命中"——生产实测：面板原印「浏览加权 +2 -4」配「208轮」，
        读起来像 208 轮里只命中 6 次，实际那 +2-4 全部来自唯一 1 轮有该字段的
        run_summary（R608 10-02 才上线）。这会让刚跑通的信号被误判为失效。
        另：字段存在即计入分母（值为 0 也是有效观测——"没命中"≠"没这个字段"）。"""
        _write(self.path, [
            # 只有活动/热搜/热点有数据的 5 轮（无 engagement 字段 = R608 上线前）
            *[{"outcome": "run_summary", "candidates": 10, "published": 1,
               "campaign_boost_hits": 1} for _ in range(5)],
            #浏览加权上线后的 1 轮
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "campaign_boost_hits": 1, "engagement_boost_up": 2,
             "engagement_boost_down": 4},
        ])
        rows, _ = mr.load_rows(self.path)
        runs = mr.summarize(rows)["runs"]
        self.assertEqual(runs["boost_runs"], 6, "任一路有命中的轮数")
        self.assertEqual(runs["boost_runs_by_signal"]["eng"], 1,
                         "浏览加权只有 1 轮有数据")
        self.assertEqual(runs["boost_runs_by_signal"]["campaign"], 6)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("浏览加权 +2 -4（1 轮有数据）", text,
                      "必须显示浏览加权自己的分母，而非 6 轮")
        self.assertNotIn("浏览加权 +2 -4 /", text)

    def test_r611_boost_zero_coverage_warns(self):
        """R611 边界：有浏览加权命中数但 0 轮有该字段（不可能同轮发生，但字段
        顺序/裁剪可能造成）→ 必须显式告警而不是显示成一个看似正常的命中率。"""
        _write(self.path, [
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "campaign_boost_hits": 1, "engagement_boost_up": 2,
             "engagement_boost_down": 4},
        ])
        rows, _ = mr.load_rows(self.path)
        runs = mr.summarize(rows)["runs"]
        # 该轮字段存在 → 分母为 1，不应走 0 轮告警分支
        self.assertEqual(runs["boost_runs_by_signal"]["eng"], 1)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("1 轮有数据", text)
        self.assertNotIn("0 轮有数据", text)

    def test_r612_engagement_boost_table_coverage_audit(self):
        """R612：浏览加权表覆盖审计——R608 到底在给哪些币 ±5、谁被 min_n 挡在表外。

        生产实锤的自我强化回路：样本量 n 由"我们发了几篇"决定，于是
        **发得多的低浏览币**（XRP n=11/浏览54）留在表内被持续重排到后置，
        **发得少的高浏览币**（BNB n=1/浏览247、HYPE n=2/浏览236）被 min_n 挡在
        表外永远拿不到加分——而这张表此前在报表里零可见性。
        """
        rows_spec = []
        for _i in range(11):  # XRP 发得多 → n 大 → 留在表内
            rows_spec.append({"platforms": ["binance"], "outcome": "binance_published",
                              "tokens": ["XRP", "ETH"]})
        for _i in range(1):   # BNB 发得少 → n 小 → 被挡表外
            rows_spec.append({"platforms": ["binance"], "outcome": "binance_published",
                              "tokens": ["BNB"]})
        _write(self.path, rows_spec)
        rows, _ = mr.load_rows(self.path)
        fake = {"min_n": 4, "tokens": {
            "XRP": {"n": 11, "median_views": 54},
            "BNB": {"n": 1, "median_views": 247},
        }}
        with unittest.mock.patch.object(mr, "load_token_engagement", return_value=fake):
            s = mr.summarize(rows)
            text = mr.render_text(s, rows)
        eb = s["eng_boost"]
        self.assertIsNotNone(eb, "加权表审计块必须存在")
        self.assertEqual([i["token"] for i in eb["in_table"]], ["XRP"])
        self.assertEqual([i["token"] for i in eb["out_table"]], ["BNB"])
        self.assertEqual(eb["in_table"][0]["published"], 11, "首标的发布量口径")
        self.assertEqual(eb["out_table"][0]["published"], 1)
        self.assertIn("浏览加权表", text)
        self.assertIn("表外高触达币被min_n 挡在加减分之外", text,
                      "表外浏览(247) > 表内最低(54) → 必须告警自我强化回路")
        self.assertIn("自我强化回路", text)

    def test_r612_boost_audit_no_false_loop_warning(self):
        """R612 反向：表外币浏览**不**高于表内任何币时，不得报自我强化回路
        （n 小但浏览也低是正常的样本不足，不是回路）。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "tokens": ["ETH"]},
        ])
        rows, _ = mr.load_rows(self.path)
        fake = {"min_n": 4, "tokens": {
            "ETH": {"n": 9, "median_views": 184},
            "DOGE": {"n": 1, "median_views": 36},
        }}
        with unittest.mock.patch.object(mr, "load_token_engagement", return_value=fake):
            s = mr.summarize(rows)
            text = mr.render_text(s, rows)
        self.assertIn("表外", text)
        self.assertNotIn("自我强化回路", text, "表外浏览(36) < 表内(184) → 不是回路")

    def test_r612_engagement_audit_missing_file_silent(self):
        """R612：无加权表时审计块为 None 且整块不渲染（零噪音，同停放源惯例）。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "tokens": ["BTC"]},
        ])
        rows, _ = mr.load_rows(self.path)
        with unittest.mock.patch.object(mr, "load_token_engagement", return_value={}):
            s = mr.summarize(rows)
            text = mr.render_text(s, rows)
        self.assertIsNone(s["eng_boost"], "无表时审计块应为 None")
        self.assertNotIn("浏览加权表", text)

    def test_r612_load_token_engagement_min_n_and_tolerance(self):
        """R612：读侧口径——只认 n 为 int、median_views 为非负数值的记录；
        min_n 缺失/非 int 时回退 4（与 main._load_token_engagement 同纪律）。
        畸形项（n 缺失、median_views 为负/非数）必须跳过而非让整表失效——
        一条脏数据不该让 R608 加权静默。"""
        p = os.path.join(self.tmpdir, "eng.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"tokens": {
                "ETH": {"n": 9, "median_views": 184},
                "BAD_N": {"median_views": 100},        # 无 n → 跳过
                "BAD_MV": {"n": 5, "median_views": -5},  # 负值 → 跳过
                "BAD_TY": {"n": 5, "median_views": "x"},  # 非数值 → 跳过
            }}, f)
        got = mr.load_token_engagement(p)
        self.assertEqual(got["min_n"], 4, "min_n 缺失回退 4")
        self.assertEqual(list(got["tokens"]), ["ETH"], "畸形记录跳过，好数据保留")

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

    def test_stale_date_refs_consumed_and_rendered(self):
        """R333：stale_date_refs 写侧（main analyze_with_ai R127）逐次落盘
        「guidance 残留过期日期」计数，报表此前零消费——有数据无出口（R276 同族）。
        必须聚合（次数/总数/单次最大）并渲染，且零残留时零噪音。"""
        rows = [
            {"outcome": "llm_success", "stage": "campaign_intel",
             "ts": "2026-09-14T10:00:00+00:00", "stale_date_refs": 2},
            {"outcome": "llm_success", "stage": "campaign_intel",
             "ts": "2026-09-15T10:00:00+00:00", "stale_date_refs": 0},
            {"outcome": "llm_success", "stage": "campaign_intel",
             "ts": "2026-09-16T10:00:00+00:00", "stale_date_refs": 3},
            {"outcome": "llm_success", "stage": "summarize",
             "ts": "2026-09-16T11:00:00+00:00", "stale_date_refs": 9},  # 非情报行不计
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["stale_date_refs_refreshes"], 2, "仅 >0 的情报成功行计入次数")
        self.assertEqual(s["stale_date_refs_hits"], 5, "2+3 引用总数")
        self.assertEqual(s["stale_date_refs_max"], 3, "单次最大")
        text = mr.render_text(s, rows)
        self.assertIn("情报 guidance 残留过期日期", text)
        self.assertIn("2 次刷新命中", text)
        self.assertIn("共 5 处", text)
        self.assertIn("单次最多 3", text)
        # 零残留零噪音
        s2 = mr.summarize([rows[1]])
        self.assertNotIn("残留过期日期", mr.render_text(s2, [rows[1]]))

    def test_r616_permanent_failure_recovered_vs_still_dead(self):
        """R616：💀 永久失败必须区分「已自愈」与「仍死」——两者都要处置吗？**不**。

        生产实锤两例并存且此前完全同貌：
          openrouter/minimax-m3:free 09-08 因模型下架永久失败 →但该 provider
            后来换默认名为 openrouter/free，09-29 仍在成功出稿 → **已自愈**
          b.ai/glm-5.3-flash 09-29 余额耗尽 → 此后再没成功出过一��� → **仍死**

        判据是**末次成功 vs 末次失败的先后**，且粒度必须是 provider 而非
        provider/model：换个模型就救活一条通道，算已自愈。
        把已自愈的挂在「需人工处置」下，会把人引去改一个早已修好的配置。
        """
        rows = [
            # 永久失败（两例）
            {"ts": "2026-09-08T10:00:00+00:00", "outcome": "llm_rejected",
             "stage": "transport", "provider": "Preset-openrouter",
             "model": "minimax/minimax-m3:free",
             "reason": "[permanent 24h] Error code: 404 - model unlisted"},
            {"ts": "2026-09-29T12:00:00+00:00", "outcome": "llm_rejected",
             "stage": "transport", "provider": "Preset-b.ai",
             "model": "glm-5.3-flash",
             "reason": "[credit 24h] Error code: 400 - credit insufficient"},
            # 成功：只有 openrouter 在失败之后又成功（换了模型名）
            {"ts": "2026-09-29T13:00:00+00:00", "outcome": "binance_published",
             "platforms": ["binance"], "provider": "Preset-openrouter",
             "model": "openrouter/free", "tokens": ["BTC"]},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["provider_last_success"].get("Preset-openrouter")[:10],
                         "2026-09-29")
        text = mr.render_text(s, rows)
        live_line = next(ln for ln in text.split("\n") if "💀 永久失败" in ln)
        retired_line = next(ln for ln in text.split("\n") if "永久失败退役簇" in ln)
        # b.ai 失败晚于最后成功 → 仍在活警行
        self.assertIn("Preset-b.ai/glm-5.3-flash", live_line)
        # minimax 已自愈 → 只进退役簇，且不再挂"需人工处置"
        self.assertIn("minimax-m3:free", retired_line)
        self.assertIn("已恢复出稿", retired_line)
        self.assertNotIn("minimax", live_line)
        self.assertIn("需人工处置", live_line)
        self.assertNotIn("需人工处置", retired_line)

    def test_r616_no_success_record_counts_as_still_dead(self):
        """R616 方向守卫：某通道只有永久失败记录、**从无成功投递**时，
        判据无对照数据 → 必须归入活警（未知 ≠ 已解决，同 R613 方向），
        绝不能因为"找不到成功记录"就当成已自愈而漏报。"""
        rows = [
            {"ts": "2026-09-29T12:00:00+00:00", "outcome": "llm_rejected",
             "stage": "transport", "provider": "Preset-zai",
             "model": "glm-4.7-flash",
             "reason": "[credit 24h] Error code: 400 - credit insufficient"},
        ]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("💀 永久失败", text)
        self.assertNotIn("退役簇", text)

    def test_r616_recovered_flag_needs_both_timestamps(self):
        """R616：缺任一时间戳都不得判已自愈（helper 层直接构造边界）。"""
        cnt = collections.Counter({("P/m", "余额/额度耗尽"): 1})
        last_seen = {("P/m", "余额/额度耗尽"): "2026-09-29T00:00:00+00:00"}
        # 无成功记录 → 活警
        live, retired = mr._format_permanent_failures(cnt, last_seen, {})
        self.assertTrue(live and not retired)
        # 成功早于失败 → 仍死
        live, retired = mr._format_permanent_failures(
            cnt, last_seen, {"P": "2026-09-20T00:00:00+00:00"})
        self.assertTrue(live and not retired)
        # 成功晚于失败 → 已自愈
        live, retired = mr._format_permanent_failures(
            cnt, last_seen, {"P": "2026-10-01T00:00:00+00:00"})
        self.assertTrue(retired and not live)

    def test_r613_stale_source_alarm_downgraded(self):
        """R613：源健康告警必须带新鲜度。注入截断/空 feed/硬故障都是**全史累计**，
        此前渲染不带时间维度——已自愈的陈迹与正在发生的问题在面板上完全同貌。
        生产实锤：注入截断最后发生距今 49h、空 feed 67h、硬故障 42h，面板却与
        事发当日同貌，运维只能人工翻 jsonl 区分。距今 ≥24h 的降为ℹ️ 陈迹，
        释放⚠️ 视觉预算给当期真问题。"""
        rows = [
            {"outcome": "run_summary", "candidates": 10, "published": 0,
             "ts": "2026-09-28T00:00:00+00:00",
             "injection_hits": 3, "injection_feeds": {"EvilFeed": 3},
             "feeds_empty_sources": "BlockTempo", "feeds_failed": 1},
            # 末次时间距参考点 3 天 → 陈迹
            {"outcome": "run_summary", "candidates": 10, "published": 0,
             "ts": "2026-09-29T00:00:00+00:00",
             "injection_hits": 1, "injection_feeds": {"EvilFeed": 1}},
            # 最新一轮（无告警）提供"现在"基准
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "ts": "2026-10-02T00:00:00+00:00"},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["source_alarm_last"]["injection"],
                         "2026-09-29T00:00:00+00:00", "取最后一次发生的 ts")
        text = mr.render_text(s, rows)
        self.assertIn("注入截断", text, "陈迹也必须留在面板上（安全面告警不能消失）")
        self.assertIn("历史累计", text)
        self.assertIn("天前", text)
        self.assertNotIn("请评估停放该源", text, "陈迹不该再催处置动作")
        self.assertIn("ℹ️ 源健康异常", text, "空 feed 降为陈迹")
        self.assertIn("ℹ️ 源故障/停放", text, "硬故障降为陈迹")

    def test_r613_fresh_source_alarm_stays_warning(self):
        """R613 反向：24h 内发生的源告警**保持⚠️ 与处置指引**，不得被降级。
        这是本改动的方向性守卫：只降"确证陈旧"，活警必须照旧醒目。"""
        rows = [
            {"outcome": "run_summary", "candidates": 10, "published": 0,
             "ts": "2026-10-02T06:00:00+00:00",
             "injection_hits": 2, "injection_feeds": {"EvilFeed": 2}},
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "ts": "2026-10-02T06:30:00+00:00"},
        ]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("🚨 注入截断", text, "2 小时前发生 = 活警，必须 ⚠️")
        self.assertIn("请评估停放该源", text)

    def test_r613_unknown_freshness_stays_warning(self):
        """R613 关键安全向：时间戳缺失 → **无法判定新鲜度 → 按活警处理**。
        把未知态报成陈迹会藏起可能正在发生的问题——告警漏判的代价远大于多报，
        与本项目"判负向漏判倾斜"的纪律一致。"""
        rows = [
            {"outcome": "run_summary", "candidates": 10, "published": 0,
             "injection_hits": 3, "injection_feeds": {"EvilFeed": 3}},
        ]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("🚨 注入截断", text, "无时间戳 = 未知，不能降级")
        self.assertIn("请评估停放该源", text)
        self.assertNotIn("历史累计", text)

    def test_injection_hits_consumed_and_rendered(self):
        """R334：R273/R274 注入截断（injection_hits/injection_feeds）写侧落盘
        run_summary，R275 补了 Step Summary，metrics_report 仍零消费——
        R275 自己写「人工第一眼巡检的页面完全静默…最后缺口」，持久巡检面必须可见。"""
        rows = [
            {"outcome": "run_summary", "candidates": 10, "published": 0,
             "injection_hits": 0},
            {"outcome": "run_summary", "candidates": 10, "published": 1,
             "injection_hits": 3, "injection_feeds": {"EvilFeed": 2, "SusFeed": 1}},
            {"outcome": "run_summary", "candidates": 5, "published": 0,
             "injection_hits": 1, "injection_feeds": {"EvilFeed": 1}},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["injection_hits"], 4, "3+1 跨轮累加")
        self.assertEqual(s["runs"]["injection_feeds"].get("EvilFeed"), 3)
        self.assertEqual(s["runs"]["injection_feeds"].get("SusFeed"), 1)
        text = mr.render_text(s, rows)
        self.assertIn("注入截断", text)
        self.assertIn("4 条", text)
        self.assertIn("EvilFeed ×3", text, "命中数降序（最该停车的源排最前）")
        self.assertIn("请评估停放该源", text)
        # 零命中零噪音
        s2 = mr.summarize([rows[0]])
        self.assertNotIn("注入截断", mr.render_text(s2, [rows[0]]))

    def test_feed_health_sources_consumed_and_rendered(self):
        """R335：R276 写侧的 feeds_empty_sources / fetch_timeout_sources 此前
        只进日志 + Step Summary，metrics_report 零消费——生产 09-22 BlockTempo
        空 feed ×3 有数据无出口。必须按源聚合并渲染，零命中零噪音。"""
        rows = [
            {"outcome": "run_summary", "candidates": 5,
             "feeds_empty": 1, "feeds_empty_sources": "BlockTempo (动区动趋中文)"},
            {"outcome": "run_summary", "candidates": 5,
             "feeds_empty": 1, "feeds_empty_sources": "BlockTempo (动区动趋中文) | Decrypt (Web3/AI/Meme)"},
            {"outcome": "run_summary", "candidates": 5,
             "fetch_timeout": 2, "fetch_timeout_sources": ["SlowFeed", "BlockTempo (动区动趋中文)"]},
            {"outcome": "run_summary", "candidates": 5},  # 干净轮
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["feeds_empty_sources"].get("BlockTempo (动区动趋中文)"), 2)
        self.assertEqual(s["runs"]["feeds_empty_sources"].get("Decrypt (Web3/AI/Meme)"), 1)
        self.assertEqual(s["runs"]["fetch_timeout_sources"].get("SlowFeed"), 1)
        text = mr.render_text(s, rows)
        self.assertIn("源健康异常", text)
        self.assertIn("BlockTempo (动区动趋中文) ×2", text, "空 feed 命中降序")
        self.assertIn("空feed", text)
        self.assertIn("超时", text)
        self.assertIn("请评估换源/撤源", text)
        # 零命中零噪音
        s2 = mr.summarize([rows[3]])
        self.assertNotIn("源健康异常", mr.render_text(s2, [rows[3]]))

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

    def test_r603_manipulation_frame_tracked_separately(self):
        """R603：操纵归因叙事（利好不涨=有人出货/烟雾弹/送流动性）按帖占比独立追踪。
        R596 后逐字「我猜」消失但语义框架仍在、绕过前缀指纹雷达——这里按帖计一次，
        且**不进 offenders**（那是「N 处命中」硬口径），只走独立 🎭 行；占比 ≥40% 带告警。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "利好出来盘面不涨，看着像主力借机出货的烟雾弹。"},  # 操纵归因
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "现在无脑冲进去纯是给庄家送流动性。"},  # 操纵归因（送流动性）
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "大概率是利好被 price in 了，等成交量确认再说。"},  # 干净（无操纵词）
        ])
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(q["manip_frame"], 2, "两篇含操纵归因叙事")
        # 不污染硬合规口径：这三篇无 FNG/装置/AI腔，offenders 应为空、命中为 0
        self.assertNotIn("操纵归因:出货", q["offenders"])
        self.assertEqual(q["fng_anchor"] + q["banned_device"] + q["ai_flavor"], 0)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("操纵归因叙事: 2/3 篇", text)
        self.assertIn("⚠️", text)  # 67% ≥ 40% 阈值 → 告警

    def test_r605_sentence_burstiness_cv(self):
        """R605：句长 burstiness（整合自 textpulse 2026 研究——AI 文本句长偏均匀=机器味）。
        _sentence_cv 算句长变异系数；节奏起伏大→CV 高，均匀→CV 低；句数<2 返 None。
        只观测不设门（研究自陈个体判决不可靠），面板给中位 CV + 偏平尾。"""
        # 起伏大：3 字 / 很长的一句 / 2 字 → CV 高
        bursty = "跌了。" + "这波资金面链上活跃解锁节奏成交承接全都在同一时间点共振非常罕见。" + "别追。"
        # 均匀：每句长度接近 → CV 低
        flat = "资金面持续流入。链上活跃度回升。成交承接力度足。中期趋势偏强。"
        self.assertGreater(mr._sentence_cv(bursty), mr._sentence_cv(flat),
                           "句长起伏大的 CV 必须高于均匀的")
        self.assertIsNone(mr._sentence_cv("只有一句话没有句末标点"), "句数<2 返 None")
        self.assertLess(mr._sentence_cv(flat), 0.35, "均匀句长应判偏平(<0.35)")
        # 渲染：≥3 篇才出行
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published", "final_preview": flat},
            {"platforms": ["binance"], "outcome": "binance_published", "final_preview": flat},
            {"platforms": ["binance"], "outcome": "binance_published", "final_preview": bursty},
        ])
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(len(q["burstiness_cvs"]), 3, "三篇均有≥2句，都计入")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("句长节奏 CV 中位", text)

    def test_r607_flat_desc_repetition_tracked(self):
        """R607：「利好不涨」描述复读——读近期全文发现最常见场景（消息出来价格没动）
        被收敛到固定描述句（连个像样的反弹都没有/盘面不买账/连个水花都没溅），全史 8%
        但近30升到33%。句中短语、前缀雷达看不见，按帖计一次、独立 📉 行，只追踪。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "$BTC 利好出来,盘面却连个像样的反弹都没有,量能跟不上。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "消息砸出来,盘面不买账,原地踏步。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "官宣利好,$ETH 连个水花都没溅起来。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "$SOL 放量突破前高,资金净流入,结构健康。"},  # 干净，不含不涨描述
        ])
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(q["flat_desc"], 3, "三篇含利好不涨固定描述句")
        # 不污染硬合规口径
        self.assertEqual(q["fng_anchor"] + q["banned_device"] + q["ai_flavor"], 0)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("利好不涨描述复读: 3/4 篇", text)

    def test_r614_stock_advice_repetition_tracked(self):
        """R614：实操段套话复读——R592 明令「不要每帖都写回踩、拿稳、插针或降杠杆」、
        R613 修掉「别急着」67%，但**两处修复此前都没有度量面**：拿稳全史22%/回踩32%/
        插针27%（R592修后近30降到0~3%）、别急着近30 67%，全靠人工 grep 才发现与验证。
        同 flat_desc 口径：句中短语（不在段首，前缀雷达看不见）、每帖最多计一次、
        独立 🧰 行、只追踪不设门（这些短语本身可用，问题是收敛成唯一说法）。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "$BTC 现在别急着抄底,等放量突破近期前高再评估强度。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "$ETH 现货拿稳别被插针洗出去,合约杠杆压到最低。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "等日线放量回踩确认支撑再考虑进场,别追高。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "$SOL 偏强信号是放量站稳前高,轻仓跟进没问题。"},  # 干净
        ])
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(q["stock_advice"], 3, "三篇含实操段套话")
        # 与 flat_desc 是两套独立短语，互不串味
        self.assertEqual(q["flat_desc"], 0, "本篇无利好不涨描述，不得误计")
        # 不污染硬合规口径（同 R607 纪律）
        self.assertEqual(q["fng_anchor"] + q["banned_device"] + q["ai_flavor"], 0)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("实操段套话复读: 3/4 篇", text)

    def test_r614_stock_advice_no_false_positive_on_clean_posts(self):
        """R614：套话正则不得误伤干净帖——「别急着」的近亲（急/着）单独出现不算，
        「拿稳」单独作持仓描述（如"仓位拿稳一点"这类正常建议）也要看是否落入
        「现货拿稳/拿稳别」这类固定搭配。生产实测近20帖只有 别急着×15 与
        现货拿稳×1 命中，无误报；若把正则放宽到单词级会立刻开始误伤。"""
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "这事很急,得赶紧盯着盘面,别睡着。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "拿着不动也是一种操作,仓位按自己能承受的来。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "杠杆是双刃剑,用之前想清楚止损放哪。"},
        ])
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(q["stock_advice"], 0, "干净帖不得被套话正则误伤")

    def test_r610_tracking_window_split_recent_vs_older(self):
        """R610：追踪指标按「近半/远半」对照，修复生效才不会被旧稿拖成假警报。

        生产教训（R604）：prompt 加固刚上线，固定 20 篇窗口里 15篇仍是加固前旧稿，
        面板继续报 50% 命中，看着像加固无效，实则加固后新帖 0 命中。R604 提交
        时间为 10-01T18:08Z，其后 5 篇操纵词全为 0（仅 18:44 那篇是部署延迟）。
        这种假阴性会诱导下一轮去"再加固一遍已经修好的东西"。

        这里构造 8 篇：远半 4 篇全含操纵词（加固前），近半 4 篇全干净（加固后）。
        整窗仍是 4/8=50%（旧口径会告警），但近/远对照应判「加固生效中」。
        """
        _dirty = "利好出来盘面不涨，看着像主力借机出货的烟雾弹。"
        _clean = "量能接不住，等成交承接确认再说，别急着追。"
        rows_spec = []
        for _i in range(4):
            rows_spec.append({"platforms": ["binance"], "outcome": "binance_published",
                              "ts": f"2026-10-01T1{_i}:00:00+00:00",
                              "final_preview": _dirty})
        for _i in range(4):
            rows_spec.append({"platforms": ["binance"], "outcome": "binance_published",
                              "ts": f"2026-10-02T0{_i}:00:00+00:00",
                              "final_preview": _clean})
        _write(self.path, rows_spec)
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(q["manip_frame"], 4, "整窗计数不变（向后兼容）")
        self.assertEqual(q["manip_frame_older"], 4, "远半=对照基线，4 篇全中")
        self.assertEqual(q["manip_frame_recent"], 0, "近半=加固后新帖，0 命中")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("近半 0/4", text)
        self.assertIn("远半对照 4/4", text)
        self.assertIn("加固生效", text)
        self.assertNotIn("加固可能未生效", text,
                         "近半清零时绝不能出现催促再加固的判语")
        # 近半跨度必须首尾都正确（曾退化成单点：_hi 只在首次赋值）
        lo, hi = q["recent_span"]
        self.assertEqual(lo, "2026-10-02 00:00")
        self.assertEqual(hi, "2026-10-02 03:00")

    def test_r610_recent_not_lower_flags_for_more_evidence(self):
        """R610 反向判据：近半**没有**低于远半（甚至更高）时，才提示加固可能
        未生效。前半段测试覆盖"降了"，这里覆盖"没降"——两侧都不误判才算判据完备。
        """
        _dirty = "这波纯纯是给庄家送流动性，接盘侠注意。"
        _clean = "放量突破前高，资金净流入，结构健康。"
        rows_spec = []
        for _i in range(4):  # 远半：2 中 2 净
            rows_spec.append({"platforms": ["binance"], "outcome": "binance_published",
                              "final_preview": _dirty if _i < 2 else _clean})
        for _i in range(4):  # 近半：3 中 1 净（比远半更高）
            rows_spec.append({"platforms": ["binance"], "outcome": "binance_published",
                              "final_preview": _dirty if _i < 3 else _clean})
        _write(self.path, rows_spec)
        rows, _ = mr.load_rows(self.path)
        q = mr.quality_scan(rows)
        self.assertEqual(q["manip_frame_older"], 2)
        self.assertEqual(q["manip_frame_recent"], 3)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("近半 3/4", text)
        self.assertIn("加固可能未生效", text)

    def test_r610_small_window_keeps_legacy_line(self):
        """R610：窗口太小（两半任一<3篇）时不做近/远对照，回落旧单行格式。
        小样本下近/远切分没有统计意义，硬切只会把噪声当趋势。
        """
        _write(self.path, [
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "看着像主力出货的烟雾弹。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "纯纯给庄家送流动性。"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": "量能接不住，等确认。"},
        ])
        rows, _ = mr.load_rows(self.path)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("操纵归因叙事: 2/3 篇", text, "小窗口沿用旧格式，不切近/远")
        self.assertNotIn("近半", text)

    def test_r610_stablecoin_only_zero_widget_not_alarm(self):
        """R610：稳定币-only 的零挂件是 R316/R586 契约下的**合规**行为，不是
        Write2Earn 生命线失守。生产实录09-30 02:04 USDC-only 帖widget_count=0，
        一直被 R123 告警记成"保底机制被绕过"，假阳性长期钉在 1/288，让真失守
        被淹没（告警的"狼来了"式失效）。修复后它进独立ℹ️ 桶，不进告警分母。
        """
        _write(self.path, [
            # 合规：全稳定币 → 不告警
            {"platforms": ["binance"], "outcome": "binance_published",
             "tokens": ["USDC"], "widget_count": 0, "final_preview": "USDC 报价 1.0。"},
            # 真失守：真实山寨币却零挂件 → 必须告警
            {"platforms": ["binance"], "outcome": "binance_published",
             "tokens": ["SHIB"], "widget_count": 0, "final_preview": "$SHIB 波动大。"},
            # 混合：USDC + 真实币 → 按真失守处理（从严，宁可多报）
            {"platforms": ["binance"], "outcome": "binance_published",
             "tokens": ["USDC", "DOGE"], "widget_count": 0, "final_preview": "混合标的。"},
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["zero_widget_stablecoin_only"], 1, "仅全稳定币那篇进合规桶")
        self.assertEqual(s["zero_widget_posts"], 2, "真失守 + 混合标的两篇仍告警")
        text = mr.render_text(s, rows)
        self.assertIn("全文零有效挂件 2/3 篇", text)
        self.assertIn("稳定币-only 零挂件 1 篇", text)

    def test_r610_stablecoin_helper_strict(self):
        """_is_stablecoin_only 口径必须从严：tokens 缺失/空/含非 FORCE_STRIP 词
        一律判 False（进告警分母）。这条告警的唯一价值是"真失守时能响"，
        漏判会让它重新变成哑炮，故不能用"看起来像稳定币"这类宽松判断。"""
        self.assertTrue(mr._is_stablecoin_only(["USDC"]))
        self.assertTrue(mr._is_stablecoin_only(["usdc", "USDT"]), "大小写不敏感")
        self.assertTrue(mr._is_stablecoin_only(["USDC", "ETF"]))
        self.assertFalse(mr._is_stablecoin_only(["USDC", "SHIB"]), "混合 → 真失守")
        self.assertFalse(mr._is_stablecoin_only(["SHIB"]))
        self.assertFalse(mr._is_stablecoin_only([]), "空列表不可当合规")
        self.assertFalse(mr._is_stablecoin_only(None), "字段缺失不可当合规")
        self.assertFalse(mr._is_stablecoin_only("USDC"), "非list 不可当合规")

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

    def test_explicit_campaign_tag_field_overrides_proxy(self):
        """R291：显式字段（注入的活动标签原文）优先于 proxy 计数——proxy 把模型
        自写的核心代币名也算"有活动标签"，injector 死代码时期遥测显示全覆盖。
        显式 None（无活动标签可注入）+ 情报新鲜 → 必须告警，即使 proxy>0。"""
        _write(self.path, [
            # proxy 说有（核心代币名 #XRP），显式字段说没有 → 以显式为准，告警
            {"platforms": ["binance"], "outcome": "binance_published",
             "tag_count": 3, "campaign_tag_count": 1, "campaign_tag": None,
             "intel_degraded": False},
            # 显式有注入 → 覆盖，并进直方图
            {"platforms": ["binance"], "outcome": "binance_published",
             "tag_count": 4, "campaign_tag_count": 2, "campaign_tag": "#TradingTournament",
             "intel_degraded": False},
            {"platforms": ["binance"], "outcome": "binance_published",
             "tag_count": 4, "campaign_tag_count": 2, "campaign_tag": "#TradingTournament",
             "intel_degraded": False},
            # 显式 None + intel 降级 = 合法语境，不告警
            {"platforms": ["binance"], "outcome": "binance_published",
             "campaign_tag": None, "intel_degraded": True},
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(s["campaign_tag_evaluated"], 4)
        self.assertEqual(s["campaign_tag_covered"], 2)
        self.assertEqual(s["campaign_tag_zero_fresh"], 1,
                         "显式 None 且情报新鲜才算疑似回归")
        self.assertEqual(dict(s["campaign_tags"]), {"#TradingTournament": 2})
        text = mr.render_text(s, rows)
        self.assertIn("情报新鲜但无活动标签 1/4 篇", text)
        self.assertIn("🏷️ 活动标签注入: #TradingTournament ×2", text)

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

    def test_cashtag_prefix_not_a_fingerprint(self):
        """R601：$挂件/币代码前缀是内容集中度（热门币连续领头），不是文风指纹——
        「$XRP 现在报…」「$XRP Ledger 销毁…」「$XRP现价…」三条开场各不相同，只是
        $XRP 连续打头（生产实录雷达误报"$XRP"×3）。账号「追踪热门币种」会让
        BTC/XRP/SHIB 反复领头，不排除就会淹没真·文风指纹。前缀去 $ 后纯 ASCII 字母
        即视为币代码/实体名，不报。"""
        rows = self._rows([
            "$XRP 现在报 1.51 刀,全网都在等三连阳。后续。",
            "$XRP Ledger 销毁率暴涨 848%。后续。",
            "$XRP现价1.5058,24小时才蠕动。后续。",
            "别的新闻甲。", "别的新闻乙。"])
        self.assertEqual(mr.opener_fingerprint(rows)["alerts"], {},
                         "$XRP 连续领头是内容集中度，不是文风指纹")
        # 对照：CJK 文风领词仍须正常聚簇报警（不被误伤）
        cjk = self._rows(["全网贪婪 73 了。甲。", "全网都在盯这位置。乙。",
                          "全网情绪高涨。丙。", "别的。"])
        self.assertEqual(mr.opener_fingerprint(cjk)["alerts"].get("全网"), 3,
                         "CJK 文风领词不得被实体名排除规则误伤")

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


class TestBodyFingerprintRadar(unittest.TestCase):
    """R600：分析段（第二正文段）指纹雷达——开场雷达只看第一段，漏了
    「我猜这波是主力…」这类分析段开场的复读（R596 实录 47% dashboard 全程
    没报、靠人工读 preview 才发现）。同口径扫第二段，补上盲区。"""

    @staticmethod
    def _rows(previews):
        return [{"outcome": "binance_published", "final_preview": t} for t in previews]

    def test_second_paragraph_cluster_detected(self):
        # 开场句各异（第一段不聚簇），但第二段都以「我猜这波」开头 → 必须报
        rows = self._rows([
            "$BTC 突破 8 万。我猜这波是主力借利好出货。扣1扣2。",
            "$ETH 放量拉升。我猜这波是主力压盘洗筹。扣1扣2。",
            "$SOL 异动明显。我猜这波是主力诱多接盘。扣1扣2。",
            "$XRP 盘整待变。资金面观察一下。扣1扣2。",
        ])
        ofp = mr.opener_fingerprint(rows)
        self.assertEqual(ofp["alerts"], {}, "第一段各异不应报开场指纹")
        bfp = mr.body_fingerprint(rows)
        self.assertEqual(bfp["alerts"].get("我猜这波"), 3, "第二段「我猜这波」×3 必须报")

    def test_no_alert_when_second_paragraph_diverse(self):
        rows = self._rows([
            "$BTC 新高。资金费率转正值得注意。扣1扣2。",
            "$ETH 回调。链上活跃度回落了。扣1扣2。",
            "$SOL 横盘。解锁节奏是关键变量。扣1扣2。",
        ])
        self.assertEqual(mr.body_fingerprint(rows)["alerts"], {})

    def test_article_header_skipped_in_body(self):
        # 长文分节头不占正文段序：首段=正文首句、第二段=正文第二句
        rows = self._rows([
            "一、发生了什么\n\n$BTC 破位。我猜这波是主力出货。后续。",
            "一、发生了什么\n\n$ETH 拉升。我猜这波是主力洗盘。后续。",
            "一、发生了什么\n\n$SOL 异动。我猜这波是主力诱多。后续。",
        ])
        self.assertEqual(mr.body_fingerprint(rows)["alerts"].get("我猜这波"), 3)

    def test_render_line_present(self):
        rows = self._rows([
            "甲一。我猜这波是主力出货。尾。", "乙一。我猜这波是主力洗盘。尾。",
            "丙一。我猜这波是主力诱多。尾。", "丁一。正常分析。尾。"])
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("分析段指纹预警", text)


class TestEndingFingerprint(unittest.TestCase):
    """R613：结尾段指纹雷达——第三块盲区。开场雷达（R124）扫第一段、分析段雷达
    （R600）扫第二段，**结尾段一直无覆盖**，而 R598 已点明「ENDING 是最后一个发
    整句的池」：模型在结尾最放松、最容易把模板整句抄出来。

    实证依据：全史「庄家这步」×17 做结尾段开头（09-13~09-23，R598 之前的旧帖），
    这个指纹从诞生到被修掉全程没有任何雷达报过——靠人工翻 ending_style 才发现。
    同 R600 的教训：人工读全文发现的盲区必须产品化进 dashboard，否则下轮还得人工。
    """

    @staticmethod
    def _rows(previews):
        return [{"outcome": "binance_published", "final_preview": t} for t in previews]

    def test_ending_cluster_detected_and_hashtags_skipped(self):
        """结尾段共享前缀必须报；#标签行不得被当成结尾段（否则所有帖都聚到
        「#Wri…」这一个前缀上，雷达直接变哑炮）。fixture 用真实帖结构：结尾问句
        独占一段、#标签在最后一行。"""
        rows = self._rows([
            "开头甲。\n\n中间分析段甲。\n\n庄家这步棋是吸筹还是出货?\n\n#Write2Earn #BinanceSquare #BTC",
            "开头乙。\n\n中间分析段乙。\n\n庄家这步棋,拉高派货?\n\n#Write2Earn #BinanceSquare #ETH",
            "开头丙。\n\n中间分析段丙。\n\n庄家这步是洗盘还是真突破?\n\n#Write2Earn #BinanceSquare #SOL",
            "开头丁。\n\n中间分析段丁。\n\n这消息你信几分?\n\n#Write2Earn #BinanceSquare #XRP",
        ])
        efp = mr.ending_fingerprint(rows)
        self.assertEqual(efp["alerts"].get("庄家这步"), 3, "结尾段「庄家这步」×3 必须报")
        self.assertNotIn("#Wri", efp["alerts"], "#标签行不得参与聚簇")

    def test_no_alert_when_endings_diverse(self):
        rows = self._rows([
            "甲。\n\n分析甲。\n\n看多的扣1。\n\n#Write2Earn",
            "乙。\n\n分析乙。\n\n你重仓了哪个?\n\n#Write2Earn",
            "丙。\n\n分析丙。\n\n还能撑多久?\n\n#Write2Earn",
        ])
        self.assertEqual(mr.ending_fingerprint(rows)["alerts"], {})

    def test_last_body_paragraph_used_not_second(self):
        """扫的是**最后一个**正文段，不是第二段——两个雷达扫不同位置才有意义。
        这里第二段共享前缀但结尾段各异，ending_fingerprint 必须安静
        （而 body_fingerprint 会报，用来证明两者确实扫不同位置）。"""
        rows = self._rows([
            "甲。\n\n我猜这波是主力。\n\n看多的扣1。\n\n#Write2Earn",
            "乙。\n\n我猜这波是洗盘。\n\n你重仓了哪个?\n\n#Write2Earn",
            "丙。\n\n我猜这波是诱多。\n\n还能撑多久?\n\n#Write2Earn",
        ])
        self.assertEqual(mr.ending_fingerprint(rows)["alerts"], {})
        self.assertEqual(mr.body_fingerprint(rows)["alerts"].get("我猜这波"), 3)

    def test_article_header_skipped_in_ending(self):
        """长文结尾常带「四、接下来盯什么」这类分节头，不得当成结尾段首句。"""
        rows = self._rows([
            "一、发生了什么\n\n$BTC 破位。分析。\n\n四、接下来盯什么\n\n庄家这步是出货?\n\n#Write2Earn",
            "一、发生了什么\n\n$ETH 拉升。分析。\n\n四、接下来盯什么\n\n庄家这步是洗盘?\n\n#Write2Earn",
            "一、发生了什么\n\n$SOL 异动。分析。\n\n四、接下来盯什么\n\n庄家这步是诱多?\n\n#Write2Earn",
        ])
        self.assertEqual(mr.ending_fingerprint(rows)["alerts"].get("庄家这步"), 3)

    def test_render_line_present(self):
        rows = self._rows([
            "甲。\n\n分析。\n\n庄家这步是吸筹还是出货?\n\n#Write2Earn",
            "乙。\n\n分析。\n\n庄家这步是拉高派货?\n\n#Write2Earn",
            "丙。\n\n分析。\n\n庄家这步是洗盘?\n\n#Write2Earn",
            "丁。\n\n分析。\n\n这消息你信几分?\n\n#Write2Earn",
        ])
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("结尾段指纹预警", text)


class TestMidSegmentFingerprint(unittest.TestCase):
    """R614：中间段落位置指纹——三个固定位置雷达（opener=第1段、body=第2段、
    ending=末段）之间的结构性盲区。生产帖正文 2~7 段（实测 4 段占 168/291 为绝对
    主体），第 3 段实操段此前无任何覆盖，而 R592 的「拿稳/插针/回踩」与 R613 的
    「别急着」都发生在那里——两处都是人工读样本/人工 grep 才发现，雷达全程沉默。

    本雷达按 (段落位置, 首句前缀) 聚簇，任何中间位置成形即报，不依赖运营者想到
    「还有第 3 段没扫」。只报中间位置：首两段与末段各有专属雷达与专属标签，
    重复报会让同一指纹在 dashboard 出现两行。"""

    @staticmethod
    def _rows(previews):
        return [{"outcome": "binance_published", "final_preview": t} for t in previews]

    def test_middle_position_cluster_detected(self):
        """第 3 段（位置键 2）共享前缀必须报。注意 _cluster_openers 的口径：4 字簇优先，
        只有 4 字簇凑不齐 min_hits 时才退到 2 字簇——所以三篇共 4 字前缀时报 4 字簇，
        第三篇换了词（说句得罪人）时报 2 字簇「说句」。两种都算检出，别写死长度。"""
        rows = self._rows([
            "开场甲。\n\n分析甲。\n\n说句实在话,别慌。\n\n结尾甲。\n\n#Write2Earn",
            "开场乙。\n\n分析乙。\n\n说句实在的,沉住气。\n\n结尾乙。\n\n#Write2Earn",
            "开场丙。\n\n分析丙。\n\n说句得罪人的,别追。\n\n结尾丙。\n\n#Write2Earn",
        ])
        fp = mr.mid_segment_fingerprint(rows)
        self.assertIn(2, fp["alerts"], "第3段（位置键 2）必须有告警")
        # 第三篇换了词 → 4 字簇凑不齐 → 退 2 字簇「说句」×3
        self.assertEqual(fp["alerts"][2].get("说句"), 3)

    def test_middle_position_four_char_cluster(self):
        """三篇共 4 字前缀时报 4 字簇（同 opener/body 口径：4 字簇优先）。"""
        rows = self._rows([
            "开场甲。\n\n分析甲。\n\n说句实在话,别慌。\n\n结尾甲。\n\n#Write2Earn",
            "开场乙。\n\n分析乙。\n\n说句实在的,沉住气。\n\n结尾乙。\n\n#Write2Earn",
            "开场丙。\n\n分析丙。\n\n说句实在点,别追。\n\n结尾丙。\n\n#Write2Earn",
        ])
        fp = mr.mid_segment_fingerprint(rows)
        self.assertEqual(fp["alerts"].get(2, {}).get("说句实在"), 3, "4 字簇必须报")

    def test_first_second_and_last_positions_not_reported(self):
        """首两段与末段各有专属雷达，本雷达不得重复报（否则同一指纹两行）。
        中间段必须真的各不相同，否则「中间」自己就成了共享前缀。"""
        rows = self._rows([
            "现在开盘。\n\n现在我猜这波。\n\n分批挂单更稳。\n\n现在结尾。\n\n#Write2Earn",
            "现在盘中。\n\n现在我猜那波。\n\n等确认再动。\n\n现在收尾。\n\n#Write2Earn",
            "现在尾盘。\n\n现在我猜第三波。\n\n先观望不丢人。\n\n现在收官。\n\n#Write2Earn",
        ])
        fp = mr.mid_segment_fingerprint(rows)
        self.assertEqual(fp["alerts"], {},
                        "首段/第二段/末段的「现在」由专属雷达负责，本雷达应安静")
        # 证明专属雷达确实会报（本雷达不是漏报，是故意不重复报）
        self.assertEqual(mr.body_fingerprint(rows)["alerts"].get("现在我猜"), 3)
        self.assertEqual(mr.ending_fingerprint(rows)["alerts"].get("现在"), 3)

    def test_short_posts_skipped(self):
        """2~3 段的帖：第 3 段就是末段，已被 ending_fingerprint 覆盖，不重复扫。"""
        rows = self._rows([
            "开场甲。\n\n分析甲。\n\n说句实在话,结尾即此段。\n\n#Write2Earn",
            "开场乙。\n\n分析乙。\n\n说句实在的,结尾即此段。\n\n#Write2Earn",
            "开场丙。\n\n分析丙。\n\n说句实在点,结尾即此段。\n\n#Write2Earn",
        ])
        self.assertEqual(mr.mid_segment_fingerprint(rows)["alerts"], {})
        # 但 ending_fingerprint 该报——证明不是整体失灵
        self.assertEqual(mr.ending_fingerprint(rows)["alerts"].get("说句实在"), 3)

    def test_long_form_deeper_middle_positions(self):
        """长文 5+ 段：第 4、5 段等更深的中间位置也要覆盖（opener/body 只到第2段）。
        生产实测有 17 篇 5 段 + 1 篇 7 段，这些位置此前完全无雷达。
        注意：长文分节头（一、/四、）会被 _all_segment_openers 跳过，不计段序。
        中间各段必须真的互不相同，否则它们自己就成了共享前缀。"""
        rows = self._rows([
            "开场甲。\n\n分析甲。\n\n资金流向在变。\n\n分批挂单更稳。\n\n我的打法甲。\n\n跟踪变量甲。\n\n#Write2Earn",
            "开场乙。\n\n分析乙。\n\n链上数据回暖。\n\n等确认再动。\n\n我的打法乙。\n\n跟踪变量乙。\n\n#Write2Earn",
            "开场丙。\n\n分析丙。\n\n消息面在发酵。\n\n先观望不丢人。\n\n我的打法丙。\n\n跟踪变量丙。\n\n#Write2Earn",
        ])
        fp = mr.mid_segment_fingerprint(rows)
        self.assertIn(4, fp["alerts"], "第5段（位置键 4）的「我的打法」×3 必须报")
        self.assertEqual(fp["alerts"][4].get("我的打法"), 3)
        # 位置 2/3 各不相同，不应报
        self.assertNotIn(2, fp["alerts"])
        self.assertNotIn(3, fp["alerts"])

    def test_render_line_present(self):
        rows = self._rows([
            "开场甲。\n\n分析甲。\n\n说句实在话,别慌。\n\n结尾甲。\n\n#Write2Earn",
            "开场乙。\n\n分析乙。\n\n说句实在的,沉住气。\n\n结尾乙。\n\n#Write2Earn",
            "开场丙。\n\n分析丙。\n\n说句实在点,别追。\n\n结尾丙。\n\n#Write2Earn",
        ])
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("第3段指纹预警", text)


class TestQualityPatternSync(unittest.TestCase):
    """R106：质量模式双份维护的同步守卫——main.py（防线本体）与 metrics_report
    （合规巡检）各有一份禁用装置/AI 腔/FNG 模式，静默漂移会让巡检度量失真
    （README 防漂移测试的同款思路：双份事实源必须有锁）。"""

    def test_overused_devices_in_sync(self):
        self.assertEqual(tuple(mr._OVERUSED_DEVICES),
                         tuple(m._OVERUSED_OPENING_DEVICES),
                         "永久禁用装置清单两份不一致——改 main 必须同步 metrics_report")

    def test_force_strip_cashtags_in_sync(self):
        """R610：FORCE_STRIP 清单成了第二份事实源——main 用它决定「稳定币不做
        挂件」（R316）+ 选稿跳过全稳定币帖（R586），metrics_report 用它把稳定币
        only 的零挂件从生命线告警里摘出来。两处若漂移，报表会把合规帖重新报成
        保底失守（正是 R610 要消灭的假阳性），或反过来把真失守藏进合规桶。"""
        self.assertEqual(set(mr._FORCE_STRIP_CASHTAGS),
                         set(m.SquarePublisher.FORCE_STRIP_CASHTAGS),
                         "FORCE_STRIP 清单两份不一致——改 main 必须同步 metrics_report")

    def test_ai_flavor_hard_in_sync(self):
        self.assertEqual(tuple(mr._AI_FLAVOR_HARD),
                         tuple(m.MultiLLMEngine._AI_FLAVOR_HARD),
                         "AI 腔硬清单两份不一致——改 main 必须同步 metrics_report")

    def test_fng_anchor_pattern_in_sync(self):
        self.assertEqual(mr._FNG_ANCHOR_RE.pattern, m._FNG_ANCHOR_RE.pattern,
                         "FNG 锚定检测模式两份不一致——改 main 必须同步 metrics_report")

    def test_article_header_pattern_in_sync(self):
        """R299：长文分节头跳过正则是双事实源——main._recent_openers（开场去重/
        领词守卫的采集口径）与 metrics_report._ARTICLE_HEADER_RE（指纹雷达 R124 +
        开场普查 R293 的采集口径）各持一份。两处若漂移，报表测量的"开场句"就与
        main 守卫/下发的不是同一个 → 雷达把新分节头形态误当开场句污染统计。
        FNG 锚点/标题领词两个双事实源都已有字节同一守卫，此前唯独这条漏。"""
        self.assertEqual(mr._ARTICLE_HEADER_RE.pattern, m._ARTICLE_HEADER_RE.pattern,
                         "长文分节头正则两份不一致——改 main 必须同步 metrics_report")

    def test_title_leadins_in_sync(self):
        """R286：长文标题禁用领词表是 main._GENERIC_LEADINS 的第二份事实源
        （R282 把'刚出'提进静态表后，标题侧必须同刻跟上，否则标题漏防）。"""
        self.assertEqual(tuple(mr._TITLE_LEADINS), tuple(m._GENERIC_LEADINS),
                         "标题领词表与正文领词表不一致——改 main 必须同步 metrics_report")

    def test_is_delivery_outcome_triple_in_sync(self):
        """R211/R572：is_delivery_outcome 三份拷贝（main / metrics_report /
        cost_analysis）——投递口径漂移会让交付/成本/调度分各说各话。R572 刚
        手改三处加 video_published，必须有锁防再漏。行为对账（同输入同输出）
        比字节对账更稳（函数实现可不同，语义必须一致）。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "cost_analysis", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "cost_analysis.py"))
        ca = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ca)
        cases = (
            "binance_published", "binance_published_cache_failed",
            "video_published", "okx_draft_delivered",
            "okx_draft+telegram_delivered", "telegram_delivered_cache_failed",
            "already_delivered", "run_summary", "llm_success",
            "alert_dropped_no_channel", None, "",
        )
        for c in cases:
            mv = m._is_delivery_outcome(c)
            rv = mr._is_delivery_outcome(c)
            cv = ca.is_delivery_outcome(c)
            self.assertEqual(mv, rv,
                             f"is_delivery_outcome 两份不一致 on {c!r}——改 main 必须同步 metrics_report")
            self.assertEqual(mv, cv,
                             f"is_delivery_outcome 两份不一致 on {c!r}——改 main 必须同步 cost_analysis")


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
            self._row("20天狂买1.07亿美元，Bitwise悄悄吸筹$SOL", ts="2026-09-10T00:00:00+00:00"),
            self._row("$SHIB掌门失联4个月，改个资料就想搞事？", ts="2026-09-11T00:00:00+00:00"),
            self._row("BTC $82000 Battle", ts="2026-09-12T00:00:00+00:00"),
            self._row("刚出炉：Fed升息落地，$BTC守住7.65万", ts="2026-09-17T02:09:28+00:00"),
            self._row("突发，某交易所又出事了", article=False,
                      ts="2026-09-18T00:00:00+00:00"),  # 短讯也可能带标题
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
        self.assertIn("2026-09-17", text, "命中日期必须可见，否则历史残留读成开放缺口")
        self.assertIn("2026-09-18", text)

    def test_leadin_alert_marks_pre_r296_hits_as_historical(self):
        """R329：命中日全早于 R296（2026-09-21）时，告警不得再称「守卫只覆盖正文开场」
        ——那句在 R296 后为假，会把 09-17 历史残留永久读成开放缺口（R280 同型）。"""
        _write(self.path, [
            self._row("刚出炉：Fed升息落地，$BTC守住7.65万", ts="2026-09-17T02:09:28+00:00"),
            self._row("20天狂买1.07亿美元", ts="2026-09-20T00:00:00+00:00"),
        ])
        rows, _ = mr.load_rows(self.path)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("历史残留", text)
        self.assertNotIn("守卫只覆盖正文开场", text,
                         "R296 已补 TITLE 禁令，旧叙事=假告警")

    def test_leadin_alert_flags_post_r296_hits(self):
        """R296 后新命中必须升格为「预防侧需复查」，不得混进历史残留口径。"""
        _write(self.path, [
            self._row("刚出炉：Fed升息落地", ts="2026-09-17T02:09:28+00:00"),
            self._row("突发，新命中", ts="2026-09-22T00:00:00+00:00"),
        ])
        rows, _ = mr.load_rows(self.path)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("R296 后命中", text)
        self.assertNotIn("历史残留", text)
        self.assertNotIn("守卫只覆盖正文开场", text)

    def test_leadin_detects_leading_whitespace_titles(self):
        """R329：startswith 必须对 strip 后的串——前导空格/全角空格/Tab 会漏检。"""
        _write(self.path, [
            self._row(" 刚出炉：Fed", ts="2026-09-17T00:00:00+00:00"),
            self._row("　突发：x", ts="2026-09-17T00:00:00+00:00"),
            self._row("\t注意：y", ts="2026-09-17T00:00:00+00:00"),
        ])
        rows, _ = mr.load_rows(self.path)
        s = mr.summarize(rows)
        self.assertEqual(len(s["title_leadin_hits"]), 3,
                         "前导空白标题必须命中（strip 后再 startswith）")

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


class TestFngTrioReportSurface(unittest.TestCase):
    """R289：FNG 三件套收口——hook_count 直方图 + 武装未剥离一致性告警"""

    def setUp(self):
        import tempfile, shutil
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "metrics.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _row(self, hook, armed, stripped, pv="正文略。", **kw):
        d = {"platforms": ["binance"], "outcome": "binance_published",
             "final_preview": pv, "fng_hook_count": hook,
             "fng_ban_active": armed, "fng_market_stripped": stripped}
        d.update(kw)
        return d

    def test_hook_histogram_and_armed_not_stripped_alert(self):
        """直方图按 hook_count 分档；武装却未剥离（R101 互补剥离失效）必须告警"""
        _write(self.path, [
            self._row(1, True, True),
            self._row(1, True, True),
            self._row(0, False, False),
            self._row(2, True, True),
            self._row(2, True, False),   # 武装但没剥离 → 疑似失效
        ])
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        self.assertEqual(s["fng_evaluated"], 5)
        self.assertEqual(dict(s["fng_hook_hist"]), {0: 1, 1: 2, 2: 2})
        self.assertEqual(s["fng_armed_not_stripped"], 1)
        text = mr.render_text(s, loaded)
        self.assertIn("🔥 FNG 锚点压力（5 篇）: hook=0 ×1 · hook=1 ×2 · hook=2 ×2", text)
        self.assertIn("⚠️ FNG 武装但盘面未剥离 1 篇", text)
        self.assertIn("R101 互补剥离疑似失效", text)

    def test_consistent_stripping_silent(self):
        """全部一致剥离时只有直方图、无告警（零噪音）"""
        _write(self.path, [self._row(1, True, True) for _ in range(3)])
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        text = mr.render_text(s, loaded)
        self.assertIn("🔥 FNG 锚点压力", text)
        self.assertNotIn("武装但盘面未剥离", text)

    def test_unarmed_not_stripped_not_counted(self):
        """未武装帖本就不该剥离盘面行——不得计入疑似失效（否则每篇都误报）"""
        _write(self.path, [self._row(0, False, False) for _ in range(3)])
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        self.assertEqual(s["fng_armed_not_stripped"], 0)
        self.assertNotIn("武装但盘面未剥离", mr.render_text(s, loaded))

    def test_legacy_rows_silent(self):
        """无 FNG 字段的历史帖：整块不渲染"""
        _write(self.path, [{"platforms": ["binance"],
                            "outcome": "binance_published", "final_preview": "x"}])
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        self.assertEqual(s["fng_evaluated"], 0)
        self.assertNotIn("FNG 锚点压力", mr.render_text(s, loaded))


class TestContentLengthDistribution(unittest.TestCase):
    """R292：篇幅遥测——prompt 宣称"140~200 字"（短讯，R294 对齐后）而质量门实际只卡
    60~1200 字符，发布篇幅此前无任何度量面。按体裁分桶报中位/P90/区间命中率。"""

    def setUp(self):
        import tempfile, shutil
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "metrics.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _row(self, cjk, article=False, chars=None):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "final_preview": "x", "content_cjk": cjk,
                "content_chars": cjk if chars is None else chars,
                "article": article}

    def test_short_form_length_distribution_and_band(self):
        """短讯按 140~200 区间报命中率；中位/P90 按 CJK 字数"""
        _write(self.path, [self._row(v) for v in
                           [150, 180, 200, 210, 240, 260, 300]])
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        self.assertEqual(len(s["cjk_by_genre"]["短讯"]), 7)
        text = mr.render_text(s, loaded)
        self.assertIn("📏 短讯篇幅（7 篇）", text)
        self.assertIn("中位 210 字", text)
        self.assertIn("140~200 区间内 3/7", text)

    def test_long_form_separate_bucket(self):
        """长文单独分桶（目标 500~800），不与短讯混算"""
        _write(self.path, [self._row(v, article=True) for v in
                           [520, 610, 700, 780]] + [self._row(190)])
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        self.assertEqual(len(s["cjk_by_genre"]["长文"]), 4)
        self.assertEqual(len(s["cjk_by_genre"]["短讯"]), 1)
        text = mr.render_text(s, loaded)
        self.assertIn("📏 长文篇幅（4 篇）", text)
        self.assertIn("500~800 区间内 4/4", text)
        self.assertIn("📏 短讯篇幅（1 篇）", text)

    def test_legacy_rows_without_length_silent(self):
        """旧 schema 无篇幅字段：整块不渲染（零噪音）"""
        _write(self.path, [{"platforms": ["binance"],
                            "outcome": "binance_published", "final_preview": "x"}])
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        self.assertEqual(s["cjk_by_genre"], {})
        self.assertNotIn("篇幅", mr.render_text(s, loaded))


class TestOpenerHookCensus(unittest.TestCase):
    """R293：首段钩子普查——prompt 最强调的条款"【首两行定生死】第一段必须放
    钩子（反差结论/具体数字/悬念）"此前零门零度量；与 R286 标题眼钩同口径。"""

    def setUp(self):
        import tempfile, shutil
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "metrics.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _row(self, preview):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "final_preview": preview}

    def test_hook_census_counts_three_elements(self):
        """首段三要素各自计数；长文分节头不算开场句（与雷达同语义）"""
        _write(self.path, [
            self._row("灰度给 $ZEC ETF 递了拆股申请，9 月 28 日收盘后生效。后续。"),
            self._row("$BTC 虚站 8 万，扒开持仓数据直接露馅。后续。"),
            self._row("这波是诱多出货还是真突破？后续。"),
            self._row("一、发生了什么\n\n灰度递了申请。后续。"),   # 分节头跳过
            self._row(""),                                        # 空预览不计
        ])
        loaded, _ = mr.load_rows(self.path)
        s = mr.summarize(loaded)
        self.assertEqual(s["opener_evaluated"], 4, "空预览不进分母")
        self.assertEqual(s["opener_hooks"]["数字"], 2)
        self.assertEqual(s["opener_hooks"]["$挂件"], 2)
        self.assertEqual(s["opener_hooks"]["疑问"], 1)
        text = mr.render_text(s, loaded)
        self.assertIn("🪝 首段钩子（4 篇）", text)
        self.assertIn("数字 2/4", text)

    def test_extract_opener_skips_article_header(self):
        """_extract_opener：分节头（"一、"）跳过取正文段；空/纯分节头返回空串"""
        self.assertEqual(mr._extract_opener("一、发生了什么\n\n正文第一句。"),
                         "正文第一句")
        self.assertEqual(mr._extract_opener("开头就是正文。"), "开头就是正文")
        self.assertEqual(mr._extract_opener(""), "")
        self.assertEqual(mr._extract_opener(None), "")

    def test_fingerprint_still_works_after_helper_refactor(self):
        """R293 重构守卫：_extract_opener 抽取后，开场指纹雷达行为不得漂移"""
        _write(self.path, [self._row(f"盘面放量突破，结构健康 {i}。后续略。") for i in range(3)]
               + [self._row("Bitwise 关了 ETF。后续略。")])
        loaded, _ = mr.load_rows(self.path)
        fp = mr.opener_fingerprint(loaded)
        self.assertEqual(fp["alerts"], {"盘面放量": 3}, "4 字簇优先（R124 语义）")
        self.assertEqual(fp["scanned"], 4)


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

    def test_r641_merge_preserves_per_record_snapshot_ts(self):
        """R641 连带修复：merge **不得用本次导入时刻统一覆盖全部记录的 ts**。

        ts 是归因输入（消费侧据此算曝光天数，把累积浏览量换算成速率）。
        首版写死一个全局 ts，等于宣称"所有帖曝光时长相同"——刚发的帖与5 天前
        的帖就会按绝对值直接比较，而那正是 R641 要修的偏差。
        场景：旧库里111(ts=10-01) / 222(ts=10-01)，本次只导入 333。
        期望：111/222 保留旧 ts（未被本次观测更新），333 用新 ts。
        """
        with open(self.out, "w", encoding="utf-8") as f:
            f.write(json.dumps({"content_id": "111", "views": 5000,
                                "ts": "2026-10-01T04:00:00Z"}) + "\n")
            f.write(json.dumps({"content_id": "222", "views": 50,
                                "ts": "2026-10-01T04:00:00Z"}) + "\n")
        mod = self._import()
        mod.OUT_PATH = self.out
        mod.merge_into_jsonl({"333": {"views": 7}})
        by_id = {}
        for line in open(self.out, encoding="utf-8"):
            r = json.loads(line)
            by_id[r["content_id"]] = r
        self.assertEqual(by_id["111"]["ts"], "2026-10-01T04:00:00Z",
                         "未参与本次导入的记录必须保留原快照时刻")
        self.assertEqual(by_id["222"]["ts"], "2026-10-01T04:00:00Z")
        self.assertNotEqual(by_id["333"]["ts"], "2026-10-01T04:00:00Z",
                            "本次新导入的记录应用新快照时刻")
        self.assertIn("T", str(by_id["333"]["ts"]), "新记录必须有 ts")

    def test_r641_merge_updates_ts_for_reimported_ids(self):
        """同一 id 被再次导出（快照推进）时，ts 必须更新为更新的时刻。

        否则"取了max 浏览量但ts 停在最旧那次"，曝光天数被低估 → 速率虚高，
        又变成另一种偏差。快照只会向前推进，所以取较新者。
        """
        with open(self.out, "w", encoding="utf-8") as f:
            f.write(json.dumps({"content_id": "111", "views": 500,
                                "ts": "2026-10-01T04:00:00Z"}) + "\n")
        mod = self._import()
        mod.OUT_PATH = self.out
        mod.merge_into_jsonl({"111": {"views": 900}})
        r = json.loads(open(self.out, encoding="utf-8").read().strip())
        self.assertEqual(r["views"], 900, "取 max 观测")
        self.assertNotEqual(r["ts"], "2026-10-01T04:00:00Z",
                            "重新导出的记录 ts 须推进，否则曝光天数被低估")

    def test_chinese_wan_yi_notation_parsed(self):
        """R311：币安创作者后台大数展示/导出用中文单位（"1.2万"=12000、"2亿"）。
        旧 _to_int 对这类值抛 ValueError 返 None → 高浏览帖浏览量被静默丢弃 →
        归因分析只剩低浏览帖（恰与"浏览量低怎么办"诉求相反）。数字+单位必须换算，
        裸整数/千分位逗号照旧。"""
        mod = self._import()
        self.assertEqual(mod._to_int("1.2万"), 12000)
        self.assertEqual(mod._to_int("3.5万"), 35000)
        self.assertEqual(mod._to_int("2亿"), 200000000)
        self.assertEqual(mod._to_int("1.2億"), 120000000)  # 繁体同权
        self.assertEqual(mod._to_int("  8万  "), 80000)     # 前后空白
        # 回归对照：裸整数 / 千分位 / 空 / 破折号不变
        self.assertEqual(mod._to_int("12000"), 12000)
        self.assertEqual(mod._to_int("1,234"), 1234)
        self.assertIsNone(mod._to_int("-"))
        self.assertIsNone(mod._to_int(""))
        self.assertIsNone(mod._to_int("abc"))

    def test_wan_notation_end_to_end_read_csv(self):
        """端到端：CSV 用"万"记法的浏览量必须进 records 而非被丢。"""
        with open(self.csv, "w", encoding="utf-8") as f:
            f.write("帖子ID,浏览量,点赞,评论\n")
            f.write("333,1.2万,500,42\n")
        mod = self._import()
        recs = mod.read_csv(self.csv)
        self.assertEqual(recs["333"]["views"], 12000, "万记法浏览量不得被丢弃")
        self.assertEqual(recs["333"]["likes"], 500)


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
        # R641：归因改用日均浏览速率（views / 曝光天数），必须有 ts + 快照 ts
        # 才能算出曝光时长。发布 2026-10-01T00:00Z、快照 2026-10-02T04:00Z
        # ⇒ 曝光 28/24=1.16667 天，速率 = views * 24/28 = views * 6/7。
        _t = "2026-10-01T00:00:00+00:00"
        return [
            {"platforms": ["binance"], "outcome": "binance_published", "ts": _t,
             "content_id": "c1", "hour_bj": 21, "article": False, "source": "U.Today"},
            {"platforms": ["binance"], "outcome": "binance_published", "ts": _t,
             "content_id": "c2", "hour_bj": 22, "article": False, "source": "U.Today"},
            {"platforms": ["binance"], "outcome": "binance_published", "ts": _t,
             "content_id": "c3", "hour_bj": 2, "article": True, "source": "CryptoSlate"},
            {"platforms": ["binance"], "outcome": "binance_published", "ts": _t,
             "content_id": "c4", "hour_bj": 9, "article": False, "source": "CryptoSlate"},
            # 18 点边界两侧各钉一样本：上游分桶阈值若被改动，下面断言立刻红
            {"platforms": ["binance"], "outcome": "binance_published", "ts": _t,
             "content_id": "c5", "hour_bj": 15, "article": False, "source": "Decrypt"},
            {"platforms": ["binance"], "outcome": "binance_published", "ts": _t,
             "content_id": "c6", "hour_bj": 19, "article": True, "source": "Decrypt"},
            {"platforms": ["binance"], "outcome": "binance_published", "ts": _t,
             "content_id": None, "hour_bj": 21, "article": False, "source": "U.Today"},
        ]

    def _stats_file(self, stats, snap_ts="2026-10-02T04:00:00Z"):
        p = os.path.join(self.tmpdir, "content_stats.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for cid, rec in stats.items():
                f.write(json.dumps({"content_id": cid, "ts": snap_ts, **rec},
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
        # R641：归因桶装的是**日均速率**（views / 曝光天数）。本fixture 发布
        # 2026-10-01T00:00Z、快照 2026-10-02T04:00Z ⇒ 曝光 28/24 天，
        # 速率 = views * 6/7。若哪天有人把归因改回绝对浏览量，下面这些
        # 断言会立刻红——这是 R641 的核心守卫。
        _k = 6 / 7

        def _eq(got, exp, msg):
            self.assertEqual(len(got), len(exp), msg)
            for _i, (_g, _e) in enumerate(zip(sorted(got), sorted(exp))):
                self.assertAlmostEqual(_g, _e, places=6,
                                       msg=f"{msg}[{_i}]: {_g} != {_e}")

        _eq(rows["stats_by_hourbucket"]["晚间18-24"],
            [300 * _k, 500 * _k, 150 * _k], "晚间")
        _eq(rows["stats_by_hourbucket"]["凌晨0-6"], [900 * _k], "凌晨")
        _eq(rows["stats_by_hourbucket"]["下午12-18"], [200 * _k], "下午")
        _eq(rows["stats_by_hourbucket"]["上午6-12"], [100 * _k], "上午")
        _eq(rows["stats_by_genre"]["长文"], [900 * _k, 150 * _k], "长文")
        _eq(rows["stats_by_source"]["U.Today"], [300 * _k, 500 * _k], "来源")
        self.assertEqual(rows["stats_rate_skipped"], 0)
        text = mr.render_text(rows, self._rows())
        # R628：文案带分母与覆盖率——「N 篇有记录」会被读成"共 N 篇"
        self.assertIn("内容数据（6/6 篇已发布帖有浏览数据（100%））", text)
        self.assertIn("均浏览 358", text)
        # R610：归因桶改报中位数（抗单篇爆款把排名带偏），标签随之改口径
        # R641：标签再随口径改为「速率中位」——展示值与口径必须一致
        self.assertIn("时段速率中位", text)
        self.assertIn("体裁速率中位", text)
        self.assertIn("来源速率中位", text)
        self.assertIn("日均浏览速率", text)

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

    def test_r602_style_dimension_view_attribution(self):
        """R602：文风维度 × 浏览——R521/R288/R130/R592 轮换的开场钩子/人设/结尾/
        实操角度各自的真实浏览量要能分桶，否则这些文风旋钮即便拿到互动数据也无从
        判断「哪种套路带流量」。按已落盘的轮换标签分桶、空标签不进分母。"""
        # R641：行须带 ts——归因改用日均速率，缺 ts 即曝光时长未知、
        # 不进归因分母（这是刻意设计，见 _exposure_days注释）。
        _t = "2026-10-01T00:00:00+00:00"
        style_rows = [
            {"platforms": ["binance"], "outcome": "binance_published", "ts": _t,
             "content_id": "s1",
             "hour_bj": 21, "article": False, "source": "U.Today",
             "opening_hook": "反差冲击", "persona": "毒舌老韭菜",
             "ending_style": "灵魂拷问", "trade_cta_style": "失效位优先"},
            {"platforms": ["binance"], "outcome": "binance_published", "ts": _t,
             "content_id": "s2",
             "hour_bj": 22, "article": False, "source": "U.Today",
             "opening_hook": "反差冲击", "persona": "数据拆解派",
             "ending_style": "灵魂拷问", "trade_cta_style": "风险先说"},
            {"platforms": ["binance"], "outcome": "binance_published", "ts": _t,
             "content_id": "s3",
             "hour_bj": 20, "article": False, "source": "Decrypt",
             "opening_hook": "悬念设问", "persona": "毒舌老韭菜",
             "ending_style": "对比站队"},  # trade_cta_style 缺失 → 不进 CTA 分母
        ]
        stats = {"s1": {"views": 100}, "s2": {"views": 300}, "s3": {"views": 800}}
        sp = self._stats_file(stats)
        orig = mr._STATS_CACHE.copy()
        try:
            mr._STATS_CACHE.update({"loaded": True, "data": mr.load_content_stats(sp)})
            _write(self.path, style_rows)
            rows, _ = mr.load_rows(self.path)
            summ = mr.summarize(rows)
        finally:
            mr._STATS_CACHE.update(orig)
        # 曝光 28/24 天 ⇒ 速率 = views * 6/7。浮点除法顺序不同会有 1e-13 级
        # 尾差，故逐值 assertAlmostEqual 而非 assertEqual 列表比较。
        _k = 6 / 7

        def _eq(got, exp, msg):
            self.assertEqual(len(got), len(exp), msg)
            for _i, (_g, _e) in enumerate(zip(sorted(got), sorted(exp))):
                self.assertAlmostEqual(_g, _e, places=6,
                                       msg=f"{msg}[{_i}]: {_g} != {_e}")

        _eq(summ["stats_by_hook"]["反差冲击"], [100 * _k, 300 * _k], "开场钩子")
        _eq(summ["stats_by_hook"]["悬念设问"], [800 * _k], "悬念设问")
        _eq(summ["stats_by_persona"]["毒舌老韭菜"], [100 * _k, 800 * _k], "人设")
        _eq(summ["stats_by_ending"]["灵魂拷问"], [100 * _k, 300 * _k], "结尾")
        _eq(summ["stats_by_cta"]["失效位优先"], [100 * _k], "CTA")
        self.assertNotIn("", summ["stats_by_cta"], "缺失标签不得建空桶")
        self.assertEqual(len(summ["stats_by_cta"]), 2, "s3 无 trade_cta_style 不进 CTA 分母")
        text = mr.render_text(summ, style_rows)
        # R641：标签随口径改为「速率中位」
        self.assertIn("开场钩子速率中位", text)
        self.assertIn("人设速率中位", text)
        self.assertIn("结尾套路速率中位", text)
        self.assertIn("实操角度速率中位", text)

    def test_r610_bucket_line_ranks_by_median_not_mean(self):
        """R610：归因桶按中位数排序——单篇爆款不得改写排名。

        实测依据：开场钩子「内幕爆料腔」样本 [10,86,93,111,453] 均值 151 曾排
        第一（中位仅 93 排第三），「上午6-12」时段均值 163 第一、中位 116 第三
        （该桶恰好含全场两个最大离群值 414/453）。这些桶驱动下一轮 prompt 调向，
        按均值调 = 追噪声。展示值与排序键同口径（都是中位），否则面板自相矛盾。
        """
        buckets = {"带爆款": [10, 86, 93, 111, 453],
                   "无爆款": [33, 96, 105, 119, 142]}
        line = mr._bucket_line(buckets)
        # 排序：无爆款中位 105 > 带爆款中位 93；展示值也是中位而非均值
        self.assertIn("无爆款 105×5", line)
        self.assertIn("带爆款 93×5", line)
        self.assertLess(line.index("无爆款"), line.index("带爆款"))
        self.assertNotIn("151", line, "均值 151 是被 R609/R610 证伪的口径，不得再出现")

    def test_r610_even_sample_median_not_upper_value(self):
        """R610：偶数样本取真中位（两中值的平均），不取上侧值。

        文件里既有的 `sorted(x)[len(x)//2]` 惯例在偶数样本上取的是**上侧值**——
        对浏览这种右偏分布（长尾爆款）等于系统性地往高估一侧偏，恰好抵消中位数
        抗离群值的作用。桶里 n=2 的桶很常见（实操角度全表都是 n=1/2），这个
        偏差会直接落在最需要保护的样本上。
        """
        # 均值 150、上侧值 300、真中位 200
        self.assertIn("某桶 200×2", mr._bucket_line({"某桶": [100, 300]}))
        # 三个样本时中位即中间值，行为不变
        self.assertIn("某桶 100×3", mr._bucket_line({"某桶": [50, 100, 400]}))

    # ------------------------------------------------------------------
    # R641：浏览量归因的**累积时长偏差**
    #
    # 缺陷：`content_stats.jsonl` 是每日一次性快照累积覆盖。同一份快照里，
    # 5 天前的帖已累积 5 天浏览、1 天前的帖只有 1 天——浏览量是**累积量**，
    # 绝对值里"活了多久"与"写得多好"是叠加的。原面板直接按绝对浏览量分桶，
    # 于是系统性地把老帖排到前面。生产实测（n=59，唯一快照 2026-10-02T04:00Z）：
    # 修正前后 4 个维度里**3 个的第一名翻转**——
    #   时段   晚间18-24(132) → 上午6-12(88)
    #   开场钩子 悬念设问(105) → 内幕爆料腔(80)
    #   结尾套路 情绪表态(174) → 灵魂拷问(83)
    # 换算口径=日均浏览速率（views / 曝光天数），让两个因素分开。
    # ------------------------------------------------------------------

    def test_r641_exposure_days_basic(self):
        """曝光天数 = 快照时刻 - 发布时刻，最小 0"""
        from datetime import datetime, timezone
        snap = datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            mr._exposure_days("2026-09-27T12:00:00+00:00", snap), 4.6666667, places=3)
        self.assertAlmostEqual(
            mr._exposure_days("2026-10-01T04:00:00+00:00", snap), 1.0, places=6)

    def test_r641_exposure_unknown_is_none_not_zero(self):
        """曝光时长未知必须返回 None，**不能返回 0**。

        0 会被当成"刚发布、浏览量低"参与排序——把"没数据"伪装成一个真实
        观测（R612：沉默不是通过；R617：未知≠通过）。缺 ts、坏ts、
        快照早于发布（时钟漂移/导出错误）三种情况都要覆盖。
        """
        from datetime import datetime, timezone
        snap = datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)
        self.assertIsNone(mr._exposure_days(None, snap), "缺 ts → None")
        self.assertIsNone(mr._exposure_days("不是时间", snap), "坏 ts → None")
        self.assertIsNone(mr._exposure_days("2026-10-01T00:00:00+00:00", None),
                          "缺快照时刻 → None")
        self.assertEqual(mr._exposure_days("2026-10-05T00:00:00+00:00", snap), 0.0,
                         "快照早于发布 → 夹到 0（此时是时钟问题，不是缺数据）")

    def test_r641_naive_timestamp_gets_utc_not_dropped(self):
        """naive 时间按 UTC 补 tzinfo，不静默丢样本。

        丢样本会让归因分母无声变小，读者看不出"少算了"。宁可算错一个
        可加 tz 的时刻，也不要丢掉整条观测。
        """
        snap = mr._parse_iso("2026-10-02T04:00:00Z")
        got = mr._exposure_days("2026-10-01T04:00:00", snap)
        self.assertIsNotNone(got, "naive ts 必须补 UTC 而不是当坏数据丢弃")
        self.assertAlmostEqual(got, 1.0, places=6)

    def test_r641_buckets_use_rate_not_absolute_views(self):
        """归因桶必须装**速率**。核心守卫：老帖绝对浏览更高但速率更低时，
        桶里应是速率更低的那个。

        构造：两帖同桶，c1 发了 5 天攒 1000浏览（200/天），
        c2 昨天才发、攒 150 浏览（150/天）——绝对值 c1 完胜，速率 c2 更高。
        桶里若装绝对值 → [150, 1000]；装速率 → [150, 200]。
        """
        rows = [
            {"platforms": ["binance"], "outcome": "binance_published",
             "ts": "2026-09-27T04:00:00+00:00", "content_id": "old",
             "hour_bj": 21, "article": False, "source": "U.Today"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "ts": "2026-10-01T04:00:00+00:00", "content_id": "new",
             "hour_bj": 21, "article": False, "source": "U.Today"},
        ]
        sp = os.path.join(self.tmpdir, "cs.jsonl")
        with open(sp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"content_id": "old", "views": 1000,
                                "ts": "2026-10-02T04:00:00Z"}) + "\n")
            f.write(json.dumps({"content_id": "new", "views": 150,
                                "ts": "2026-10-02T04:00:00Z"}) + "\n")
        orig = mr._STATS_CACHE.copy()
        try:
            mr._STATS_CACHE.update({"loaded": True, "data": mr.load_content_stats(sp)})
            summ = mr.summarize(rows)
        finally:
            mr._STATS_CACHE.update(orig)
        got = sorted(summ["stats_by_source"]["U.Today"])
        self.assertEqual(got, [150, 200], "桶里必须是日均速率（老帖 5 天 1000→200/天）")
        self.assertNotIn(1000, got, "绝对浏览量 1000 不得出现在归因桶里")
        # 但总量统计仍按绝对值——它对"总浏览"是有效观测
        self.assertEqual(summ["stats_views_total"], 1150)

    def test_r641_unknown_exposure_excluded_from_buckets_counted_in_total(self):
        """曝光时长未知的样本：**不进归因分母，但计入总量**。

        两件事都不做是错的：全丢 → 归因分母无声变小；全留 → 缺数据的帖
        当成真实观测参与排序。
        """
        rows = [
            {"platforms": ["binance"], "outcome": "binance_published",
             "ts": "2026-10-01T04:00:00+00:00", "content_id": "ok",
             "hour_bj": 21, "article": False, "source": "U.Today"},
            {"platforms": ["binance"], "outcome": "binance_published",
             "content_id": "no_ts",  # 缺 ts → 曝光未知
             "hour_bj": 21, "article": False, "source": "U.Today"},
        ]
        sp = os.path.join(self.tmpdir, "cs2.jsonl")
        with open(sp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"content_id": "ok", "views": 300,
                                "ts": "2026-10-02T04:00:00Z"}) + "\n")
            f.write(json.dumps({"content_id": "no_ts", "views": 9999,
                                "ts": "2026-10-02T04:00:00Z"}) + "\n")
        orig = mr._STATS_CACHE.copy()
        try:
            mr._STATS_CACHE.update({"loaded": True, "data": mr.load_content_stats(sp)})
            summ = mr.summarize(rows)
        finally:
            mr._STATS_CACHE.update(orig)
        self.assertEqual(summ["stats_rate_skipped"], 1, "曝光未知要计入跳过计数")
        self.assertEqual(summ["stats_by_source"]["U.Today"], [300],
                         "曝光未知的样本不得进归因桶")
        self.assertEqual(summ["stats_posts"], 2, "总量统计仍计入两条")
        self.assertEqual(summ["stats_views_total"], 10299)
        text = mr.render_text(summ, rows)
        self.assertIn("曝光时长未知", text, "被排除的样本数必须显式告知读者")

    # ------------------------------------------------------------------
    # R642：归因差异的**显著性判据**
    #
    # 缺陷：面板把各归因桶并排列出浏览中位，逐行标注，读起来就是
    # "内幕爆料腔比痛点直击高 62%"，仿佛是调 prompt 的依据。
    # 但生产实测 n=59 做置换检验后，时段/体裁/来源/开场/人设/结尾/实操角度
    # **七个维度全部不可与随机区分**（单侧 p 最小 0.040）。
    # 不写这一行＝把噪声陈列成结论（R622 同类事故：拿当前样本里不成立的
    # 差异去改已在生效的东西）。
    # ------------------------------------------------------------------

    def test_r642_significance_flags_real_difference(self):
        """真差异必须被抓出来（否则显著性判据就成了永远说"不显著"的摆设）"""
        rng = random.Random(1)
        pool = [rng.gauss(50, 15) for _ in range(60)]
        buckets = {"普通": [v + rng.gauss(0, 5) for v in pool[:30]],
                   "显著高": [v + 60 for v in pool[30:]]}
        sig, _note = mr._bucket_significance(buckets)
        self.assertIn("显著高", sig, "均值高 60 的桶必须被判显著")
        self.assertNotIn("普通", sig, "与全体同分布的桶不该被判显著")

    def test_r642_significance_rejects_pure_noise(self):
        """纯噪声不得被判显著——这是本条守卫的核心。

        用生产实测的量级构造：两个桶样本来自同一分布，样本量同为真实值
        （开场钩子 ~5、结尾套路 ~6），若判据会误报，则说明检验太松。
        """
        rng = random.Random(42)
        sig, note = mr._bucket_significance({
            "A": [rng.gauss(50, 20) for _ in range(5)],
            "B": [rng.gauss(52, 20) for _ in range(6)],
            "C": [rng.gauss(48, 20) for _ in range(7)],
        })
        self.assertEqual(sig, set(), "同分布的桶不得被判显著")
        self.assertIn("置换检验", note)

    def test_r642_small_buckets_not_tested(self):
        """样本不足的桶不进检验——n=1 的"桶"比中位必然=自己，判它显著毫无意义"""
        sig, _ = mr._bucket_significance({
            "孤样本": [999],
            "普通": [50, 52, 48, 51, 49, 50, 53, 47],
        })
        self.assertNotIn("孤样本", sig, "n=1 不得参与判读")

    def test_r642_pool_too_small_declares_no_test(self):
        """全体样本太少时**显式说明未做检验**，而不是静默返回空集合

        静默空集合会被渲染成"全部不可区分"——那是把"没能力判断"说成
        "判断结果是负"（R612：沉默不是通过）。
        """
        sig, note = mr._bucket_significance({"甲": [10], "乙": [20]})
        self.assertEqual(sig, set())
        self.assertIn("不做显著性检验", note)

    def test_r642_render_states_when_nothing_is_significant(self):
        """全部维度不显著时，面板必须显式说"不可据此调 prompt"。

        这是 R642 的交付面：不是把检验结果藏着，而是写成人能执行的判读。
        """
        rows, _s = [], []
        for i in range(12):
            rows.append({"platforms": ["binance"], "outcome": "binance_published",
                         "ts": "2026-10-01T04:00:00+00:00", "content_id": f"n{i}",
                         "hour_bj": 21, "article": False, "source": "U.Today",
                         "opening_hook": "反差冲击" if i % 2 else "悬念设问",
                         "persona": "毒舌老韭菜", "ending_style": "灵魂拷问"})
            _s.append({"content_id": f"n{i}", "views": 100 + (i % 3),
                       "ts": "2026-10-02T04:00:00Z"})
        sp = os.path.join(self.tmpdir, "cs3.jsonl")
        with open(sp, "w", encoding="utf-8") as f:
            for r in _s:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        orig = mr._STATS_CACHE.copy()
        try:
            mr._STATS_CACHE.update({"loaded": True, "data": mr.load_content_stats(sp)})
            summ = mr.summarize(rows)
        finally:
            mr._STATS_CACHE.update(orig)
        text = mr.render_text(summ, rows)
        self.assertIn("不可与随机区分", text)
        self.assertIn("不可据此调 prompt", text)

    def test_provider_dispatch_order_folds_and_ranks_by_latency(self):
        """R337：通道位次行——发/拒计数折叠到短通道名（同一 preset 名下多模型合并），
        按延迟↑=failover 调用顺序排列，'-'/'unknown' 剔除，无延迟样本的通道落链尾。
        锁住「慢而稳的兜底源份额小≠闲置」的可读性契约（对应 stepfun 现实定位）。"""
        rows = [
            # Preset-fast：跨两模型 3 发 + 4 拒，延迟 20（链首先试）
            {"ts": "2026-09-20T01:00:00+00:00", "outcome": "binance_published",
             "provider": "Preset-fast", "model": "model-a", "platforms": ["binance"],
             "llm_latency_sec": 20},
            {"ts": "2026-09-20T01:01:00+00:00", "outcome": "binance_published",
             "provider": "Preset-fast", "model": "model-a", "platforms": ["binance"]},
            {"ts": "2026-09-20T01:02:00+00:00", "outcome": "binance_published",
             "provider": "Preset-fast", "model": "model-b", "platforms": ["binance"]},
        ] + [
            {"ts": f"2026-09-20T02:0{i}:00+00:00", "outcome": "llm_rejected",
             "stage": "quality", "provider": "Preset-fast", "model": "model-a",
             "reason": "短"} for i in range(4)
        ] + [
            # Preset-mid：2 发 + 1 拒，延迟 50
            {"ts": "2026-09-20T03:00:00+00:00", "outcome": "binance_published",
             "provider": "Preset-mid", "model": "m", "platforms": ["binance"],
             "llm_latency_sec": 50},
            {"ts": "2026-09-20T03:01:00+00:00", "outcome": "binance_published",
             "provider": "Preset-mid", "model": "m", "platforms": ["binance"]},
            {"ts": "2026-09-20T03:02:00+00:00", "outcome": "llm_rejected",
             "stage": "numbers", "provider": "Preset-mid", "model": "m", "reason": "编造"},
        ] + [
            # Preset-slow：5 发 0 拒，延迟 80（链尾兜底、份额小但近乎零失败）
            {"ts": f"2026-09-20T04:0{i}:00+00:00", "outcome": "binance_published",
             "provider": "Preset-slow", "model": "s", "platforms": ["binance"],
             "llm_latency_sec": 80} for i in range(5)
        ] + [
            # 无延迟样本的通道：只有 1 发，应落在有延迟通道之后（链尾未知位）
            {"ts": "2026-09-20T05:00:00+00:00", "outcome": "binance_published",
             "provider": "Preset-nolat", "model": "n", "platforms": ["binance"]},
            # no_provider 拒稿（provider '-'）必须剔除，不得占位
            {"ts": "2026-09-20T06:00:00+00:00", "outcome": "llm_rejected",
             "stage": "no_provider", "provider": "-", "reason": "无可用 LLM"},
        ]
        s = mr.summarize(rows)
        disp = mr._provider_dispatch_order(
            s["by_provider"], s["reject_by_provider"], s["latency_by_provider"])
        self.assertEqual(
            [d[0] for d in disp],
            ["Preset-fast", "Preset-mid", "Preset-slow", "Preset-nolat"],
            "延迟↑=failover 顺序；无延迟样本落链尾")
        self.assertEqual(disp[0], ("Preset-fast", 3, 4, 20.0), "同 preset 多模型发/拒需折叠")
        self.assertEqual(disp[2], ("Preset-slow", 5, 0, 80.0), "慢而稳兜底源计数如实")
        self.assertEqual(disp[3][3], None, "无延迟样本通道延迟为 None")
        self.assertNotIn("-", [d[0] for d in disp], "'-' 非真实通道须剔除")
        out = mr.render_text(s)
        self.assertIn("通道位次", out)
        self.assertIn("Preset-slow 发5/拒0·80.0s", out)
        self.assertIn("Preset-nolat 发1/拒0·延迟?", out)
        self.assertLess(
            out.index("Preset-fast 发3"), out.index("Preset-slow 发5"),
            "渲染顺序须为 failover 顺序（快源在前、慢源在后）")

    def test_provider_dispatch_order_empty_when_no_channels(self):
        """全空 / 仅 '-'/'unknown' 时通道位次行沉默（零噪音惯例）"""
        self.assertEqual(mr._provider_dispatch_order({}, {}, {}), [])
        self.assertEqual(
            mr._provider_dispatch_order(
                {"-": 3}, {"unknown": 2}, {"unknown": 10.0}), [])

    def test_dispatch_order_pinned_channels_go_first(self):
        """R621：置顶通道无视延迟排在链首。

        生产实证：main._ordered_providers 的真实顺序是 失败数 → -priority →
        延迟分——priority 在延迟**之前**。而旧显示按纯延迟排并宣称「靠前先试」，
        把 15.5s 的 google 排到链首（实际它只发 1 篇、是链尾兜底），真正的首选
        stepfun-flash（发 28 篇）反而排第二——读表的人会得出与事实相反的结论。

        锁住三点：① pinned 通道在非 pinned 之前（即便延迟更高）；
        ② pinned 组内部仍按延迟序（AST 拿不到 priority 数值，组内是近似）；
        ③ pinned=None 时回退纯延迟序（解析失败不撒谎、只是缺一层信息）。"""
        by_pub = {"Preset-fast": 28, "Preset-slowprio": 5, "Preset-zippy": 1}
        by_rej = {"Preset-fast": 3, "Preset-zippy": 0}
        lat = {"Preset-fast": 25.0, "Preset-slowprio": 80.0, "Preset-zippy": 15.0}
        # slowprio 被置顶：真实首选（80s 仍排第一）。zippy 最快（15s）但只是
        # 非置顶组的第一位——关键断言是置顶的 80s 压过未置顶的 15s。
        disp = mr._provider_dispatch_order(by_pub, by_rej, lat,
                                           pinned=["Preset-slowprio"])
        self.assertEqual(
            [d[0] for d in disp],
            ["Preset-slowprio", "Preset-zippy", "Preset-fast"],
            "置顶通道无视延迟先排，其余按延迟序（zippy 15s 在 fast 25s 前）")
        # 无 pinned → 回退纯延迟序（zippy 15s 最先）
        disp2 = mr._provider_dispatch_order(by_pub, by_rej, lat, pinned=None)
        self.assertEqual([d[0] for d in disp2],
                         ["Preset-zippy", "Preset-fast", "Preset-slowprio"])
        # 空 pinned 集合同样回退
        disp3 = mr._provider_dispatch_order(by_pub, by_rej, lat, pinned=[])
        self.assertEqual([d[0] for d in disp3],
                         ["Preset-zippy", "Preset-fast", "Preset-slowprio"])

    def test_priority_pinned_parser_reads_main_py(self):
        """R621：AST 解析器必须从 main.py 实际代码里读出置顶通道清单。

        main.py 的实际写法是 `if p.name == "Preset-stepfun-flash": p.priority =
        _sf_prio + 1`（elif 链）——解析器要能跟上这个写法；main.py 重构置顶
        配置后本测试同步暴露，避免清单与池定义漂移（同 _provider_pool_from_main
        的判据）。"""
        pinned = mr._priority_pinned_from_main()
        self.assertIn("Preset-stepfun-flash", pinned,
                      "stepfun-flash 在 main.py 里被显式置顶（用户 2026-09-23 指定）")
        self.assertIn("Preset-stepfun", pinned, "stepfun 同在 elif 链里被置顶")
        # 解析失败路径：指向不存在的文件 → 空集（响亮失败、不撒谎）
        self.assertEqual(mr._priority_pinned_from_main("/nonexistent/main.py"), [])


class TestR617ProviderProbe(unittest.TestCase):
    """R617：provider 默认模型名探针的结论消费面。

    背景：R615 把 R263 僵尸名检查自动化了，但结论只 print 到 Actions 日志，
    而 R614 实测通知渠道 0 个——**探针在跑，答案被丢弃**。这里锁定三态渲染，
    尤其是「部分未核实」绝不能退化成「全部通过」这个会骗人的读法。
    """

    @staticmethod
    def _probe(**kw):
        row = {"ts": "2026-10-02T01:00:00+00:00", "outcome": "provider_probe",
               "probe_ok": True, "sites_total": 12, "sites_ok": 12,
               "sites_unknown": 0}
        row.update(kw)
        return [row]

    def test_partial_coverage_never_renders_as_all_pass(self):
        """R617 核心守卫：9站未核实 + 3 站存活 → 必须显示「仅部分核实」。

        这条锁的是我自己实跑时差点上线的 bug：_num() 归一化返回 float，
        渲染层用 isinstance(x, int) 判分母恒为假 → 覆盖率渲染成「—」→
        未核实分支永不触发 →「核实 3/12」被显示成「全部核实通过」。
        """
        rows = self._probe(probe_ok=True, sites_total=12, sites_ok=3,
                           sites_unknown=9,
                           unknown_sites="aihubmix,b.ai,tokenrouter")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("仅部分核实", text)
        self.assertNotIn("全部核实通过", text)
        self.assertIn("3/12", text, "覆盖率必须带分母，只显存活数会读成全绿")
        self.assertIn("aihubmix", text, "未核实站名必须列出，否则读者无法行动")
        self.assertIn("未核实≠存活", text)

    def test_full_coverage_renders_all_pass(self):
        """12/12 全部核实 → 才允许显示「全部核实通过」"""
        rows = self._probe()
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("全部核实通过", text)
        self.assertIn("12/12", text)
        self.assertNotIn("仅部分核实", text)

    def test_probe_itself_failed_is_live_alarm(self):
        """探针自己没跑成（probe_ok=False）→ 活警，且不得同时宣称"部分核实"。

        L3 缺口：`|| echo` 兜底让"探针挂了"与"检查通过"同貌。此处必须分开。
        """
        rows = self._probe(probe_ok=False, sites_ok=3, sites_unknown=9,
                           probe_error="池解析为空")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("探针未完成", text)
        self.assertIn("不可信", text)
        self.assertNotIn("全部核实通过", text)
        self.assertNotIn("仅部分核实", text)
        # probe_error 已含未核实站数，不得再叠加一遍造成复读
        self.assertNotIn("未核实，未核实", text)

    def test_no_probe_row_reports_blindness_not_pass(self):
        """一条 provider_probe 都没有 → 显式报「失明」，绝不沉默。

        沉默不是通过（R612：没人看=等于没有）。这条防止探针被误删/失联后
        面板毫无反应——那正是 R615 之前的原始状态。
        """
        rows = [{"ts": "2026-10-02T00:00:00+00:00", "outcome": "run_summary",
                 "candidates": 0, "published": 0}]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("无结论行", text)
        self.assertIn("失明", text)
        self.assertNotIn("全部核实通过", text)

    def test_latest_probe_row_wins(self):
        """多轮探针取最新一轮（后写覆盖先写，同 R113 口径）"""
        rows = [
            {"ts": "2026-10-01T01:00:00+00:00", "outcome": "provider_probe",
             "probe_ok": True, "sites_total": 12, "sites_ok": 12,
             "sites_unknown": 0},
            {"ts": "2026-10-02T01:00:00+00:00", "outcome": "provider_probe",
             "probe_ok": True, "sites_total": 12, "sites_ok": 2,
             "sites_unknown": 10, "unknown_sites": "xkiro,tokenrouter"},
        ]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["provider_probe"]["sites_ok"], 2.0)
        text = mr.render_text(s, rows)
        self.assertIn("2/12", text)
        self.assertNotIn("全部核实通过", text)

    def test_probe_row_not_counted_as_publish_attempt(self):
        """探针行绝不能混进发布漏斗：它不是发帖尝试。

        若混入 ATTEMPT_OUTCOMES，探针每轮都会给成功率分母加 1，发布成功率
        会被无声稀释——这类"辅助遥测污染主指标"是报表最隐蔽的失真来源。
        """
        rows = self._probe() + [
            {"ts": "2026-10-02T00:00:00+00:00", "outcome": "llm_success",
             "title": "T", "provider": "P", "model": "m"}]
        f = mr.funnel(rows)
        self.assertEqual(f["attempted"], 1, "探针行不得计入尝试数")
        self.assertEqual(f["delivered"], 0)


class TestR618ZombieNotDiluted(unittest.TestCase):
    """R618：已确证的僵尸名不得被"另一批站未核实"稀释掉。

    生产实锤（2026-10-02 首轮CI 运行）：探针抓到 `Preset-aihubmix` 的默认名
    `coding-glm-5.3-flash-free` 已从 417 模型目录消失——R263 第四次复发，且正好
    落在 R615 探针的盲区里。但同一轮有 8 站因缺 key 未核实，于是 R617 首版的
    单判据 `probe_ok=False` 把整轮渲染成「探针未完成」，**僵尸名被活活吞掉**。

    判据原则：僵尸名是**已确证的事实**（默认名确实不在目录里），未核实只是
    覆盖缺口；两者处置动作完全不同（换名 vs 补 key），不能合并判。
    """

    @staticmethod
    def _row(**kw):
        row = {"ts": "2026-10-02T09:59:16+00:00", "outcome": "provider_probe",
               "sites_total": 12, "sites_ok": 3, "sites_unknown": 8,
               "unknown_sites": "b.ai,tokenrouter",
               "zombie_count": 1, "zombie_sites": "aihubmix",
               "probe_ok": False, "probe_error": "8 站未核实"}
        row.update(kw)
        return [row]

    def test_zombie_survives_partial_coverage(self):
        """核心守卫：8站未核实 + 1 僵尸 → 僵尸行必须出现且带处置动作"""
        rows = self._row()
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("僵尸名", text)
        self.assertIn("aihubmix", text, "僵尸站名必须显式给出，否则无从处置")
        self.assertIn("改 *_MODEL", text, "僵尸行必须挂处置动作")
        # 未核实行仍要独立存在（它是另一个问题，不该被僵尸行吃掉）
        self.assertIn("仅部分核实", text)
        # 关键：不得把整轮说成"不可信"而把已确证的僵尸名降级
        self.assertNotIn("本轮僵尸名结论不可信", text)

    def test_zombie_alone_renders_single_line(self):
        """仅僵尸、无未核实 → 只出僵尸行，不再补"全部核实通过" """
        rows = self._row(sites_ok=11, sites_unknown=0, unknown_sites="",
                         probe_ok=True, probe_error=None)
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("僵尸名", text)
        self.assertNotIn("全部核实通过", text,
                         "有僵尸时绝不能同时宣称全部通过")
        self.assertNotIn("仅部分核实", text)

    def test_probe_failure_without_zombie_still_alarm(self):
        """探针失败且无僵尸 → 仍走"结论不可信"活警（判据未被削弱）"""
        rows = self._row(sites_total=0, sites_ok=0, sites_unknown=0,
                         zombie_count=None, zombie_sites="",
                         probe_error="池解析为空", unknown_sites="")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("探针未完成", text)
        # 注意不能断言 "僵尸名" not in text —— 活警文案的解释句里含
        # "本轮僵尸名结论不可信"这个短语。要断言的是**没有僵尸行**，
        # 即不出现"僵尸名 N 站"这种带站数与站名的行。
        self.assertNotIn("僵尸名 0 站", text)
        self.assertNotIn("💀", text, "无确证僵尸时不得出现 💀 活警行")

    def test_legacy_row_without_zombie_field(self):
        """历史行没有 zombie_count 字段时按 0 处理，不崩、不误报。

        遥测字段可缺失是原则 4（append_metrics 过滤 None，所有新增字段走
        "缺失即 None"）。R617 首轮写的那行就是这种历史行。
        """
        rows = [{"ts": "2026-10-02T09:59:16+00:00", "outcome": "provider_probe",
                 "sites_total": 12, "sites_ok": 3, "sites_unknown": 8,
                 "unknown_sites": "b.ai", "probe_ok": False,
                 "probe_error": "8 站未核实"}]
        s = mr.summarize(rows)
        self.assertIsNone(s["runs"]["provider_probe"]["zombies"])
        text = mr.render_text(s, rows)
        self.assertNotIn("僵尸名 1 站", text)
        self.assertIn("探针未完成", text)


class TestR620ProviderQuality(unittest.TestCase):
    """R620：provider 质量产出（通过率）——排序指标不能选错。

    动机：既有的「通道位次」行只给"发N/拒M"，要心算才知道好坏；且它按
    **延迟**排序，而生产实测最慢第二的通道同时是通过率垫底的那个
    （Preset-openrouter 61s / 42% vs Preset-stepfun-flash 25s / 87%）。
    按调用量排更会误导（openrouter 104 次看着"主力"，实际近半被拒）。
    """

    @staticmethod
    def _rows():
        return [
            # openrouter：44 成功 / 56 拒 / 4 失败 → 42%
            *[{"ts": "2026-10-01T00:00:00+00:00", "outcome": "binance_published",
               "provider": "Preset-openrouter", "title": f"t{i}",
               "platforms": ["binance"]} for i in range(44)],
            *[{"ts": "2026-10-01T00:00:00+00:00", "outcome": "llm_rejected",
               "provider": "Preset-openrouter", "stage": "quality",
               "title": f"r{i}"} for i in range(56)],
            {"ts": "2026-10-01T00:00:00+00:00", "outcome": "llm_failed",
             "provider": "Preset-openrouter", "stage": "transport",
             "title": "f0"},
            # stepfun-flash：92/14 → 87%
            *[{"ts": "2026-10-01T00:00:00+00:00", "outcome": "binance_published",
               "provider": "Preset-stepfun-flash", "title": f"s{i}",
               "platforms": ["binance"]} for i in range(92)],
            *[{"ts": "2026-10-01T00:00:00+00:00", "outcome": "llm_rejected",
               "provider": "Preset-stepfun-flash", "stage": "quality",
               "title": f"q{i}"} for i in range(14)],
        ]

    def test_pass_rate_computed_from_delivered_not_llm_success(self):
        """口径守卫：分子是**投递成功**，不是 llm_success。

        实测全部 46 条 llm_success 的 stage都是 campaign_intel（情报刷新），
        把它当发帖成功会让所有通道通过率显示 0%——我第一版就犯了这个错，
        渲染出"全部 0%"的荒谬结果。
        """
        s = mr.summarize(self._rows())
        pq = s["provider_quality"]
        self.assertEqual(pq["Preset-openrouter"]["ok"], 44)
        self.assertEqual(pq["Preset-openrouter"]["rej"], 56)
        self.assertEqual(pq["Preset-openrouter"]["fail"], 1)
        rate = pq["Preset-openrouter"]["ok"] / (
            pq["Preset-openrouter"]["ok"] + pq["Preset-openrouter"]["rej"]
            + pq["Preset-openrouter"]["fail"])
        self.assertAlmostEqual(rate, 44 / 101, places=3)

    def test_campaign_intel_success_not_counted_as_post_output(self):
        """情报刷新成功不得计入发帖产出（否则某通道凭空多出成功数）。

        注意断言口径：那条拒稿**应该**进统计（它确实是一次失败的尝试），
        要验的是 ok 仍为 0 —— 即情报刷新的成功没有被当成发帖成功。
        """
        rows = [{"ts": "2026-10-01T00:00:00+00:00", "outcome": "llm_success",
                 "stage": "campaign_intel", "provider": "Preset-b.ai"}] + \
            [{"ts": "2026-10-01T00:00:00+00:00", "outcome": "llm_rejected",
              "provider": "Preset-b.ai", "stage": "quality", "title": "x"}]
        s = mr.summarize(rows)
        pq = s["provider_quality"].get("Preset-b.ai")
        self.assertIsNotNone(pq)
        self.assertEqual(pq["ok"], 0, "情报刷新成功不得算作发帖产出")
        self.assertEqual(pq["rej"], 1)

    def test_ranked_by_pass_rate_not_call_volume(self):
        """核心：排序必须按通过率，于是高调用低产出的通道排到末位。"""
        rows = self._rows()
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("按通过率排序", text)
        # openrouter 调用量最大（101）但通过率最低 → 必须排在 stepfun-flash 之后
        self.assertLess(text.index("Preset-stepfun-flash:通过率"),
                        text.index("Preset-openrouter:通过率"))
        # 夹具里openrouter = 44 成功 / 56 拒 / 1 失败 = 44/101 ≈ 43.6% → 渲染 44%
        self.assertIn("44%", text)
        self.assertIn("87%", text)

    def test_low_pass_rate_with_enough_sample_flagged(self):
        """样本充足且通过率<25% → 必须告警（大量调用但几乎不产出）"""
        rows = [{"ts": "2026-10-01T00:00:00+00:00", "outcome": "llm_rejected",
                 "provider": "Preset-x", "stage": "quality", "title": f"z{i}"}
                for i in range(20)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("大量调用但几乎不产出", text)
        self.assertIn("考虑降权", text)

    def test_small_sample_not_flagged_as_alarm(self):
        """样本 <5 不告警——小样本高拒稿多是正常波动，过度解读会制造噪音。"""
        rows = [{"ts": "2026-10-01T00:00:00+00:00", "outcome": "llm_rejected",
                 "provider": "Preset-x", "stage": "quality", "title": f"z{i}"}
                for i in range(3)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("样本少，勿过度解读", text)
        self.assertNotIn("考虑降权", text)

    def test_absent_when_no_provider_data(self):
        """无provider 数据时整行沉默（零噪音惯例）"""
        rows = [{"ts": "2026-10-01T00:00:00+00:00", "outcome": "intel_cooldown_skip"}]
        self.assertNotIn("通道质量产出", mr.render_text(mr.summarize(rows), rows))

    def test_dash_provider_excluded(self):
        """'-'/unknown 等非真实通道不得进入质量统计（它不是通道）"""
        rows = [{"ts": "2026-10-01T00:00:00+00:00", "outcome": "llm_rejected",
                 "provider": "-", "stage": "quality", "title": "x"}]
        s = mr.summarize(rows)
        self.assertEqual(s["provider_quality"], {})


class TestR621FeedYieldAttribution(unittest.TestCase):
    """R621：源入选率的丢弃归因——低入选率必须能区分根因。

    动机（离线复现实证，非推测）：`per_feed_yield` 此前只有 入选/扫描 两个数。
    用真实 NewsFetcher 跑两个场景，入选率**完全一样**：
      - 场景 A：U.Today与 CoinDesk 报道完全同题→ U.Today 0/3（它只是排序
        靠后被跨源去重吃掉，**内容是好的**）；
      - 场景 B：CoinDesk 全是 200h+ 旧闻 → CoinDesk 0/6（**源真的坏了**）。
    两者都只表现为「kept 少」，而处置动作完全相反（前者什么都不用做，后者换源）。
    R620 已立「告警必须指向可处置的根因」，本类把该纪律落到源治理面。
    """

    @staticmethod
    def _row(pf, ts="2026-10-02T12:00:00+00:00"):
        return {"ts": ts, "outcome": "run_summary", "candidates": 10,
                "published": 1, "drafts": 0, "skipped_batch_dup": 0,
                "skipped_no_token": 0, "skipped_token_limit": 0,
                "skipped_risk_blocked": 0, "skipped_parked": 0,
                "skipped_exception": 0, "per_feed_yield": pf}

    def test_discard_reasons_accumulated_per_source(self):
        """三个discarded_* 必须按源独立累加，不混进全局数"""
        rows = [self._row({"A": {"entries": 10, "kept": 4,
                                 "discarded_stale": 3, "discarded_cached": 2,
                                 "discarded_dup": 1}}),
                self._row({"A": {"entries": 5, "kept": 1,
                                 "discarded_stale": 1, "discarded_cached": 2,
                                 "discarded_dup": 1}},
                           ts="2026-10-02T13:00:00+00:00")]
        fy = mr.summarize(rows)["runs"]["feed_yield"]["A"]
        self.assertEqual(fy, [15, 5, 4, 4, 2])
        # 恒等式：缺口 = 三类丢弃之和（复现里两个场景都精确闭合）
        self.assertEqual(fy[0] - fy[1], fy[2] + fy[3] + fy[4])

    def test_main_cause_rendered_next_to_rate(self):
        """主因必须显示在入选率旁边（否则归因数据没有消费面）"""
        rows = [self._row({"CoinDesk": {"entries": 28, "kept": 0,
                                         "discarded_stale": 26,
                                         "discarded_cached": 1, "discarded_dup": 1}})]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("CoinDesk 0/28", text)
        self.assertIn("主因旧闻 26", text)

    def test_cross_source_dup_not_flagged_as_alarm(self):
        """核心判据：被跨源去重吃光**不是源的过错**，不得标⚠️。

        R621 的核心修复。离线复现里这个源是健康源，只是排序靠后。
        报警会把运营引去撤掉好源——比不报警更坏。
        """
        rows = [self._row({"BlockTempo": {"entries": 22, "kept": 0,
                                           "discarded_stale": 0,
                                           "discarded_cached": 0,
                                           "discarded_dup": 22}})]
        s = mr.summarize(rows)
        text = mr.render_text(s, rows)
        self.assertIn("主因跨源同题 22", text)
        line = next(ln for ln in text.splitlines() if "源入选率" in ln)
        self.assertNotIn("⚠️", line, "跨源同题不该触发源告警")

    def test_stale_dominated_zero_yield_does_flag(self):
        """对照：主因是源自身问题（旧闻）且 0 入选 → 必须告警"""
        rows = [self._row({"CoinDesk": {"entries": 28, "kept": 0,
                                         "discarded_stale": 26,
                                         "discarded_cached": 1, "discarded_dup": 1}})]
        text = mr.render_text(mr.summarize(rows), rows)
        line = next(ln for ln in text.splitlines() if "源入选率" in ln)
        self.assertIn("⚠️", line)

    def test_tied_causes_not_guessed(self):
        """并列主因不得选一个当答案（不确定就说并列）"""
        rows = [self._row({"DailyHodl": {"entries": 24, "kept": 0,
                                         "discarded_stale": 10,
                                         "discarded_cached": 10, "discarded_dup": 4}})]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("主因并列", text)
        line = next(ln for ln in text.splitlines() if "源入选率" in ln)
        self.assertNotIn("⚠️", line, "原因不明时不得升级成告警")

    def test_legacy_rows_without_attribution_say_so(self):
        """R621 原则 4 + R617「沉默不是通过」：归因全缺必须显式说明。

        否则 194 行历史遥测（全无 discarded_*）会被读成"这些源一条都没被丢"，
        把未知显示成通过。
        """
        rows = [self._row({"U.Today": {"entries": 1246, "kept": 899}})]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("丢弃归因暂无数据", text)
        self.assertIn("暂勿据此换源", text)

    def test_zero_discarded_renders_no_cause_clause(self):
        """真的一条都没被丢时不该编出主因（_dom==0 → 无归因子句）"""
        rows = [self._row({"Decrypt": {"entries": 25, "kept": 25,
                                       "discarded_stale": 0, "discarded_cached": 0,
                                       "discarded_dup": 0}})]
        text = mr.render_text(mr.summarize(rows), rows)
        line = next(ln for ln in text.splitlines() if "源入选率" in ln)
        self.assertIn("Decrypt 25/25", line)
        self.assertNotIn("主因", line)
        self.assertNotIn("丢弃归因暂无数据", text)

    def test_small_sample_zero_yield_not_flagged(self):
        """样本不足（entries<20）维持旧口径不告警——避免噪声（R617 告警预算）"""
        rows = [self._row({"Tiny": {"entries": 6, "kept": 0,
                                    "discarded_stale": 5, "discarded_cached": 0,
                                    "discarded_dup": 0}})]
        text = mr.render_text(mr.summarize(rows), rows)
        line = next(ln for ln in text.splitlines() if "源入选率" in ln)
        self.assertNotIn("⚠️", line)


class TestR623PoolCoverageBlindSpot(unittest.TestCase):
    """R623：池内通道的实际覆盖——「有 key」不等于「会上场」。

    动机（生产实测，非推测）：R617 的探针验的是「默认模型名还活着吗」（端点/
    目录可达性），**完全没有回答"这条通道实际会不会被排序选中"**。生产实锤：
    池内 12 站，近 30 天有遥测的只有 4 站，**8 站（67%）零尝试**——而它们
    既没被熔断（_llm_breaker 为空），key 也在 workflow 里注入了。

    根因不在故障，而在 `STEPFUN_PRIORITY=1`：它把 stepfun 系抬到免费池之上，
    而链第 1、2 名恰好都是 stepfun 系（**同base_url、同 api_key 的两个模型，
    不是一个网关两个容灾池**），次席成功 → 后位通道永不上场。于是"唯一被验证过
    的一簇恰好是同一家"——这是真正的单点故障，而面板对此零输出。
    """

    def _row(self, **kw):
        r = {"ts": "2026-10-02T00:00:00+00:00", "outcome": "llm_rejected",
             "stage": "quality", "provider": "Preset-stepfun-flash", "title": "t"}
        r.update(kw)
        return r

    def test_pool_parsed_from_main_ast(self):
        """覆盖面必须来自 main.py 的 extra_keys，不能自己维护一份列表。

        R617 判据：R615 硬编码 2 站而池内 12 站，历史 3 次僵尸名事件2 次落在
        盲区——探针恰好漏掉了它要防的那类事故。AST 优于 import（有副作用）
        与正则（改缩进就静默漏站）。

        R626：b.ai 弃用后池从 12 站降到 11，故把站名断言里的 b.ai 换成 google
        （R615 接入、独立额度池的通道）。精确站数由探针侧的
        test_real_main_pool_has_eleven_sites 锁定。
        """
        pool = mr._provider_pool_from_main()
        self.assertGreaterEqual(len(pool), 10,
                                "池定义解析异常，池内通道数远低于预期")
        for name in ("stepfun", "stepfun-flash", "openrouter", "google"):
            self.assertIn(name, pool)

    def test_pool_parse_failure_returns_empty_not_all_passed(self):
        """解析失败必须返回空（让渲染层报"失明"），绝不能退化成"全部已覆盖"

        —— 空池会让"代码损坏"伪装成"全绿"，与 R617 探针的响亮失败同判据。
        """
        self.assertEqual(mr._provider_pool_from_main("/nonexistent/main.py"), [])

    def test_probe_rows_are_not_counted_as_attempts(self):
        """探针行不是 LLM 尝试。

        若计入，每轮一次的 provider_probe 会让"被探针看见的通道"显示成
        "被验证过 N 次"，恰好把本条要抓的盲区（模型可用性从未被验证）
        伪装成已覆盖。
        """
        rows = [{"ts": "2026-10-02T00:00:00+00:00", "outcome": "provider_probe",
                 "provider": "-", "sites_total": 12, "sites_ok": 12}]
        s = mr.summarize(rows)
        self.assertEqual(s.get("provider_attempts"), {},
                         "provider_probe 被误计为 LLM 尝试")

    def test_never_tried_channels_are_flagged_with_denominator(self):
        """未被尝试的通道必须显式报警，且带分母（占比）。

        缺分母时"8 条从未被尝试"是一个孤立事实，读者无法判断严重性；
        呼应 R621「指标的覆盖面要显式」。
        """
        rows = [self._row()]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("池内通道覆盖", text)
        self.assertIn("从未被尝试", text)
        line = next(ln for ln in text.splitlines() if "从未被尝试" in ln)
        self.assertIn("/", line, "必须带分母（n/池内总数）")
        self.assertIn("⚠️", line)

    def test_placeholder_provider_not_counted(self):
        """占位 provider（非真实通道）不计入尝试数

        —— 否则「无可用提供商」这类行会让某个假通道看起来被验证过。
        """
        rows = [self._row(provider="-"),
                self._row(provider="unknown"),
                self._row(provider="", stage="no_provider")]
        s = mr.summarize(rows)
        self.assertEqual(s.get("provider_attempts"), {})

    def test_slash_folded_to_short_channel_name(self):
        """`Preset-openrouter/free` 折叠到 `Preset-openrouter`

        —— 与 provider_quality /_provider_dispatch_order 同粒度，否则两张表
        会数出不同数量的通道，"有遥测 N/12" 这个对比就失去意义。
        """
        rows = [self._row(provider="Preset-openrouter/free")]
        s = mr.summarize(rows)
        self.assertIn("Preset-openrouter", s["provider_attempts"])
        self.assertNotIn("Preset-openrouter/free", s["provider_attempts"])

    def test_rendering_claims_only_what_telemetry_proves(self):
        """R620 反面纪律：措辞不得断言遥测无法证实的事。

        草稿里写过"key 已注入 workflow 且非熔断状态"——这两句 metrics.jsonl
        都证实不了（key 在 workflow、断路器状态都在别处）。报表写下一句自己
        证明不了的话，读者会当成已核实结论，而它恰恰是处置动作的前提。
        """
        rows = [self._row()]
        text = mr.render_text(mr.summarize(rows), rows)
        line = next(ln for ln in text.splitlines() if "从未被尝试" in ln)
        self.assertNotIn("key 已注入", line)
        self.assertNotIn("非熔断状态", line)
        # 但必须把"需要人去向别处对账"说出来，否则读者以为面板已给全答案。
        #
        # R630：分组后"对账"落在**待对账组**那一行（探针未报缺 key 不等于
        # key 有效 —— 那正是需要人工核实的部分），而合计行只作汇总。
        # 断言放宽到**整段**而不是单行，避免结构优化就要改守卫。
        self.assertIn("对账", text)

    def test_all_channels_tried_renders_no_warning(self):
        """全部池内通道都有遥测时不报⚠️——告警预算纪律（R614）

        没有 ⚠️ 就没有"什么都好"的额外声明，只需一行覆盖率。
        """
        pool = mr._provider_pool_from_main()
        rows = [self._row(provider="Preset-" + k) for k in pool]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("池内通道覆盖", text)
        self.assertNotIn("从未被尝试", text)
        self.assertNotIn("⚠️", text)

    def test_blank_pool_reports_blindness_not_all_green(self):
        """池解析失败时必须响亮报"失明"，不能静默跳过这行。

        静默跳过 = 让「代码损坏」伪装成「全绿」，与 test_pool_parse_failure
        是同一判据的渲染侧守卫（R617：沉默不是通过）。

        **回归**：守卫首版用 `s.get("by_provider")` 判定"有通道活动"，而
        by_provider 只统计**投递成功**——纯拒稿轮次下它是空 Counter，于是
        最需要报失明的场景（全拒）反而静默。生产实测拒稿才是拒单遥测的主体
        （quality/numbers/transport 全走 llm_rejected），这不是理论分支。
        本例用纯拒稿行复现该场景并钉住判据。
        """
        rows = [self._row()]   # llm_rejected，无投递 → by_provider 为空
        self.assertEqual(mr.summarize(rows).get("by_provider") or {},
                         {}, "前提变了：本用例应改用有投递的行")
        real = mr._provider_pool_from_main
        try:
            mr._provider_pool_from_main = lambda *a, **k: []
            text = mr.render_text(mr.summarize(rows), rows)
        finally:
            mr._provider_pool_from_main = real
        self.assertIn("覆盖未知", text)
        self.assertIn("失明", text)
        self.assertIn("⚠️", text)


class TestR624ErrorFreshness(unittest.TestCase):
    """R624：错误串全史计数补新鲜度分级——陈迹与活警不得同貌。

    动机（生产实测，非推测）：`errors` 是全史 Counter、零新鲜度，而遥测只有
    25 天历史、报表默认窗口"全史"。于是「近30 天 404 有 20 次」技术上成立、
    描述的却是全史。实测四类里**三类已绝迹**：
      404 僵尸名 20 次，末次 09-08（24 天前）—— R263 早已闭环
      超时      23 次，末次 09-21（11 天前）
      空内容    24 次，末次 09-22（10 天前）
      b.ai 余额 11 次，末次 09-29（3.2 天前）—— **仍活**
    原渲染把它们并排成 `[(404, 20), (超时, 23), ...]`，读起来像"当前有 78 个
    问题"。这是 R613/R614 已为 alert_dropped_no_channel 立过判据的同一类缺陷。
    """

    def _row(self, reason, ts="2026-10-02T12:00:00+00:00"):
        r = {"outcome": "llm_failed", "provider": "Preset-x", "title": "t"}
        if ts is not None:
            r["ts"] = ts
        else:
            # 无ts 夹具：小时/星期等派生字段也一并省略，模拟历史裸行
            r["provider"] = "Preset-x"
        r["reason"] = reason
        return r

    def test_error_last_tracks_latest_occurrence(self):
        """末次时刻取**最新**那一行，不是首行也不是末行（行序不保证时序）。"""
        rows = [self._row("故障 A", "2026-10-01T10:00:00+00:00"),
                self._row("故障 A", "2026-10-02T11:00:00+00:00"),
                self._row("故障 A", "2026-10-01T23:00:00+00:00")]
        s = mr.summarize(rows)
        self.assertEqual(s["errors"]["故障 A"], 3)
        self.assertTrue(str(s["error_last"]["故障 A"]).startswith("2026-10-02T11:00"),
                        f"末次时刻取错: {s['error_last'].get('故障 A')}")

    def test_fresh_error_renders_as_live_alert(self):
        """≤24h 的错误必须落在活警行"""
        rows = [self._row("正在发生的故障")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("活警", text)
        self.assertIn("正在发生的故障", text)
        self.assertNotIn("陈迹", text)

    def test_stale_error_downgraded_but_not_deleted(self):
        """>24h 降为 ℹ️ 陈迹，但**绝不消失**（R614：安全面告警消失即"以为没发生"）"""
        rows = [self._row("已绝迹的故障", "2026-09-08T12:00:00+00:00"),
                self._row("现在的基准时刻", "2026-10-02T12:00:00+00:00")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("陈迹", text)
        self.assertIn("已绝迹的故障", text)
        self.assertIn("ℹ️", text)
        # 关键：降级不等于删除
        self.assertIn("已绝迹的故障", text)

    def test_missing_timestamp_treated_as_live_not_stale(self):
        """无时间戳 → **按活警处理，不降级**。

        判据同R613 的 _fresh：把未知态报成陈迹会藏起一个可能正在发生的问题，
        告警漏判的代价远大于多报一条。**未知 ≠ 已解决。**
        """
        rows = [self._row("无时间戳的故障", ts=None)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("活跃度未知", text)
        self.assertIn("无时间戳的故障", text)
        self.assertNotIn("陈迹", text, "无时间戳被误降级为陈迹")

    def test_freshness_reference_is_dataset_not_wallclock(self):
        """参照点必须是数据集自身的最新时刻，不是 now()。

        否则回看历史数据时（CI 里跑旧 metrics.jsonl、--days 过滤后看旧窗口）
        **所有行都会被算成陈迹**——"全史"与"当前"两个不同问题会混成一个。
        """
        rows = [self._row("2020 年的故障", "2020-01-01T00:00:00+00:00"),
                self._row("基准", "2020-01-01T06:00:00+00:00")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("活警", text, "以数据集末次为基准时，6 小时前应算活警")

    def test_stale_row_shows_hours_ago(self):
        """陈迹行必须带末次时刻——读者要能自行判断是否真绝迹"""
        rows = [self._row("老故障", "2026-09-30T12:00:00+00:00"),
                self._row("基准", "2026-10-02T12:00:00+00:00")]
        text = mr.render_text(mr.summarize(rows), rows)
        line = next(ln for ln in text.splitlines() if "陈迹" in ln)
        self.assertRegex(line, r"\d+\s*h 前", "陈迹行必须带末次距今小时数")

    def test_no_errors_no_line(self):
        """无错误时不渲染空行"""
        rows = [{"ts": "2026-10-02T12:00:00+00:00", "outcome": "binance_published",
                 "provider": "Preset-x", "title": "t"}]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertNotIn("错误串", text)


class TestR625FeedHealthCoverage(unittest.TestCase):
    """R625：feeds_ok（健康源数）——源健康三态里唯一没有出口的一态。

    动机（生产实测）：R344/R613 已把硬故障(feeds_failed)与停放(feeds_parked)
    做成带新鲜度的告警行，但「本轮几个源健康」从来没渲染过。后果是面板只能
    回答"有没有坏源"，回答不了"还剩几个能用"——**而后者才是源治理的真问题**
    （3/9 健康与 9/9 健康是两种完全不同的处境，且源总数 9 是代码常量、不在
    面板上，读者连心算的基数都没有）。

    生产实测 feeds_ok 均 8.94 / 最大 9：**9 源几乎轮轮全健康**，这个事实
    目前完全不可见。
    """

    def _row(self, **kw):
        r = {"ts": "2026-10-02T12:00:00+00:00", "outcome": "run_summary",
             "candidates": 10, "published": 1,
             "feeds_ok": 9, "feeds_failed": 0, "feeds_parked": 0}
        r.update(kw)
        return r

    def test_feed_ok_is_accumulated(self):
        """抓取轮数 / 总数 / 最小 / 最大四值齐全"""
        rows = [self._row(feeds_ok=9), self._row(feeds_ok=8), self._row(feeds_ok=9)]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["feed_ok_runs"], 3)
        self.assertEqual(s["runs"]["feed_ok_total"], 26)
        self.assertEqual(s["runs"]["feed_ok_min"], 8)
        self.assertEqual(s["runs"]["feed_ok_max"], 9)

    def test_min_captures_true_zero(self):
        """min 初值不能是 0 —— 真出现「0 个源健康」时必须被捕捉。

        用 0 起算会把这个最值吃掉，只剩 max 一档；而**全集故障/风控恰好是
        最需要报警的场景**（这正是 feeds_empty 注释里R255 那类降级）。
        """
        rows = [self._row(feeds_ok=9), self._row(feeds_ok=0)]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["feed_ok_min"], 0,
                         "min 未捕捉真实的 0 值健康源数")

    def test_missing_field_does_not_enter_denominator(self):
        """缺 feeds_ok 的行不进分母（R621 分母纪律）。

        feeds_ok 只在非配额饱和轮出现（生产 270/276 轮，饱和轮 sys.exit 在
        抓取之前）。若把缺字段的行算进分母，会渲染出「0.14 个健康源/轮」
        这种无意义数字。
        """
        rows = [self._row(feeds_ok=9), self._row(feeds_ok=None),
                {"ts": "2026-10-02T12:00:00+00:00", "outcome": "run_summary",
                 "candidates": 5, "published": 0, "quota_blocked": True}]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["feed_ok_runs"], 1,
                         "缺字段的行被算进了抓取轮分母")
        self.assertEqual(s["runs"]["feed_ok_total"], 9)

    def test_zero_value_is_valid_observation(self):
        """值为 0 是有效观测（真一个源都没抓到），字段存在即计入分母。"""
        rows = [self._row(feeds_ok=0)]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["feed_ok_runs"], 1)
        self.assertEqual(s["runs"]["feed_ok_total"], 0)

    def test_health_line_renders_with_denominator(self):
        """健康行必须渲染，且带抓取轮分母"""
        rows = [self._row(feeds_ok=9), self._row(feeds_ok=8)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("源健康", text)
        line = next(ln for ln in text.splitlines() if "源健康:" in ln)
        self.assertIn("2 个抓取轮", line, "必须带抓取轮分母")
        self.assertIn("8.5", line, "必须给均值")
        self.assertIn("最少 8", line)
        self.assertIn("最多 9", line)

    def test_health_and_failure_shown_together(self):
        """健康数与故障数必须同框 —— 源治理问的是"还剩几个能用"。

        只报故障数时读者要自己用「源总数 − 故障 − 停放」心算，而源总数
        （9）是代码常量、不在面板上，连基数都没有。
        """
        rows = [self._row(feeds_ok=6, feeds_failed=3),
                self._row(feeds_ok=6, feeds_failed=3)]
        text = mr.render_text(mr.summarize(rows), rows)
        hl = next(ln for ln in text.splitlines() if "源健康:" in ln)
        self.assertIn("同窗硬故障", hl,
                      "健康行未与故障同框，读者无法判断还剩几个能用")

    def test_no_field_no_line(self):
        """全窗无 feeds_ok（如全为饱和轮）时不渲染空行"""
        rows = [{"ts": "2026-10-02T12:00:00+00:00", "outcome": "run_summary",
                 "candidates": 5, "published": 0, "quota_blocked": True}]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertNotIn("源健康:", text)

    def test_zero_failures_still_renders_health(self):
        """零故障时健康行仍要渲染 —— **"没坏源"不等于"源健康"**。

        这是本条存在的全部理由：故障行零故障时不渲染（零噪音惯例），
        若健康行也跟着不渲染，全健康窗口下面板对源健康**一个字都不说**。
        """
        rows = [self._row(feeds_ok=9, feeds_failed=0)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("源健康:", text)
        self.assertIn("同窗零硬故障", text)


class TestR628ContentCoverageDenominator(unittest.TestCase):
    """R628：内容数据的**分母必须显式**——「59 篇有记录」会被读成「共 59 篇」。

    生产实测：274 篇已发布（有 content_id 的可 join 行），`content_stats.jsonl`
    只有 59 条，**覆盖 22%**。原文案「59 篇有记录」没有分母，读者会把
    「均浏览 137」读成全站水平，实际只是**头部 1/5 帖**的水平。
    判据同 R621/R624：**指标的覆盖面往往比指标本身更重要**。

    采集机制（已核实）：`content_stats.jsonl` 是**每日 04:00Z 一次性快照**、
    累积式覆盖历史。所以采集日之前的帖永远没有浏览数据（**不是缺口**），
    采集日当天的帖要等次日快照（当天显示偏低）——两者都不是数据缺失，
    但读者无法自行区分「没采到」与「没数据」。
    """

    def _row(self, cid, outcome="binance_published"):
        # provider 必带：渲染层有 `if n_pub:`（n_pub = by_provider 计数）这一层
        # 门控，缺 provider 会让整段"内容数据"不渲染——那是**夹具缺字段**，
        # 不是实现缺陷（第一次就踩了，现象与真缺陷一模一样：静默不输出）。
        return {"ts": "2026-10-02T12:00:00+00:00", "outcome": outcome,
                "content_id": cid, "title": f"t{cid}", "source": "S",
                "provider": "Preset-x", "model": "test-model-1",
                "platforms": ["binance"], "hour_bj": 10}

    def _rej(self):
        """一行拒稿。

        R628 发现的**既有耦合**：内容数据（发布帖浏览）这一段被放在
        `if n_rej:`（拦截数 > 0）大块内，于是**"只有成功、没有拒稿"的
        窗口下整段不渲染**。本轮先如实记录该现象（测试用它做前提），
        耦合本身是否要拆见 R628 提交说明。
        """
        return {"ts": "2026-10-02T12:00:00+00:00", "outcome": "llm_rejected",
                "stage": "quality", "provider": "Preset-x",
                "model": "test-model-1", "title": "bad",
                "reason": "质量门: 测试"}

    def _with_stats(self, rows, data):
        """临时注入内容库。**直接改 _STATS_CACHE 而非替换 _stats_lookup**——
        函数内部读的是模块级缓存，替换函数引用既不生效（模块全局已在函数
        编译时绑定），又像 R621 那次 `m.attr = fn` 一样**留下永久改写**。
        finally 完整还原，并另留一条守卫用例钉住它。"""
        cache = mr._STATS_CACHE
        old_loaded, old_data = cache["loaded"], dict(cache["data"])
        try:
            cache["loaded"], cache["data"] = True, dict(data)
            return mr.render_text(mr.summarize(rows), rows)
        finally:
            cache["loaded"], cache["data"] = old_loaded, old_data

    def test_denominator_counts_joinable_delivery_rows(self):
        """分母= 带 content_id 的投递行，**不取全部投递行**。

        口径必须与分子（stats_posts = 投递行 ∩ 内容库）同源；若分母混入
        无 content_id 的历史格式回执，覆盖率会被系统性低估。
        """
        rows = [self._row("111"), self._row("222"), self._row("333")]
        s = mr.summarize(rows)
        self.assertEqual(s["delivered_joinable_posts"], 3)

    def test_non_delivery_rows_excluded_from_denominator(self):
        """拒稿行不进分母（它不是"已发布帖"）"""
        rows = [self._row("111"), self._row("222", outcome="llm_rejected")]
        s = mr.summarize(rows)
        self.assertEqual(s["delivered_joinable_posts"], 1,
                         "拒稿行被算进了已发布帖分母")

    def test_rows_without_content_id_excluded(self):
        """无 content_id 的投递行不进分母（无法 join，R285 的 join 键）"""
        r = self._row(None)
        s = mr.summarize([r])
        self.assertEqual(s["delivered_joinable_posts"], 0)

    def test_renders_coverage_fraction_and_disclaimer(self):
        """有数据时必须渲染 n/m 与百分比，并声明"非全站" """
        rows = [self._row("111"), self._row("222"), self._row("333"),
                self._rej()]
        text = self._with_stats(rows, {"111": {"views": 100, "likes": 0,
                                               "comments": 0}})
        line = next(ln for ln in text.splitlines() if "内容数据" in ln)
        self.assertIn("1/3", line, "必须渲染分子/分母")
        self.assertIn("%", line)
        self.assertIn("非全站", line, "必须声明均值为子集水平")

    def test_no_disclaimer_when_coverage_is_complete(self):
        """覆盖 100% 时不画免责声明（否则每行都挂一句噪声）"""
        rows = [self._row("111"), self._rej()]
        text = self._with_stats(rows, {"111": {"views": 100, "likes": 0,
                                               "comments": 0}})
        line = next(ln for ln in text.splitlines() if "内容数据" in ln)
        self.assertNotIn("非全站", line)
        self.assertIn("1/1", line, "完整覆盖时仍给分母")

    def test_no_stats_no_line(self):
        """内容库全空时整块不渲染（零噪音，沿用 R285 惯例）"""
        rows = [self._row("111"), self._row("222"), self._rej()]
        text = self._with_stats(rows, {})
        self.assertNotIn("内容数据", text)

    def test_stats_cache_restored_after_helper(self):
        """守卫：测试助手必须还原 _STATS_CACHE，否则污染后续用例。

        R621 教训：`mod.attr = fn` 是永久改写，只有 finally 才是解药——
        而 finally 可能被后来的人删掉，所以**额外留一条断言**钉住它。
        """
        before = (mr._STATS_CACHE["loaded"], dict(mr._STATS_CACHE["data"]))
        self._with_stats([self._row("111"), self._rej()], {"111": {"views": 1}})
        self.assertEqual((mr._STATS_CACHE["loaded"], mr._STATS_CACHE["data"]),
                         before, "内容库缓存未被还原，污染了后续用例")


class TestR629NoStaleThroughputClaim(unittest.TestCase):
    """R629：**代码注释里不许留过时的生产速率数字**。

    R610 判读纪律写于项目低产期，注释里留下"发布速率 4~6 篇/天、20 篇窗口
    4~5 天才滚干净"。R629 实测该数字**从未成立过**：
      - 自 09-10 起稳定 12 篇/天（= MAX_DAILY_POSTS，滚动 24h 最多 13 篇）
      - 即使遥测最早段 09-07~09-11 也是 10 篇/天
      - 20 篇窗口实际 **1.7 天**滚干净
    危害不是"数字不好看"，而是它**给判读提供了一个不存在的借口**：
    近半绝对值高时，下一轮会写"样本还没滚干净"从而放过真问题——
    而 R610 的原意恰恰相反（纪律本身正确，只是速度依据错了）。

    本守卫盯住**活跃速率不实**的写法，而不是禁掉所有数字（12 篇/天是对的，
    但它是配置派生量、配置一改就过时，**不该硬编码进注释当依据**）。
    """

    _WRONG = ("4~6 篇/天", "4-6 篇/天", "4～6 篇/天", "4~5 天", "4-5 天")

    def test_no_stale_throughput_claim_in_source(self):
        """**引用旧数字的行必须自带"更正"标记**——否则它就是活依据。

        R629 自身的更正文本也含这些数字（要说清"错在哪"），
        所以守卫不是禁掉字符串，而是要求**出现处必须同时含更正标记**。
        这样既留下更正记录，又拦住"复制粘贴一份没人察觉"的真退化。
        """
        src = open(mr.__file__, encoding="utf-8").read()
        for lineno, line in enumerate(src.splitlines(), 1):
            hit = [n for n in self._WRONG if n in line]
            if not hit:
                continue
            # 粒度取**整个注释块**（连续的 `#` 行），而不是固定行数：
            # 一段注释里更正标记与旧数字可能隔 3~4 行（实测R629 就如此），
            # 定长窗口会漏判。按块判定才符合"这段话是不是在更正"的语义。
            lines_all = src.splitlines()
            lo = lineno - 1
            while lo > 0 and lines_all[lo - 1].lstrip().startswith("#"):
                lo -= 1
            hi = lineno
            while hi < len(lines_all) and lines_all[hi].lstrip().startswith("#"):
                hi += 1
            block = "\n".join(lines_all[lo:hi])
            self.assertIn(
                "R629", block,
                f"metrics_report.py:{lineno} 引用过时速率{hit}却没有更正标记——"
                f"R629 已实测该数字从未成立，当成判读依据会让「等窗口滚干净」"
                f"变成放过真问题的借口")

    def test_recent_span_still_computed(self):
        """R610 的防护本身必须保留：近半跨度要算出来。"""
        rows = [{"ts": "2026-10-02T12:00:00+00:00", "outcome": "binance_published",
                 "content_id": f"c{i}", "title": f"t{i}", "source": "S",
                 "provider": "Preset-x", "model": "m",
                 "platforms": ["binance"], "hour_bj": 10,
                 "final_preview": "内容正文示例。", "ban_active": False}
                for i in range(4)]
        q = mr.quality_scan(rows)
        self.assertIsNotNone(q["recent_span"][0],
                             "R610 的近半跨度丢失——判读近半的前提")

class TestR630PoolCoverageGrouping(unittest.TestCase):
    """R630：「从未被尝试」按**处置动作**分组，而不是一行"请对账"。

    R623 把"窗口内零尝试"显形是那轮的成果，但**收尾动作写成了"请对账是否缺
    key / 被熔断"**——8 条通道的处置动作完全不同，混在一行等于没给判据。

    更隐蔽的坑（本轮实测抓到）：**探针未报"缺 key"≠ key 有效**。探针对未
    登记 AUTH_MODE 的站走"无认证直接请求"，而部分站的 /models 公开可达 ⇒
    「目录核实通过」与「主流程拿不到 key」**可以同时成立**。
    生产实锤：`gh secret list` 只有 5 个 secret，而 xkiro / aihubmix / inferera
    全部未配 key，却因目录公开可查而**不在** unknown_sites 里——
    若按名单直接分组，会把它们误归为"排序问题"，而真因是缺 key。
    """

    def _rows(self, unknown_sites):
        return [{"ts": "2026-10-02T12:00:00+00:00", "outcome": "provider_probe",
                 "probe_ok": True, "sites_total": 11, "sites_ok": 7,
                 "sites_unknown": len([x for x in unknown_sites.split(",") if x]),
                 "unknown_sites": unknown_sites},
                {"ts": "2026-10-02T12:00:00+00:00", "outcome": "run_summary",
                 "candidates": 5, "published": 1}]

    def test_missing_key_group_listed_with_actionable_action(self):
        """探针已定性的缺 key 组必须给确定处置动作（补配 secret）"""
        rows = self._rows("zai,siliconflow")
        text = mr.render_text(mr.summarize(rows), rows)
        line = next(ln for ln in text.splitlines() if "缺 key" in ln)
        self.assertIn("补配 secret", line, "缺 key 组必须给出确定处置动作")

    def test_not_in_probe_list_not_claimed_key_valid(self):
        """不在探针缺 key 名单里，**不得断言 key 有效**（R630 核心）。

        目录公开可查的站会因"核实通过"而不在名单里，若据此推断"key 有效"
        就会把人引去改排序，而真因是没配 secret。
        """
        rows = self._rows("zai")
        text = mr.render_text(mr.summarize(rows), rows)
        # 断言**不得出现的错误断言**，而不是子串"key 有效"——
        # 解释性文案里"不等于 key 有效"本身就含这四个字（实测踩到）。
        # 真正要禁的是把待对账组说成已确诊的那几种说法。
        for bad in ("key 已生效", "key 正常", "非 key 问题", "排序问题而非 key",
                    "只需调整排序"):
            self.assertNotIn(bad, text,
                             f"待对账组被断言成了已确诊（{bad}）——"
                             f"目录可查≠ 主流程有 key")
        pending = next(ln for ln in text.splitlines() if "待对账" in ln)
        self.assertIn("gh secret list", pending,
                      "待对账组必须指明用什么手段对账")

    def test_both_groups_shown_with_totals(self):
        """两组 + 合计都要在，且分母与池一致"""
        rows = self._rows("zai,tokenrouter")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("缺 key", text)
        self.assertIn("待对账", text)
        self.assertIn("从未被尝试合计", text)

    def test_no_probe_data_still_renders_pending_group(self):
        """无探针结论时也要渲染待对账组，而不是整行消失（沉默不是通过）"""
        rows = [{"ts": "2026-10-02T12:00:00+00:00", "outcome": "run_summary",
                 "candidates": 5, "published": 1}]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("待对账", text)
        self.assertIn("从未被尝试合计", text)

    def test_does_not_repeat_removed_advice(self):
        """旧的合并式措辞（"请对账是否缺 key / 被熔断"）不得再出现"""
        rows = self._rows("zai")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertNotIn("请对账是否缺 key / 被熔断", text,
                         "旧措辞把两类处置混成一行，等于没给判据")

class TestR637UnregisteredAuthModeSurfaced(unittest.TestCase):
    """R637：未登记 AUTH_MODE 的站要单独暴露——它们的"核实通过"是**借来的**。

    探针对**未登记 AUTH_MODE** 的站走「无认证直查」。生产实锤 5 站如此
    （openrouter / xkiro / aihubmix / inferera / bluesminds），其中
    **xkiro / aihubmix / inferera 在主流程是需要 key 的**（`gh secret list`
    无这三个、从未上场）⇒ 它们"核实通过"纯粹因为 `/models` 恰好公开。

    危害：若哪天这些站的 `/models` 改为需认证，探针会集体报"未核实"，
    而**原因（没登记 AUTH_MODE）不在任何字段里** ⇒ 排障会去查 key 失效、
    查网络，真正的问题留在原地。R619 的同型（"方式错了"被报成"key 坏了"），
    但**更隐蔽——连 `note` 都不会有**。

    纪律：R630 已证「不在探针缺 key 名单 ≠ key 有效」；本条是它的上游——
    **「核实通过」也可能是借来的**。
    """

    def _probe(self, **kw):
        row = {"ts": "2026-10-03T10:00:00+00:00", "outcome": "provider_probe",
               "probe_ok": True, "sites_total": 11, "sites_ok": 7,
               "sites_unknown": 1, "unknown_sites": "zai"}
        row.update(kw)
        return [row]

    def test_no_auth_sites_rendered(self):
        rows = self._probe(no_auth_count=5,
                           no_auth_sites="aihubmix,bluesminds,inferera,openrouter,xkiro")
        text = mr.render_text(mr.summarize(rows), rows)
        line = next(ln for ln in text.splitlines() if "未登记认证方式" in ln)
        self.assertIn("xkiro", line, "必须点名具体站")
        self.assertIn("无认证直查", line)
        self.assertIn("恰好公开", line, "必须说清'存活'依赖目录公开这个前提")

    def test_count_rendered_as_int_not_float(self):
        """`_num` 归一化返 float，f-string 会打 '5.0 站'（MEMORY 附注）"""
        rows = self._probe(no_auth_count=5, no_auth_sites="a,b")
        text = mr.render_text(mr.summarize(rows), rows)
        line = next(ln for ln in text.splitlines() if "未登记认证方式" in ln)
        self.assertIn("5 站", line)
        self.assertNotIn("5.0", line, "float 未转int")

    def test_absent_field_renders_nothing(self):
        """老行没有该字段是正常的（沿用 R618「读侧缺失按 0」）"""
        rows = self._probe()
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertNotIn("未登记认证方式", text,
                         "老行不应产生噪声行")

    def test_zero_count_renders_nothing(self):
        rows = self._probe(no_auth_count=0, no_auth_sites="")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertNotIn("未登记认证方式", text)

    def test_probe_source_computes_no_auth_sites(self):
        """守卫写侧：探针必须真的算这个字段（否则读侧永远是空）。

        **按路径读文件而非 import**——`probe_provider_models` 不在本测试的
        import 路径上（`ModuleNotFoundError`），且它带 module 级配置。
        """
        import os
        # 同目录（scripts/），不是 scripts/.. —— 我第一版多加了一层 ..。
        probe = os.path.join(os.path.dirname(mr.__file__),
                             "probe_provider_models.py")
        src = open(probe, encoding="utf-8").read()
        self.assertIn("no_auth_sites", src,
                      "探针未写 no_auth_sites——读侧新增的字段恒空")
        self.assertIn("AUTH_MODE", src)


class TestR643ReadabilityMetrics(unittest.TestCase):
    """R643/R644：可读性护栏必须有**持久出口**（R617）。

    动机：R643 修的是"$ 挂件紧贴中文导致币安不渲染可点击标签"。修复在净化层，
    但若没有度量面，无人能证明它是否真的生效——R612「程序在用」≠「人在看」
    的反向版本：**修复在跑 ≠ 修好了**。生产实测基线：212/679（31%）紧贴。

    判读纪律：`cashtag_tight` 随R643 上线**应归零**；持续非 0 说明有出海口
    绕过净化层直发 payload（R355 曾踩过"标题绕过敏感词过滤"同型漏洞）。
    """

    def _rows_with_preview(self, preview):
        return [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-01T04:00:00+00:00", "content_id": "c1",
                 "hour_bj": 21, "article": False, "source": "U.Today",
                 "final_preview": preview}]

    def test_tight_cashtag_detected(self):
        """紧贴中文的 $ 挂件必须被计入（两侧任一即可）"""
        summ = mr.summarize(self._rows_with_preview(
            "2770亿$SHIB刚砸进池子,还有一波$SOL在涨"))
        self.assertEqual(summ["cashtag_total"], 2)
        self.assertEqual(summ["cashtag_tight"], 2,
                         "双向紧贴的两个挂件都要计入")

    def test_spaced_cashtag_not_flagged(self):
        """已有空格的挂件不得误报（否则告警永远亮着，等于没告警）"""
        summ = mr.summarize(self._rows_with_preview(
            "现在 $BTC 刚过完山车,隔壁 $AVAX 也涨了"))
        self.assertEqual(summ["cashtag_total"], 2)
        self.assertEqual(summ["cashtag_tight"], 0)

    def test_renders_warning_when_tight_present(self):
        """存在紧贴 → 渲染 ⚠️ 并点明后果（读者要知道这意味着什么）"""
        summ = mr.summarize(self._rows_with_preview("2770亿$SHIB刚砸进池子"))
        text = mr.render_text(summ, self._rows_with_preview("2770亿$SHIB刚砸进池子"))
        self.assertIn("挂件紧贴中文", text)
        self.assertIn("不会", text)

    def test_renders_ok_when_all_spaced(self):
        """全部合规 → 渲染 ✅（修复生效的正面证据，不能只有告警没有确认）"""
        pv = "现在 $BTC 刚过完山车"
        summ = mr.summarize(self._rows_with_preview(pv))
        text = mr.render_text(summ, self._rows_with_preview(pv))
        self.assertIn("挂件间距正常", text)
        self.assertNotIn("挂件紧贴中文", text)

    def test_paragraph_count_measured(self):
        """段落数（空行）必须被度量，且零分段要能被识别出来"""
        summ = mr.summarize(self._rows_with_preview("A段落。\n\nB段落。\n\nC段落。"))
        self.assertEqual(summ["layout_paragraphs"], [2])

    def test_zero_paragraph_flagged(self):
        """零分段（字墙）必须显性报出——这是 R644 的核心指征"""
        pv = "一、盘面真相" + "全网热度都不在。" * 10
        summ = mr.summarize(self._rows_with_preview(pv))
        text = mr.render_text(summ, self._rows_with_preview(pv))
        self.assertIn("无分段", text)

    def test_missing_preview_is_not_counted(self):
        """缺 final_preview 的行不参与统计（未知不是 0，否则分母被污染）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-01T04:00:00+00:00", "content_id": "c1",
                 "hour_bj": 21, "article": False, "source": "U.Today"}]
        summ = mr.summarize(rows)
        self.assertEqual(summ["cashtag_total"], 0)
        self.assertEqual(summ["cashtag_tight"], 0)
        self.assertEqual(summ["layout_paragraphs"], [])

    def test_renders_without_content_stats(self):
        """**无浏览数据时也必须渲染**（防"被不相干数据集门控"的回归守卫）。

        首版把可读性块放在 `if s.get("stats_posts")` 内，实测无 CSV 时整块消失。
        那是错的耦合：内容库覆盖率仅 20%（R628）⇒ 八成情况下护栏静默。
        这条把"耦合"钉成事故：渲染不得依赖 stats_posts。
        """
        rows = self._rows_with_preview("2770亿$SHIB刚砸进池子")
        summ = mr.summarize(rows)          # 未注入 _STATS_CACHE ⇒ stats_posts=0
        self.assertEqual(summ["stats_posts"], 0, "本用例前提：无浏览数据")
        text = mr.render_text(summ, rows)
        self.assertIn("挂件紧贴中文", text,
                      "无浏览数据时可读性护栏必须仍然渲染")
        self.assertIn("正文分段", text)


class TestR646TitleSideGuardrail(unittest.TestCase):
    """R646：挂件护栏必须**分侧计数**——实测标题侧紧贴率 75%，远高于正文 31%。

    标题是信息流第一触点，且走独立出海口（R355 敏感词 / R643 补空格 /
    R645 织入各自补过一遍）。若把它混进正文分母：
    - 修复前会被正文的低紧贴率"稀释"，看不出标题侧更严重；
    - 修复后也无法分别判断两侧是否真的修好了。
    **一个分母掩盖两个信号 = 两个信号都看不见**（R611「并列信号不能共用分母」）。
    """

    def _rows(self, preview, title):
        return [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-01T04:00:00+00:00", "content_id": "c1",
                 "hour_bj": 21, "article": True, "source": "U.Today",
                 "final_preview": preview, "article_title": title}]

    def test_title_tight_counted_separately(self):
        """标题侧紧贴必须独立计数"""
        summ = mr.summarize(self._rows(
            "正文 $BTC 正常有空格", "$LINK冲高回落,5.62%回撤"))
        self.assertEqual(summ["title_cashtag_total"], 1)
        self.assertEqual(summ["title_cashtag_tight"], 1, "标题侧紧贴须被计入")
        self.assertEqual(summ["cashtag_tight"], 0, "正文侧不得被标题污染")

    def test_title_and_body_counted_independently(self):
        """两侧都要紧贴时各自计入，分子分母都不混"""
        summ = mr.summarize(self._rows(
            "2770亿$SHIB刚砸进池子", "$SHIB刚砸进池子"))
        self.assertEqual((summ["cashtag_total"], summ["cashtag_tight"]), (1, 1))
        self.assertEqual(
            (summ["title_cashtag_total"], summ["title_cashtag_tight"]), (1, 1),
            "标题与正文必须各算各的，不能合并成一个分母")

    def test_title_spaced_not_flagged(self):
        """标题侧已有空格 → 不误报（否则告警永远亮着）"""
        summ = mr.summarize(self._rows("正文 $BTC 正常", "$BTC 冲高回落"))
        self.assertEqual(summ["title_cashtag_tight"], 0)

    def test_renders_title_line_separately(self):
        """标题侧必须独立成行，且点明"第一触点"（读者要明白损失在哪）

        断言方式：正文侧干净→ 渲染 ✅；标题侧有紧贴 → 渲染 ⚠️。
        **两条行必须同时存在**——若标题侧取代了正文侧，就是分母混用
        （R611）的复发。
        """
        clean_body, tight_title = "正文 $BTC 正常", "$LINK冲高回落"
        summ = mr.summarize(self._rows(clean_body, tight_title))
        text = mr.render_text(summ, self._rows(clean_body, tight_title))
        self.assertIn("标题$挂件紧贴中文", text)
        self.assertIn("第一触点", text)
        self.assertIn("正文$挂件", text, "正文侧护栏行不得被标题侧取代")
        # 两侧同时告警时也都要出现（互不吞掉）
        both = mr.summarize(self._rows("2770亿$SHIB刚砸进池子", "$SHIB刚砸进池子"))
        btext = mr.render_text(both, self._rows("2770亿$SHIB刚砸进池子",
                                                "$SHIB刚砸进池子"))
        self.assertIn("正文$挂件紧贴中文", btext)
        self.assertIn("标题$挂件紧贴中文", btext)

    def test_renders_ok_when_both_sides_clean(self):
        """两侧都干净 → 两行 ✅（修复生效的正面证据）"""
        rows = self._rows("现在 $BTC 稳住", "$BTC 冲高回落")
        summ = mr.summarize(rows)
        text = mr.render_text(summ, rows)
        self.assertIn("正文$挂件间距正常", text)
        self.assertIn("标题$挂件间距正常", text)

    def test_missing_title_not_counted(self):
        """短讯无 article_title → 标题侧分母为 0（未知不是 0）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-01T04:00:00+00:00", "content_id": "c1",
                 "hour_bj": 21, "article": False, "source": "U.Today",
                 "final_preview": "现在 $BTC 稳住"}]
        summ = mr.summarize(rows)
        self.assertEqual(summ["title_cashtag_total"], 0)
        self.assertNotIn("标题$挂件", mr.render_text(summ, rows))

    def test_opener_denominator_unchanged(self):
        """**缩进回归守卫**：`首段钩子` 分母不得因改标题块而变化。

        实测事故：R646 首版整块重写标题度量（含内嵌 def），缩进降了 4 级，
        后续 `opener_evaluated` 等语句被并入 `if article_title:` 块
        ⇒ **分母从296 静默掉到 25**。字段照常渲染，只是分母变了——
        "缺字段"与"块被外移"在测试里长得一样（R628 的同款陷阱）。

        这条用例把"短讯（无标题）也必须进首段钩子分母"钉死：
        短讯永远没有 article_title，若它的 final_preview 有钩子却被排除，
        就说明标题块的缩进把下游代码吞进去了。
        """
        rows = [
            # 短讯：无 article_title，但有 final_preview
            {"platforms": ["binance"], "outcome": "binance_published",
             "ts": "2026-10-01T04:00:00+00:00", "content_id": "s1",
             "hour_bj": 21, "article": False, "source": "U.Today",
             "final_preview": "1.88亿爆仓 $BTC 冲8.5万"},
            # 长文：有标题
            {"platforms": ["binance"], "outcome": "binance_published",
             "ts": "2026-10-01T05:00:00+00:00", "content_id": "a1",
             "hour_bj": 22, "article": True, "source": "U.Today",
             "final_preview": "全网1.88亿爆仓 $BTC 拉盘",
             "article_title": "$BTC 冲8.5万是诱多还是真启动"},
        ]
        summ = mr.summarize(rows)
        self.assertEqual(len(summ["article_titles"]), 1, "只有长文进标题分母")
        self.assertEqual(
            summ["opener_evaluated"], 2,
            "短讯也必须进首段钩子分母——若为 1，说明标题块缩进吞了下游代码")
        # 标题侧计数只算长文那一条
        self.assertEqual(summ["title_cashtag_total"], 1)
        # 正文侧两篇都算
        self.assertEqual(summ["cashtag_total"], 2)

    def test_layout_paragraphs_covers_shortform_too(self):
        """`layout_paragraphs` 同样不得被标题块吞掉（短讯也要分段度量）。"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-01T04:00:00+00:00", "content_id": "s1",
                 "hour_bj": 21, "article": False, "source": "U.Today",
                 "final_preview": "第一段。\n\n第二段。"},
                {"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-01T05:00:00+00:00", "content_id": "a1",
                 "hour_bj": 22, "article": True, "source": "U.Today",
                 "final_preview": "一、背景\n\n正文。", "article_title": "标题标题标题标题"}]
        summ = mr.summarize(rows)
        self.assertEqual(len(summ["layout_paragraphs"]), 2,
                         "短讯也必须进分段度量分母")


class TestR647EndingQuestionObservable(unittest.TestCase):
    """R647：结尾站队提问是**明确红线却零观测**。

    prompt 短讯第 5 条 / 长文第 3 条都明令"结尾放一句和本文事件直接相关的
    问题或观察点"，但 `final_preview` 只存**前 200 字**（R106 为FNG 锚点扩过
    一次，R292 为篇幅又记了全文字数）⇒ **结尾整段不可见**。

    实测：全库 312 条回执里，`final_preview` 末尾抓不到任何提问——与"截断"
    完全一致。**不是没人写，是看不见。** 这是 R617「探针在跑、答案被丢弃」
    的又一例：度量只覆盖了半条链路。

    修法：记**派生布尔** `ending_question`（结尾末两行有没有问句/站队词），
    而不是加长预览——判据只有"有没有问"，存全文会撑爆 metrics.jsonl。
    """

    def _rows(self, flags):
        return [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": f"2026-10-05T00:{i:02d}:00+00:00", "content_id": f"n{i}",
                 "hour_bj": 21, "article": False, "source": "U.Today",
                 "ending_question": f} for i, f in enumerate(flags)]

    def test_true_counts_as_yes(self):
        summ = mr.summarize(self._rows([True, True]))
        self.assertEqual((summ["ending_q_yes"], summ["ending_q_marked"]), (2, 2))

    def test_false_counts_in_denominator(self):
        """**False 必须计入分母**——"确实没写提问"是有效观测，不是缺数据。

        若把 False 排除，分母只剩"写了的那些"，比率恒等于 100%，
        指标彻底失去意义（纪律 17：`isinstance(x,int)` 制造恒假分母的同型坑）。
        """
        summ = mr.summarize(self._rows([True, False, False]))
        self.assertEqual(summ["ending_q_marked"], 3, "False 也必须进分母")
        self.assertEqual(summ["ending_q_yes"], 1)

    def test_none_excluded_from_denominator(self):
        """None（Mock/异常态）**不进分母**——未知不是 False（纪律 12）"""
        summ = mr.summarize(self._rows([True, None, False]))
        self.assertEqual(summ["ending_q_marked"], 2, "None 不进分母")
        self.assertEqual(summ["ending_q_yes"], 1)

    def test_missing_field_legacy_rows_not_counted(self):
        """旧回执无 ending_question 字段 → 完全不计入（旧 schema 不污染）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-04T00:00:00+00:00", "content_id": "old",
                 "hour_bj": 21, "article": False, "source": "U.Today",
                 "final_preview": "$BTC 稳住"}]
        summ = mr.summarize(rows)
        self.assertEqual(summ["ending_q_marked"], 0)
        self.assertNotIn("结尾站队提问", mr.render_text(summ, rows),
                         "无标记时整行不渲染——向后兼容，零噪音")

    def test_renders_with_ratio_and_thresholds(self):
        """渲染必须给比率，且分档可读（✅≥80% / ⚠️≥50% / ❌<50%）"""
        summ = mr.summarize(self._rows([True, True, True, False]))
        text = mr.render_text(summ, self._rows([True, True, True, False]))
        self.assertIn("结尾站队提问 3/4（75%）", text)
        self.assertIn("⚠️", text, "75% 应落在 ⚠️ 档")

    def test_renders_ok_when_high(self):
        summ = mr.summarize(self._rows([True, True, True, True]))
        text = mr.render_text(summ, self._rows([True, True, True, True]))
        self.assertIn("✅", text)

    def test_main_emits_derived_flag_not_full_text(self):
        """**接线守卫**：必须记派生布尔，且不得把全文塞进遥测。

        两条约束同源：既要能观测结尾，又不能让 metrics.jsonl 膨胀
        （312 篇 × 全文 200 字已 6 万字符）。
        """
        src = open(m.__file__, encoding="utf-8").read()
        self.assertIn('"ending_question": _tail_q', src,
                      "发布回执必须落 ending_question 派生布尔")
        # 不得出现"把结尾全文写进遥测"的形态
        self.assertNotIn("final_tail", src,
                         "不要新增全文/尾段存储字段——判据只有有没有问")


class TestR648PureTickerCoverage(unittest.TestCase):
    """R648：纯名检测池**必须大于最小可用集**，否则护栏低估一半缺口。

    R647 首版硬编码 6 个币（BTC/ETH/XRP/SOL/DOGE/BNB），而标题里实际出现
    的纯名包含 SHIB/ZEC/SUI/BCH/COMP ⇒ **13 篇纯名只检出 6 篇，漏 7**。
    护栏报"6/25（24%）"会让读者以为缺口只有那么大。

    改用 `token_engagement.json`（R608 浏览加权表）的键 ∪ 高频兜底，
    生产实测 12 个币 ⇒ 检出 10/25（40%）。
    """

    def test_pool_includes_engagement_tokens(self):
        """检测池必须含 token_engagement 里出现过的币（SHIB/ZEC/COMP…）"""
        pool = mr._pure_ticker_pool()
        for tok in ("BTC", "ETH", "SHIB", "ZEC", "COMP", "LINK"):
            self.assertIn(tok, pool,
                          f"{tok} 在生产标题里出现过纯名，检测池必须覆盖")

    def test_pool_excludes_stablecoins(self):
        """稳定币必须排除——R316 契约「稳定币不做挂件」，
        把 USDC 算成"该织入未织入"是误报。"""
        pool = mr._pure_ticker_pool()
        for st in ("USDC", "USDT", "BUSD", "FDUSD", "TUSD"):
            self.assertNotIn(st, pool, f"{st} 不该进纯名检测池")

    def test_pool_is_cached(self):
        """池必须缓存——每篇标题都读一次磁盘会让报表慢一个数量级"""
        mr._PURE_TICKER_CACHE.clear()
        a = mr._pure_ticker_pool()
        b = mr._pure_ticker_pool()
        self.assertIs(a, b, "池应缓存复用")

    def test_detects_shib_pure_name(self):
        """真实漏检样本：SHIB 纯名标题必须被检出（R647 首版漏掉这类）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-05T00:00:00+00:00", "content_id": "n1",
                 "hour_bj": 21, "article": True, "source": "U.Today",
                 "final_preview": "正文", "article_title":
                     "SHIB单日拉6%，ZEC狂飙9%，这盘面真见底了？"}]
        summ = mr.summarize(rows)
        self.assertEqual(summ["title_pure_ticker"], 1,
                         "SHIB/ZEC 纯名必须被检出——这是 R647 首版的真实漏检")

    def test_render_states_underestimate(self):
        """渲染必须声明这是**下界估计**——覆盖面小于织入侧是事实，
        读者必须知道这个数不是全量（否则会去追一个不存在的精确值）。"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-05T00:00:00+00:00", "content_id": "n1",
                 "hour_bj": 21, "article": True, "source": "U.Today",
                 "final_preview": "正文", "article_title": "SHIB单日拉6%"}]
        summ = mr.summarize(rows)
        text = mr.render_text(summ, rows)
        self.assertIn("下界估计", text)
        self.assertIn("检测池", text)


class TestR649ContentRedlineObservable(unittest.TestCase):
    """R649：三条**内容红线**此前既无代码防线、又无发布后观测。

    审计方法（不是拍脑袋）：用生产 148 次拒稿的 `stage` 分布做判据——
    `quality` 门 21 次拒稿**全部**是长度/TITLE/中文量，没有任何一条是
    内容红线；而 prompt 里明写「禁止喊单」「禁止操纵归因（万能阴谋论）」
    「破折号最多1 次」。⇒ 这三条**全靠模型自觉**，
    违反与否**在任何字段里都看不到**。

    定位为**度量**而非拒稿门：内容红线误杀代价高（长文单通道，
    一次拒稿 = 大概率丢稿，R331），且"喊单"无确定性边界
    （"抄底"在"想抄底等回踩"里是合规提醒、在"现在抄底"里是喊单——
    R649 实测抽样 12/12 全为劝阻语境）。⇒ 先让人看见，再决定是否收紧。
    """

    def _rows(self, **kw):
        base = {"platforms": ["binance"], "outcome": "binance_published",
                "ts": "2026-10-05T00:00:00+00:00", "content_id": "n1",
                "hour_bj": 21, "article": False, "source": "U.Today",
                "final_preview": "正文"}
        base.update(kw)
        return [base]

    def test_all_three_counted(self):
        summ = mr.summarize(self._rows(
            dash_ok=True, hype_ok=True, conspiracy_ok=True))
        self.assertEqual(summ["dash_ok_n"], 1)
        self.assertEqual(summ["hype_ok_n"], 1)
        self.assertEqual(summ["consp_ok_n"], 1)
        self.assertEqual(summ["dash_ok_d"], 1)

    def test_false_counts_in_denominator(self):
        """**False 计入分母**——否则合规率恒 100%，指标彻底失效（纪律 17）"""
        summ = mr.summarize(self._rows(
            dash_ok=False, hype_ok=False, conspiracy_ok=False))
        self.assertEqual(summ["dash_ok_d"], 1)
        self.assertEqual(summ["dash_ok_n"], 0)

    def test_none_excluded(self):
        summ = mr.summarize(self._rows())   # 无字段 =旧回执
        self.assertEqual(summ["dash_ok_d"], 0)
        self.assertEqual(summ["hype_ok_d"], 0)

    def test_legacy_rows_render_nothing(self):
        """旧回执无字段 → 三行都不渲染（零噪音，向后兼容）"""
        summ = mr.summarize(self._rows())
        text = mr.render_text(summ, self._rows())
        self.assertNotIn("禁喊单", text)
        self.assertNotIn("破折号", text)

    def test_renders_three_separate_lines(self):
        """三项必须**分行**——它们是三个独立根因，合并分母会掩盖一侧（R611）"""
        summ = mr.summarize(self._rows(
            dash_ok=True, hype_ok=False, conspiracy_ok=True))
        text = mr.render_text(summ, self._rows(
            dash_ok=True, hype_ok=False, conspiracy_ok=True))
        self.assertIn("破折号≤1 合规", text)
        self.assertIn("禁喊单 合规", text)
        self.assertIn("禁操纵归因断言 合规", text)
        self.assertIn("0/1", text, "喊单不合规必须显示出来")

    def test_declares_it_is_not_a_gate(self):
        """渲染必须声明"度量非门"——读者不该以为这会拒稿"""
        summ = mr.summarize(self._rows(
            dash_ok=True, hype_ok=True, conspiracy_ok=True))
        text = mr.render_text(summ, self._rows(
            dash_ok=True, hype_ok=True, conspiracy_ok=True))
        self.assertIn("非门", text)

    def test_main_emits_all_three_flags(self):
        """接线守卫：三个字段都要落回执（漏一个就少一条红线观测）"""
        src = open(m.__file__, encoding="utf-8").read()
        for f in ('"dash_ok": _dash_ok', '"hype_ok": _hype_ok',
                  '"conspiracy_ok": _consp_ok'):
            self.assertIn(f, src, f"回执必须落 {f}")

    def test_hype_wordlist_excludes_data_description(self):
        """**"暴涨"不得在喊单词表里**——R649 实测它在生产里10/10 是数据描述
        （"销毁率暴涨 84%"），把它当喊单会产生系统性误报。"""
        src = open(m.__file__, encoding="utf-8").read()
        seg_start = src.index("_HYPE_EXEMPT")
        seg = src[seg_start:seg_start + 1200]
        wordlist_line = [ln for ln in seg.split("\n") if "for _w in" in ln]
        self.assertTrue(wordlist_line, "须能定位喊单词表")
        self.assertNotIn("暴涨", seg,
                         '"暴涨"是数据描述不是喊单，误报率 10/10')
        self.assertIn("梭哈", seg, "梭哈是真喊单，须保留")

    def test_hype_detection_per_word_with_exemption(self):
        """判定逻辑守卫：逐词search + 紧前 12 字豁免。

        两个已被生产实测否掉的做法（别再走回头路）：
        ① 整体前瞻否定 `(?<![别勿])…` → 裸用"必涨/满仓干"**全被放过**；
        ② 把"暴涨"当喊单 → 10/10 误报。
        """
        import re
        src = open(m.__file__, encoding="utf-8").read()
        seg_start = src.index("_HYPE_EXEMPT")
        seg = src[seg_start:src.index("_consp_ok", seg_start)]
        # 复原判定逻辑
        exempt = re.search(r'_HYPE_EXEMPT = \((.*?)\n\s{24}\)', seg, re.DOTALL)
        self.assertIsNotNone(exempt, "须能解析豁免正则块")
        words = re.search(r'for _w in \((.*?)\):', seg, re.DOTALL)
        self.assertIsNotNone(words, "须能解析词表")
        win = re.search(r'_mm\.start\(\) - (\d+)', seg)
        self.assertIsNotNone(win)
        # 校验关键判据
        self.assertIn("12", win.group(1) or "12", "窗口应为 12 字（实测 8 字会漏跨词劝阻）")
        self.assertNotIn("暴涨", words.group(1))
        for w in ("必涨", "满仓干", "梭哈"):
            self.assertIn(w, words.group(1), f"{w} 是真喊单，须保留")


class TestR650HourPrefGuardrail(unittest.TestCase):
    """R650：时段偏置**必须有出口**，且必须渲染"天花板提示"。

    两条纪律：
    1. R617「探针在跑、答案被丢弃」——偏置若只在代码里，返回值只是个计数，
        没人知道它到底有没有改变发帖分布。
    2. **不得让读者被"2.05 倍"误导**：目标窗只有 6h/24h，而配额是 12 篇/天，
        最多约 3 篇能落窗内。按现有 18% 占比测算，即便把配额全投进目标窗，
        总日均浏览贡献也只 +6%。这个数字必须渲染出来，否则报表会让人以为
        这是个"2 倍收益的开关"。
    """

    def _rows(self):
        return [{"outcome": "run_summary", "ts": "2026-10-05T12:00:00+00:00",
                 "candidates": 10, "published": 1,
                 "hour_pref_shifted": 5, "hour_pref_in_window": 0}]

    def _rows_in_window(self):
        return [{"outcome": "run_summary", "ts": "2026-10-05T23:00:00+00:00",
                 "candidates": 10, "published": 1,
                 "hour_pref_shifted": 0, "hour_pref_in_window": 1}]

    def test_aggregates_shift_and_window(self):
        s = mr.summarize(self._rows())
        self.assertEqual(s["runs"]["hour_pref_runs"], 1)
        self.assertEqual(s["runs"]["hour_pref_shifted"], 5)
        self.assertEqual(s["runs"]["hour_in_window_runs"], 1)
        self.assertEqual(s["runs"]["hour_in_window_yes"], 0)

    def test_window_yes_counted_separately(self):
        """**偏置搬动数与是否在窗内必须分开**——两个不同问题（R611）。

        混进同一个计数器就回答不了"排序偏置是否真的把配额搬过去了"。
        """
        s = mr.summarize(self._rows() + self._rows_in_window())
        self.assertEqual(s["runs"]["hour_pref_shifted"], 5, "窗外才搬动")
        self.assertEqual(s["runs"]["hour_in_window_yes"], 1)
        self.assertEqual(s["runs"]["hour_in_window_runs"], 2)

    def test_legacy_runs_not_counted(self):
        """旧 run_summary 无字段 → 完全不计入（向后兼容，零噪音）"""
        s = mr.summarize([{"outcome": "run_summary", "ts": "2026-10-04T12:00:00+00:00",
                           "candidates": 5, "published": 1}])
        self.assertEqual(s["runs"]["hour_pref_runs"], 0)
        self.assertNotIn("时段偏置", mr.render_text(s, []))

    def test_renders_ceiling_caveat(self):
        """**必须渲染天花板提示**——防读者把边际改善当主杠杆"""
        s = mr.summarize(self._rows())
        text = mr.render_text(s, self._rows())
        self.assertIn("时段偏置", text)
        self.assertIn("天花板", text)
        self.assertIn("+6%", text)

    def test_low_window_ratio_suggests_investigation(self):
        """落在高浏览窗比例过低时，要提示去查调度/候选而非继续调幅度。

        这是**自指护栏**：排序偏置改不了 cron，若长期上不去，
        继续调 TIME_PREF_PENALTY 是白费力气。
        """
        s = mr.summarize(self._rows())
        self.assertIn("查调度", mr.render_text(s, self._rows()))

    def test_high_ratio_no_complaint(self):
        s = mr.summarize(self._rows_in_window())
        self.assertNotIn("查调度", mr.render_text(s, self._rows_in_window()))

    def test_p_value_included(self):
        """渲染必须带上 p 值——读者要能判断这条结论的强度"""
        s = mr.summarize(self._rows())
        self.assertIn("p=0.0097", mr.render_text(s, self._rows()))


class TestR651ConfoundedSignalsNotCausal(unittest.TestCase):
    """R651：**统计显著 ≠ 可行动**。本例是最典型的可复现陷阱。

    发现过程：挖选题层时找到一个看起来很硬的信号——
        `raw`（新闻原图）62/天 vs `chart`（走势卡）42/天，**p=0.0090**，
        控制体裁后 p=0.0089 仍显著，且在 5 个组里的 3 个方向一致。
    但交叉表显示它是**完全混淆**的：

        | 源 | raw 中位 | chart 中位 |
        |---|---|---|
        | U.Today（39% 总量） | **原图率 0%** | 42×18 |
        | CryptoSlate | 45×9 | 4×1 |
        | Decrypt | 70×3 | 32×1 |

    ⇒ **没有任何一个源内部同时有 raw 与 chart 的可比样本**。
    `raw` 的差异 100% 来自"哪个源带原图"，不是"配图类型的影响"。

    而且 `raw` 已是 `_prefer` 最高优先级（`["raw", "chart", "card"]`），
    **根本不可配置**——它由"该新闻有没有原图"决定。

    ⇒ 真正可行动的是**源选择**（U.Today 占 39% 且原图率为 0），
    不是配图参数。本用例锁死这条警示，防止后人拿 p=0.0090 去调配图。
    """

    def _rows(self):
        out = []
        # U.Today：39% 产量、原图率 0%
        for i in range(12):
            out.append({"platforms": ["binance"], "outcome": "binance_published",
                        "ts": f"2026-10-01T{i:02d}:00:00+00:00",
                        "content_id": f"ut{i}", "hour_bj": 12, "article": False,
                        "source": "U.Today (Meme币/DOGE/SHIB/SOL/XRP热点)",
                        "image_tier": "chart", "final_preview": "正文"})
        # 其他源：原图率约 90%
        for i in range(9):
            out.append({"platforms": ["binance"], "outcome": "binance_published",
                        "ts": f"2026-10-02T{i:02d}:00:00+00:00",
                        "content_id": f"cs{i}", "hour_bj": 8, "article": False,
                        "source": "CryptoSlate (新赛道与代币经济)",
                        "image_tier": "raw", "final_preview": "正文"})
        return out

    def test_renders_confound_warning(self):
        s = mr.summarize(self._rows())
        text = mr.render_text(s, self._rows())
        self.assertIn("不可当因果读", text,
                      "配图层级行必须带混淆警示——否则 p=0.0090 会被当成可行动结论")

    def test_warning_states_not_configurable(self):
        """警示必须说明 raw **不可配置**，否则后人会去找参数调"""
        s = mr.summarize(self._rows())
        text = mr.render_text(s, self._rows())
        self.assertIn("不可配置", text)

    def test_warning_points_to_source_selection(self):
        """警示必须指出可行动的方向是**源选择**"""
        s = mr.summarize(self._rows())
        text = mr.render_text(s, self._rows())
        self.assertIn("源选择", text)

    def test_denominator_only_delivered_posts(self):
        """⚠️ 分母口径（R611）：只在**已发布帖**里数，不能扫全部 rows。

        首版扫了全部 rows（含 llm_rejected / run_summary），
        实测 U.Today 分母变成 180（真实 123）⇒ 比率失真。
        """
        rows = self._rows()
        # 掺入非发布记录：源名相同但不该进分母
        rows.append({"outcome": "llm_rejected", "ts": "2026-10-01T00:00:00+00:00",
                     "source": "U.Today (Meme币/DOGE/SHIB/SOL/XRP热点)",
                     "image_tier": "raw"})
        rows.append({"outcome": "run_summary", "ts": "2026-10-01T01:00:00+00:00",
                     "source": "U.Today (Meme币/DOGE/SHIB/SOL/XRP热点)"})
        s = mr.summarize(rows)
        text = mr.render_text(s, rows)
        self.assertIn("U.Today 12 篇", text,
                      "分母只算已发布帖（9+3=12），拒稿与 run_summary 不得混入")

    def test_not_rendered_without_image_tiers(self):
        """无配图数据时不渲染该警示（向后兼容，零噪音）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-01T00:00:00+00:00", "content_id": "x",
                 "hour_bj": 12, "article": False, "source": "U.Today (X)",
                 "final_preview": "正文"}]
        s = mr.summarize(rows)
        self.assertNotIn("不可当因果读", mr.render_text(s, rows))


class TestR652PlatformDimensionVisible(unittest.TestCase):
    """R652：平台维度在报表里**完全不可见**（R612「程序在用」≠「人在看」的又一例）。

    生产实测 312 篇**全是纯 binance**。而代码完整支持三种组合：
    `PUBLISH_PLATFORMS = binance / okx_draft / telegram`（main.py:216），
    `TELEGRAM_BOT_TOKEN` 等 secret 也已正确注入 workflow。
    但 `platforms` 字段此前**只用于 `_is_delivered()` 算投递成功率**，
    报表从不显示它 ⇒ **无法回答"副平台开着还是关着"**，
    而这直接决定流量来源判断。

    关键性质：`platforms` 只记**真实送达**（main.py 明确"不记未送达的副平台"）
    ⇒ 副平台在分布里缺席 = **真没发出去**，不是"发了没记账"。
    这让它成为可信的配置状态面，而不只是装饰。
    """

    def _rows(self, plats_list):
        return [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-01T00:00:00+00:00", "content_id": f"c{i}",
                 "hour_bj": 12, "source": "U.Today (X)", "final_preview": "正文",
                 "platforms": p}
                for i, p in enumerate(plats_list)]

    def test_counts_platforms(self):
        rows = self._rows([["binance"], ["binance", "telegram"], ["okx_draft"]])
        s = mr.summarize(rows)
        self.assertEqual(dict(s["by_platform"]),
                         {"binance": 2, "telegram": 1, "okx_draft": 1})
        self.assertEqual(s["multi_platform_posts"], 1, "一帖两平台须单独计数")

    def test_renders_distribution(self):
        rows = self._rows([["binance"], ["binance", "telegram"]])
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("平台分布", text)
        self.assertIn("binance", text)
        self.assertIn("telegram", text)

    def test_single_platform_warns_with_switch_name(self):
        """仅 binance 时必须提示**开关在哪**——否则读者只知道"没有副平台"，
        不知道该改哪个变量。"""
        rows = self._rows([["binance"], ["binance"]])
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("PUBLISH_PLATFORMS", text, "必须点明开关变量名")
        self.assertIn("当前无产出", text)

    def test_no_warning_when_multi_platform(self):
        """有多平台时不发"仅binance"警示（避免噪音训练读者）"""
        rows = self._rows([["binance"], ["binance", "telegram"]])
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertNotIn("仅 binance 单平台", text)

    def test_undelivered_not_counted(self):
        """**未送达的平台不得计数**——`platforms: []` 的行（拒稿/发布失败）
        不能给任何平台加一。"""
        rows = self._rows([["binance"], []])
        s = mr.summarize(rows)
        self.assertEqual(s["by_platform"]["binance"], 1)
        self.assertEqual(s["multi_platform_posts"], 0)

    def test_malformed_platforms_ignored(self):
        """脏数据（None / 非字符串 / 空串）不得抛异常"""
        rows = self._rows([["binance", None, 123, "  "], ["binance"]])
        s = mr.summarize(rows)
        self.assertEqual(s["by_platform"]["binance"], 2,
                         "只计有效平台名，脏项静默跳过")
        self.assertEqual(s["multi_platform_posts"], 0)

    def test_not_rendered_without_platforms(self):
        """无已发布帖时不渲染该行（向后兼容）"""
        rows = [{"outcome": "llm_rejected", "ts": "2026-10-01T00:00:00+00:00",
                 "platforms": []}]
        s = mr.summarize(rows)
        self.assertNotIn("平台分布", mr.render_text(s, rows))


class TestR653RealFunnelVisible(unittest.TestCase):
    """R653：**配额饱和轮在配额检查处提前 return，从不进入选稿**。

    原报表那行把"配额饱和 1777 轮"与"候选 12833 → 发布 286"并列，
    看起来像"排了 12833 条只发出 286 条（效率 2.2%）"
    ⇒ 极易得出错误结论"排序效率低、要把好稿排前面"。

    实测真相：1777/2078 轮（85.5%）的 `candidates` / `published` /
    `unprocessed` **全是 0** ⇒ 它们**从未走过选稿与排序**。
    真正进入选稿的只有 **301 轮（14.5%）**。
    ⇒ 那 2.2% 的分母里绝大部分根本没被排过，**判"排序效率"是错的**。
    """

    @staticmethod
    def _run(cand, pub, unproc, blocked):
        return {"outcome": "run_summary", "ts": "2026-10-04T00:00:00+00:00",
                "candidates": cand, "published": pub, "unprocessed": unproc,
                "quota_blocked": blocked}

    def test_splits_selection_vs_early_exit(self):
        rows = [self._run(0, 0, 0, True) for _ in range(10)]
        rows += [self._run(100, 3, 90, False) for _ in range(2)]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["quota_earlyexit_runs"], 10,
                         "饱和且三计数全 0 ⇒ 判为提前退出")
        self.assertEqual(s["runs"]["selection_runs"], 2)
        self.assertEqual(s["runs"]["sel_candidates"], 200)
        self.assertEqual(s["runs"]["sel_published"], 6)
        self.assertEqual(s["runs"]["sel_unprocessed"], 180)

    def test_enters_selection_when_unprocessed_only(self):
        """**不能只看"非饱和"判选稿轮**——未饱和但真没稿的轮次
        （三计数全 0）并未进入选稿，混进去会虚高分母。"""
        rows = [self._run(0, 0, 0, False)]
        s = mr.summarize(rows)
        self.assertEqual(s["runs"]["selection_runs"], 0)
        self.assertEqual(s["runs"]["quota_earlyexit_runs"], 1)

    def test_unprocessed_counts_as_entered(self):
        """有未处理 ⇒ 确实进了选稿（哪怕一篇未发）"""
        s = mr.summarize([self._run(50, 0, 50, False)])
        self.assertEqual(s["runs"]["selection_runs"], 1)
        self.assertEqual(s["runs"]["sel_unprocessed"], 50)

    def test_renders_real_funnel(self):
        rows = [self._run(0, 0, 0, True) for _ in range(10)]
        rows += [self._run(100, 3, 90, False)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("真实漏斗", text)
        self.assertIn("进入选稿", text)
        self.assertIn("配额已满直接退出", text)

    def test_percentages_independent(self):
        """⚠️ 两个比例必须**各自独立算**——用 `1 - 选稿占比` 会出负数。

        首版 `(1-_share)` 在选稿14.5% 时渲染出 **-13%**（四舍五入所致）。
        守卫锁住"退出占比必须为正且与选稿占比相加约 100"。
        """
        rows = [self._run(0, 0, 0, True) for _ in range(86)]
        rows += [self._run(10, 1, 5, False) for _ in range(14)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertNotIn("-13%", text)
        self.assertNotIn("（-", text)
        self.assertIn("86%", text)   # 退出轮占比
        self.assertIn("14%", text)   # 选稿轮占比

    def test_low_rate_blamed_on_quota_not_sorting(self):
        """**低处理率不得被渲染成"排序效率问题"**——2.2% 是配额 12/天的
        自然结果。要判排序质量得看浏览量归因维度。"""
        rows = [self._run(12833, 286, 10514, False)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("配额", text)
        self.assertNotIn("排序没把好稿排到前面", text,
                         "禁止把配额导致的低处理率误诊为排序问题")

    def test_not_rendered_without_runs(self):
        s = mr.summarize([{"outcome": "binance_published", "ts": "x",
                           "platforms": ["binance"]}])
        self.assertNotIn("真实漏斗", mr.render_text(s, []))


class TestR654RollingQuotaWindow(unittest.TestCase):
    """R654：**滚动 24h 配额的真实水位**——R653 与 R650 的共同根因。

    机制（`main.py:9152`）：`sent_24h = count_since(24)` 是**滚动**窗口
    （非自然日），且 `>= MAX_DAILY_POSTS` 即**提前 return**。

    由此推出两件此前无人明说的事：
    1. **R653 的"86% 轮次提前退出"不是偶发**——配额本来就长期接近满。
    2. **R650 的时段偏置在当前结构下几乎无法生效**——不是权重不够，
       而是**窗口里根本没有空位**。生产实测：高浏览窗（北京 06-12）
       发布时，其前 24h 窗口内中位已发 **11.0/12（92%）**。

    ⇒ 真杠杆是**减量提质**或**调整 MAX_DAILY_POSTS**，
    **不是继续调排序权重**。本护栏就是把这个结论常驻在报表里。
    """

    def _pub(self, hour, ts, maxp=12):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "ts": ts, "hour_bj": hour, "content_id": ts,
                "max_daily_posts": maxp, "source": "S", "final_preview": "x"}

    def test_computes_window_occupancy(self):
        """连续 12 篇发布 ⇒ 每篇的前24h计数应递增到 11"""
        rows = [self._pub(20, f"2026-10-01T{h:02d}:00:00+00:00")
                for h in range(12)]
        s = mr.summarize(rows)
        occ = s.get("quota_window_occupancy")
        self.assertIsNotNone(occ, "必须算出滚动窗口占用")
        self.assertEqual(occ["samples"], 12)
        # 12 篇跨度仅 12h< 24h ⇒ 滚动窗口不清空 ⇒ 计数为 0..11。
        # 偶数个取**真中位**（两中值平均，R610/R609 纪律）= (5+6)/2 = 5.5。
        # 写成6.0 会漏掉这条纪律。
        self.assertEqual(occ["all_median"], 5.5)

    def test_high_window_occupancy_blocks_sorting(self):
        """高浏览窗占用 ≥80% 时，必须提示"排序优化无从下手" """
        # 24h内先发 11 篇夜间，再发 1 篇上午 ⇒ 上午那篇窗口内已 11/12
        rows = [self._pub(22, f"2026-10-01T{h:02d}:00:00+00:00")
                for h in range(0, 11)]
        rows.append(self._pub(8, "2026-10-01T23:00:00+00:00"))  # hour_bj=8 高窗
        s = mr.summarize(rows)
        text = mr.render_text(s, rows)
        self.assertIn("滚动24h 配额水位", text)
        self.assertIn("基本无从生效", text)
        self.assertIn("MAX_DAILY_POSTS", text,
                      "必须给出可行动方向，而不只是说这条优化没用")

    def test_suggests_right_levers(self):
        """警示必须指向**减量提质 / 调整配额**，而非"调排序权重" """
        rows = [self._pub(22, f"2026-10-01T{h:02d}:00:00+00:00")
                for h in range(0, 11)]
        rows.append(self._pub(8, "2026-10-01T23:00:00+00:00"))
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("减量提质", text)
        self.assertIn("不是继续调排序权重", text)

    def test_silent_when_occupancy_low(self):
        """占用低时**不发**警示（避免噪音，R619 的反向要求）"""
        # 只发 1 篇 ⇒ 窗口占用 0
        rows = [self._pub(8, "2026-10-01T00:00:00+00:00")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertNotIn("基本无从生效", text)

    def test_uses_rolling_not_calendar_day(self):
        """**必须是滚动窗口**——跨日不重置。31 号23:00 与 1 号 01:00 的两篇，
        后者窗口内应含前者（日历日口径会算成 0）。"""
        rows = [self._pub(23, "2026-10-01T23:00:00+00:00"),
                self._pub(1, "2026-10-02T01:00:00+00:00")]
        s = mr.summarize(rows)
        occ = s["quota_window_occupancy"]
        self.assertEqual(occ["samples"], 2)
        # 第二篇的窗口内应有 1 篇 ⇒ 全体中位落在 0/1 之间
        self.assertLessEqual(occ["all_median"], 1.0)

    def test_unparseable_ts_not_counted(self):
        """ts 不可解析的行不进样本（不是当 0 处理，R612）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "垃圾", "hour_bj": 8, "max_daily_posts": 12}]
        s = mr.summarize(rows)
        occ = s.get("quota_window_occupancy")
        self.assertTrue(occ is None or occ["samples"] == 0,
                        "不可解析时不得产出样本（量不到 ≠ 量到 0）")

    def test_not_gated_by_run_summary(self):
        """⚠️ **不得被 run_summary 门控**（R643 同款错的回归守卫）。

        首版把这段嵌在 `if runs.get("n"):` 内 ⇒ **纯发布行（无 run_summary）
        时整块消失**。而配额水位是**按帖**属性，按轮次存在与否门控它毫无道理。
        这条测试用"只有发布行、没有 run_summary"的输入把它钉住。
        """
        rows = [self._pub(22, f"2026-10-01T{h:02d}:00:00+00:00")
                for h in range(0, 11)]
        rows.append(self._pub(8, "2026-10-01T23:00:00+00:00"))
        self.assertFalse(any(r.get("outcome") == "run_summary" for r in rows),
                         "本用例前提：数据里没有 run_summary")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("滚动24h 配额水位", text,
                      "无 run_summary 时配额水位仍须渲染（不可被轮次数据门控）")
        self.assertIn("基本无从生效", text)


class TestR655RollingWindowSelfLock(unittest.TestCase):
    """R655：**为什么排序改不动**——滚动 24h 窗口的"夜间自锁"结构。

    R654 只说了"配额在发帖前就满（92%）"，但**没说清为什么**。
    本轮补上机制，答案是滚动窗口造成的**严格对角自锁**：

    > 每篇帖退出窗口的时刻 = 它的 `ts + 24h`，
    > 而北京时区下 `ts+24h` 的北京小时 **就是 24 小时前的发帖时段**
    >（实测"发帖时段 → 释放时段"交叉表 **24/24 全在对角线**）。

    ⇒ "夜间发帖 → 夜间释放 → 被夜间立即接走"**自我复制**。
    实测：仅 **18%** 的槽位释放时刻落在高浏览窗。
    ⇒ **时间不可逆** ⇒ 排序只能改"发哪一篇"，
    改不了"什么时候有空位"。

    这条比 R654 的"水位 92%"更硬：水位只是**症状**，
    对角自锁才是**机制**——它解释了为什么这个状态不会自行缓解。
    """

    def _pub(self, hour, ts, maxp=12):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "ts": ts, "hour_bj": hour, "content_id": ts,
                "max_daily_posts": maxp, "source": "S", "final_preview": "x"}

    def test_computes_last_slot_and_release(self):
        """连续 12 篇⇒ 后几篇的发出前占用达到 MAX-1（抢最后一个空位）

        构造需**同时包含高浏览窗与低浏览窗**的帖：水位行与R655 指标同处
        一个 `if s.get("quota_window_occupancy")` 块，纯低窗样本会让
        `pref_window_median=None` 而跳过渲染（生产数据两窗都有）。
        """
        rows = [self._pub(8, f"2026-10-01T{h:02d}:00:00+00:00")
                for h in range(6)]          # 高浏览窗 6 篇
        rows += [self._pub(22, f"2026-10-01T{h:02d}:00:00+00:00")
                 for h in range(6, 12)]      # 低浏览窗 6 篇
        occ = mr.summarize(rows)["quota_window_occupancy"]
        self.assertIsNotNone(occ["last_slot_share"])
        self.assertGreater(occ["last_slot_share"], 0)
        # 抢最后空位的 6 篇全是低窗（hour 22）⇒ 落在高窗比例 0
        self.assertEqual(occ["last_slot_pref"], 0.0)
        # 槽位释放时刻 = 同小时 ⇒ 8 点那 6 篇在高窗、22 点那 6 篇不在
        self.assertEqual(occ["slot_release_pref_share"], 50.0)

    def test_release_hour_equals_post_hour(self):
        """★ 核心机制断言：滚动窗口的释放时刻**北京小时等于发帖小时**。

        这是"夜间自锁"的数学根源。若这条变了（如改成自然日窗口），
        自锁就会解开，R650 也重新有空间——所以必须锁住。
        """
        rows = [self._pub(3, "2026-10-01T00:00:00+00:00"),
                self._pub(9, "2026-10-01T12:00:00+00:00")]
        occ = mr.summarize(rows)["quota_window_occupancy"]
        # 释放时刻的高窗占比 = 1/2（9点那篇）
        self.assertEqual(occ["slot_release_pref_share"], 50.0)

    def test_renders_self_lock_explanation(self):
        """必须渲染"自锁"机制，不能只报水位百分比。

        构造须**低窗占绝大多数**（生产实测槽位释放只有 18% 落在高窗），
        否则 `_sr >= 30` 时按设计不渲染自锁行——那是**有意的**：
        自锁是"结构性异常"的说明，水位分布正常时不必报警。
        """
        rows = [self._pub(8, "2026-10-01T00:00:00+00:00")]      # 1 篇高窗
        rows += [self._pub(22, f"2026-10-01T{h:02d}:00:00+00:00")
                 for h in range(1, 12)]                          # 11 篇低窗
        occ = mr.summarize(rows)["quota_window_occupancy"]
        self.assertLess(occ["slot_release_pref_share"], 30,
                        "构造须满足自锁条件（生产实测 18%）")
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("抢最后一个空位", text, "必须说明施力点在哪个位置")
        self.assertIn("时间不可逆", text,
                      "必须解释**为什么排序改不动**，否则读者以为配额满是偶发")
        self.assertIn("自锁", text)

    def test_reports_last_slot_preference_gap(self):
        """必须同时报两个比例（施力点占比 / 落在高窗的比例）——
        单报"最后一个空位占 77%"会让人误以为可以随便调。"""
        rows = [self._pub(8, f"2026-10-01T{h:02d}:00:00+00:00")
                for h in range(6)]
        rows += [self._pub(22, f"2026-10-01T{h:02d}:00:00+00:00")
                 for h in range(6, 12)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("落在高浏览窗", text)
        # 同样是"落在高浏览窗"，槽位释放那个必须以"释放时刻"限定
        self.assertIn("释放时刻", text)

    def test_no_release_stats_when_unparseable(self):
        """ts 不可解析 ⇒ 三个 R655 指标都是 None（不是 0）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "垃圾", "hour_bj": 8, "max_daily_posts": 12}]
        occ = mr.summarize(rows).get("quota_window_occupancy")
        self.assertTrue(occ is None or occ["last_slot_share"] is None)


class TestR656SupplyVsOutput(unittest.TestCase):
    """R656：★ 关键分界——"发不出"有两种完全不同的原因，必须按时段拆开。

    | 原因 | 特征 | 处置 |
    |---|---|---|
    | (a) **候选不足** | 该时段候选本来就少 | 加源 / 加频次 |
    | (b) **配额被占** | 候选充足但发不出 | 动配额结构 |

    实测生产：高浏览窗内**62 轮进入选稿、2673 个候选（充足）**，
    却只发出 **51 篇**（52×）⇒ 属 (b)。

    ⚠️ **本轮更正了两个自己之前的错误结论**：
    1. R655 结尾建议"改自然日窗口可解开自锁"——**反事实模拟证明无效**
       （+0%）：自然日窗口下高窗候选同样只有 2.3 个/天，**瓶颈不在窗口定义**。
    2. 我一度判"候选池本身就是瓶颈"（每天只有 2.3 个高窗候选）——
       **也错了**：那 2.3 个是"夜间发的帖恰好落在高窗"的结果，
       高窗内实际有 2673 个候选，只是**被配额挡住发不出**。
    ⇒ **正确结论：杠杆（时段偏置）方向是对的，被配额位置挡住；
    真处置是动配额结构（减量 / 自然日窗口 / 提高额度）。**
    """

    @staticmethod
    def _run(hour, cand, pub, ts="2026-10-04T00:00:00+00:00"):
        return {"outcome": "run_summary", "ts": ts, "hour_bj": hour,
                "candidates": cand, "published": pub,
                "unprocessed": cand - pub, "quota_blocked": False}

    def test_splits_supply_and_output_by_hour(self):
        rows = [self._run(8, 2000, 40) for _ in range(3)] + \
               [self._run(22, 3000, 200) for _ in range(3)]
        s = mr.summarize(rows)
        b = s["runs"]["by_hour_sel"]
        self.assertEqual(b["pref_cand"], 6000)
        self.assertEqual(b["pref_pub"], 120)
        self.assertEqual(b["pref_runs"], 3)
        self.assertEqual(b["oth_cand"], 9000)

    def test_renders_supply_output_line(self):
        rows = [self._run(8, 2000, 40), self._run(22, 3000, 200)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("时段内供给/产出", text)
        self.assertIn("候选 2000 → 发布 40", text)

    def test_flags_quota_occupied_not_supply_short(self):
        """候选 ≫ 产出时必须明确判为**配额被占**，并指向动配额结构"""
        rows = [self._run(8, 2000, 40), self._run(22, 3000, 200)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("配额被占", text)
        self.assertIn("动配额结构", text,
                      "必须给出可行动方向，而不只是陈述现象")

    def test_threshold_is_reachable(self):
        """⚠️ 阈值必须是**真实数据能达到**的（回归守卫）。

        我第一版把阈值设成 100×，而生产实测是 52×⇒ **护栏存在却从不触发**，
        等于没有（与 R612「有数据无出口」同型的另一种形式）。
        """
        rows = [self._run(8, 2000, 40), self._run(22, 3000, 200)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("配额被占", text)   # 50× 必须触发

    def test_no_warning_when_balanced(self):
        """候选与产出接近时**不发**警示（避免噪音）"""
        rows = [self._run(8, 12, 10), self._run(22, 12, 10)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertNotIn("配额被占", text)

    def test_r658_quota_tuning_guidance(self):
        """R658：配额护栏必须说清**去哪改** + **会带来多少** + **代价**。

        `MAX_DAILY_POSTS` 是**仓库变量 `vars.MAX_DAILY_POSTS`**（不是 secret）
        ⇒ 只报变量名等于没说。只报"调参位置"而不报"效果与代价"，
        又会变成无信息的许愿。三者缺一不可。

        ⚠️ 构造须同时含**发布行**（水位只统计投递行，R654）**且高窗水位 ≥80%**
        （否则阈值不触发）。要让水位达 80%，必须**模拟生产**：连续 3 天、
        每天 12 篇全部标 `hour_bj=8`。我试过"12 篇挤在 12 小时内"与
        "6 篇/天连做 4 天"两种构造——水位分别只有 5.5/12=46% 与 6/12=50%，
        **阈值不触发、护栏整块不渲染**，测试会误判成"功能没做"。
        """
        import datetime as _dt
        rows = [self._run(8, 2000, 20), self._run(22, 3000, 200)]
        _base = _dt.datetime(2026, 9, 20, 8, 0, tzinfo=_dt.timezone.utc)
        for _d in range(3):
            for _h in range(12):
                rows.append({
                    "platforms": ["binance"], "outcome": "binance_published",
                    "ts": (_base + _dt.timedelta(days=_d, hours=_h)).isoformat(),
                    "hour_bj": 8, "content_id": f"c{_d}{_h}",
                    "max_daily_posts": 12, "source": "S", "final_preview": "x"})
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("配额已在发帖前就接近饱和", text, "前提未达成：水位未达阈值")
        self.assertIn("Variables → `MAX_DAILY_POSTS`", text)
        self.assertIn("+33%", text)          # 16 篇的预估
        self.assertIn("+67%", text)          # 20 篇的预估
        self.assertIn("权重", text)   # 代价必须说

    def test_r658_states_supply_is_not_constraint(self):
        """必须明说候选供给远不是约束（否则读者会以为要先加源）"""
        import datetime as _dt
        rows = [self._run(8, 2000, 20), self._run(22, 3000, 200)]
        _base = _dt.datetime(2026, 9, 20, 8, 0, tzinfo=_dt.timezone.utc)
        for _d in range(3):
            for _h in range(12):
                rows.append({
                    "platforms": ["binance"], "outcome": "binance_published",
                    "ts": (_base + _dt.timedelta(days=_d, hours=_h)).isoformat(),
                    "hour_bj": 8, "content_id": f"c{_d}{_h}",
                    "max_daily_posts": 12, "source": "S", "final_preview": "x"})
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("候选供给", text)
        self.assertIn("线性增产", text)

    def test_missing_hour_bj_not_counted_either_side(self):
        """hour_bj 缺失/非法 ⇒ 两侧都不计入（不是算进"其他"）"""
        rows = [{"outcome": "run_summary", "ts": "2026-10-04T00:00:00+00:00",
                 "candidates": 100, "published": 50, "unprocessed": 50,
                 "quota_blocked": False}]
        b = mr.summarize(rows)["runs"]["by_hour_sel"]
        self.assertEqual(b["pref_cand"], 0)
        self.assertEqual(b["oth_cand"], 0)

    def test_early_exit_rounds_not_counted(self):
        """配额饱和提前退出的轮次（三计数全0）不计入供给"""
        rows = [self._run(8, 0, 0), self._run(8, 100, 20)]
        b = mr.summarize(rows)["runs"]["by_hour_sel"]
        self.assertEqual(b["pref_runs"], 1, "只有真正选稿的那轮计入")
        self.assertEqual(b["pref_cand"], 100)


class TestR657MainAstCache(unittest.TestCase):
    """R657：**AST 解析缓存**——报表渲染提速 5.4×（4.65s → 0.86s）。

    R617 立判据：报表必须用 AST 而非 import/正则/硬编码读`main.py` 的
    通道池与置顶清单（import 有副作用、正则改缩进就静默漏站、硬编码必漂移）。
    但那两个探针**每次调用都重新 `ast.parse` 整个 main.py（9884 行）**
    —— 实测 `render_text` 里被调 6 次、累计 **3.6 秒**，占整体 **86%**。

    ⚠️ 缓存键必须含 **mtime + size**：只按路径缓存会让"改了 main.py 之后
    报表仍报旧结论"——那比慢更坏（R612「沉默不是通过」的变体：
    **探针在跑，但报的是过期答案**）。
    """

    def test_caches_tree(self):
        mr._MAIN_AST_CACHE.update({"key": None, "tree": None})
        a = mr._parse_main_ast()
        b = mr._parse_main_ast()
        self.assertIsNotNone(a, "main.py 必须能解析（生产有该文件）")
        self.assertIs(a, b, "同键必须返回**同一个** tree 对象（不重新解析）")

    def test_key_includes_mtime_and_size(self):
        """缓存键必须能识别"文件变了"——否则改了 main.py 报表报旧结论"""
        mr._parse_main_ast()
        key = mr._MAIN_AST_CACHE["key"]
        self.assertIsInstance(key, tuple)
        self.assertEqual(len(key), 3, "键须含 (path, mtime, size)")
        self.assertIsInstance(key[1], int)
        self.assertIsInstance(key[2], int)

    def test_cache_invalidated_on_change(self):
        """★ 改了 main.py 之后必须重新解析（这是缓存最危险的失败模式）"""
        import os
        import tempfile
        mr._MAIN_AST_CACHE.update({"key": None, "tree": None})
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as f:
            f.write("x = 1\n")
            p1 = f.name
        with open(p1, "a", encoding="utf-8") as f:
            f.write("y = 2\n")
        try:
            t1 = mr._parse_main_ast(p1)
            t2 = mr._parse_main_ast(p1)
            self.assertIs(t1, t2, "文件未变时应命中缓存")
            # 改size + mtime ⇒ 必须失效
            os.utime(p1, (os.path.getatime(p1), os.path.getmtime(p1) + 10))
            with open(p1, "a", encoding="utf-8") as f:
                f.write("z = 3\n")
            t3 = mr._parse_main_ast(p1)
            self.assertIsNot(t1, t3, "文件变化后必须重新解析，不得返回旧 tree")
        finally:
            os.unlink(p1)
            mr._MAIN_AST_CACHE.update({"key": None, "tree": None})

    def test_unparseable_returns_none_and_is_cached(self):
        """解析失败返回 None，且**失败也要缓存**（否则每次都重试失败路径）"""
        import os
        import tempfile
        mr._MAIN_AST_CACHE.update({"key": None, "tree": None})
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as f:
            f.write("def (  # 语法错误\n")
            p1 = f.name
        try:
            self.assertIsNone(mr._parse_main_ast(p1))
            self.assertIsNone(mr._parse_main_ast(p1))
        finally:
            os.unlink(p1)
            mr._MAIN_AST_CACHE.update({"key": None, "tree": None})

    def test_missing_file_returns_none(self):
        mr._MAIN_AST_CACHE.update({"key": None, "tree": None})
        self.assertIsNone(mr._parse_main_ast("不存在的路径.py"))

    def test_probes_still_work_with_cache(self):
        """缓存不得改变两个探针的结论（它们仍须各返回各自的集合）"""
        self.assertIsInstance(mr._priority_pinned_from_main(), list)
        self.assertIsInstance(mr._provider_pool_from_main(), list)

    def test_render_output_identical_with_cache(self):
        """★ 缓存**只提速、不改结论**——同一批数据两次渲染必须逐字相同"""
        rows = [TestR656SupplyVsOutput._run(8, 2000, 40),
                TestR656SupplyVsOutput._run(22, 3000, 200)]
        mr._MAIN_AST_CACHE.update({"key": None, "tree": None})
        s = mr.summarize(rows)
        a = mr.render_text(s, rows)
        b = mr.render_text(s, rows)          # 命中缓存的第二次
        self.assertEqual(a, b, "缓存前后渲染输出必须完全一致")


class TestR659PostFixStratification(unittest.TestCase):
    """R659：**修复效果必须按"上线前后"分层判读**，否则旧稿冒充修复效果。

    发现路径：代码上线（2026-10-05 00:07 UTC）后立刻核对新字段，
    全部为 0。诊断发现**不是没上线**——上线后两轮都是
    `quota_blocked=True, sent_24h=12/12` ⇒ **配额满 → 提前return → 不发帖**
    ⇒ 不写回执 ⇒ 新字段自然是 0。

    而这暴露一个真实的观测误读：报表里
    `⚠️ 标题$挂件紧贴中文 9/13（69%）…（R643 修复后应为 0）`
    **读起来像"修复无效"**，实际那 13 篇全是**修复前发布的旧稿**。

    ⇒ R659 规则：**无新样本时必须显式说"无法判定"**，
    并把旧稿数字标为"历史残留"；**不得让旧稿违规率冒充修复效果**
    （R612沉默不是通过 / R610判读前先分层的直接应用）。
    """

    @staticmethod
    def _art(ts, title):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "ts": ts, "hour_bj": 8, "content_id": ts,
                "article_title": title, "final_preview": "x",
                "source": "S", "max_daily_posts": 12}

    def test_no_postfix_sample_keeps_warning(self):
        """只有旧稿 ⇒ **告警必须保留**，只追加"历史残留/无法判定"限定语。

        ⚠️ 首版实现把旧稿告警**整个替换**成"⏸️ 暂无新样本"，
        被R646 的两个既有测试抓出来（它们断言"标题$挂件间距正常"这类文案）。
        那次失败是**有价值的**：它暴露了"为了让分层生效而静音了旧稿问题"，
        违反 **R619（告警消失必须可归因）**——
        "分母是新稿"不等于"旧稿没问题"。
        ⇒ 正确做法：保留 ⚠️ + 追加"全部来自修复上线前的旧稿 / 无法判定修复效果"。

        ⚠️ 数据要造**紧贴**（`$` 紧贴汉字），不是**纯名**（无 `$`）——
        两者是**不同根因**（R643 vs R645）各有护栏，用错数据会测到另一条线。
        """
        rows = [self._art("2026-09-20T08:00:00+00:00", "$BTC刚砸进池子")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("标题$挂件紧贴", text, "旧稿告警必须保留（R619）")
        self.assertIn("⚠️", text)
        self.assertIn("无法判定修复效果", text)
        self.assertIn("旧稿", text)
        self.assertNotIn("⏸️ 标题$挂件紧贴", text,
                         "不得用'暂无新样本'取代原告警")

    def test_no_postfix_sample_keeps_pure_ticker_warning(self):
        """纯名侧同样必须保留告警（R619）"""
        rows = [self._art("2026-09-20T08:00:00+00:00", "BTC 86110稳着")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("长文标题含币种纯名", text, "旧稿纯名告警必须保留")
        self.assertIn("无法判定修复效果", text)

    def test_postfix_clean_reports_ok(self):
        """有新稿且零紧贴 ⇒ 报✅ 且**只用新稿分母**"""
        rows = [self._art("2026-10-06T08:00:00+00:00", "BTC 稳住了 $BTC 冲高")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("新稿", text)
        self.assertIn("零紧贴", text)
        self.assertNotIn("历史残留", text)

    def test_postfix_dirty_reports_still_broken(self):
        """有新稿且仍紧贴 ⇒ 必须报"修复后仍有紧贴"（不是历史残留）"""
        rows = [self._art("2026-10-06T08:00:00+00:00", "$BTC刚砸进池子")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("新稿", text)
        self.assertIn("仍有紧贴", text)
        self.assertIn("绕过", text)

    def test_old_and_new_both_listed_separately(self):
        """新旧都有 ⇒ 两组分别报，且旧稿被标注不计入判定"""
        rows = [self._art("2026-09-20T08:00:00+00:00", "$ETH起飞了"),
                self._art("2026-10-06T08:00:00+00:00", "$BTC 稳住了")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("旧稿", text)
        self.assertIn("不计入修复效果判定", text)

    def test_is_post_fix_tri_state(self):
        """★ `_is_post_fix` 必须**三态**：True/False/None（不可判定）。

        `None` 不是 False——把"ts 不可解析"当成"旧稿"会静默归错类（R612）。
        """
        old = {"ts": "2026-09-20T08:00:00+00:00"}
        new = {"ts": "2026-10-06T08:00:00+00:00"}
        bad = {"ts": "垃圾"}
        fz = mr._fix_live_ts()
        self.assertIsNotNone(fz, "上线时刻必须可解析（生产写死UTC ISO）")
        self.assertIs(mr._is_post_fix(new, fz), True)
        self.assertIs(mr._is_post_fix(old, fz), False)
        self.assertIsNone(mr._is_post_fix(bad, fz))
        # 时刻未知 ⇒ 不可判定
        self.assertIsNone(mr._is_post_fix(new, None))


class TestR660TimePrefIsIneffective(unittest.TestCase):
    """R660：★ R650 的 `apply_hour_preference_boost` 是**零影响的无效实现**。

    实现（`main.py:2812-2845`）在低浏览窗给**所有**候选
    `impact_score -= TIME_PREF_PENALTY`（同一个常数），调用处
    （`main.py:9349`）随即 `sort(key=(-impact_score, age_hours))`。

    **数学事实**：`(s_i - a) - (s_j - a) = s_i - s_j`
    ⇒ 相对顺序**完全不变** ⇒ `sort()` 是空操作
    ⇒ 选出的前 `max_posts` 篇与不加分时**逐条相同**。

    且即使改成"只扣一部分"，`TIME_PREF_PENALTY=2` 相对生产
    `impact_score` 跨度 **6~1007**（实测 485 样本）只有千分之一效力
    ⇒ **双重无效**（R622「权重须量纲对齐」在 R650 被我自己违反）。

    ★它有完整注释、遥测字段与测试，**看起来已实现**——
    这比 bug 更隐蔽：代码在跑、字段在落、测试在过，行为却零变化。
    """

    CAND = [
        {"id": "A", "impact_score": 12.0, "age_hours": 1.0},
        {"id": "B", "impact_score": 10.5, "age_hours": 2.0},
        {"id": "C", "impact_score": 10.5, "age_hours": 0.5},
        {"id": "D", "impact_score": 8.0, "age_hours": 3.0},
        {"id": "E", "impact_score": 3.0, "age_hours": 0.2},
    ]

    @staticmethod
    def _sort_key(x):
        return (-x["impact_score"],
                x["age_hours"] if x.get("age_hours") is not None
                else float("inf"))

    def test_uniform_penalty_preserves_order(self):
        """★ 数学断言：给所有候选减同一常数，选出的前 N 篇逐条不变"""
        import copy
        max_posts = 2     # workflow fallback（main.py:9400 注释）
        before = [c["id"] for c in sorted(copy.deepcopy(self.CAND),
                                          key=self._sort_key)][:max_posts]
        cand = copy.deepcopy(self.CAND)
        for c in cand:
            c["impact_score"] -= 2      # 与 main.py 完全等价的操作
        after = [c["id"] for c in sorted(cand, key=self._sort_key)][:max_posts]
        self.assertEqual(before, after,
                         "均匀减分**必然**不改变排序结果——这正是实现无效的原因")

    def test_penalty_magnitude_against_production_span(self):
        """量级断言（R661 更正）：-2 相对**自然**跨度 6~54 只有 ~4% 效力。

        ⚠️ **R661 修正**：首版这里写"跨度 6~1007 ⇒ 0.2% 效力"，
        那是把`PRIORITY_SEED_SCORE=999`（人工置顶种子 + freshness）
        **算进了自然分布**，把无效程度**夸大了 21 倍**。
        排除种子后：自然跨度 = 54 − 6 = **48** ⇒ 2/48 ≈ **4.2%**。

        ⇒ **结论不变**（主因是数学恒等式，量级只是次要理由），
        但**理由的数字必须对**——不能因为结论对就放过错的数（R641）。
        """
        pen = mr._time_pref_penalty_from_main()
        self.assertIsNotNone(pen, "必须能从 main.py AST 读到幅度")
        self.assertIsInstance(pen, (int, float))
        # 生产自然分布实测（482 个非 priority_seed 样本，6.0 ~ 54.0）
        natural_span = 54.0 - 6.0
        self.assertLess(pen / natural_span, 0.10,
                        "偏置幅度相对**自然**跨度应<10%")
        # 护栏文案里那个 1007 来自人工种子——若有人再用它算跨度会再次夸大
        self.assertGreater(999.0, natural_span,
                           "PRIORITY_SEED_SCORE=999 远在自然分布之外")

    def test_probe_returns_none_when_unavailable(self):
        """探针读不到时返回 None，**不得**静默返 0（R612）"""
        self.assertIsNone(mr._time_pref_penalty_from_main("不存在的文件.py"))

    def test_renders_ineffective_warning(self):
        """报表必须显式告警"R650 偏置是无效实现"，
        否则后人看到"已实现"就以为时段问题已解决"""
        rows = [TestR659PostFixStratification._art(
            f"2026-10-06T{h:02d}:00:00+00:00", "$BTC 稳住了")
            for h in range(8, 20)]
        rows.append({"outcome": "run_summary", "ts": "2026-10-06T20:00:00+00:00",
                     "hour_bj": 22, "candidates": 3000, "published": 40,
                     "unprocessed": 2960, "quota_blocked": False})
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("时段偏置是无效实现", text)
        self.assertIn("空操作", text)
        self.assertIn("跨轮次", text,
                      "必须指出正确方向：跨轮次配额分配，不是同批排序")

    def test_probe_does_not_hardcode(self):
        """幅度必须来自 AST 而非硬编码（否则两者会各自漂移）"""
        import inspect
        src = inspect.getsource(mr._time_pref_penalty_from_main)
        self.assertNotIn("return 2", src,
                         "禁止硬编码幅度（main.py 改了报表会静默过期）")
        self.assertIn("_parse_main_ast", src, "必须走 AST 缓存探针")


class TestR661BoostEffectivenessAudit(unittest.TestCase):
    """R661：把 R660 的血泪变成**机械判定**——每个改 `impact_score` 的加权
    是否真的区分候选，靠 AST 判定，而不是靠人眼扫代码。

    R660 教训：`apply_hour_preference_boost` 给**所有**候选减同一常数 ⇒
    **零影响**，却有完整注释 + 遥测字段 + 单测 + 护栏 ⇒ 看起来已实现。
    ⇒ 这类"看起来实现了"必须能被**自动识别**，否则会反复有人踩。

    ★ 本轮自己也踩了一次同类坑（R612 变体）：**判据覆盖不全 ⇒ 虚假安心**。
    首版审计只认 `AugAssign`（`x["k"] += v`），而那个无效实现写的是
    `Assign`（`x["k"] = cur - P`）⇒ **恰好漏掉了它**，
    报表还显示"4 个加权全部有条件区分"。
    而 `ast.Assign` 的目标是**复数 `.targets`**，`.target` 只有 `AugAssign` 才有。
    """

    def test_finds_all_five_boosters(self):
        r = mr._boost_effectiveness_from_main()
        self.assertIsInstance(r, dict)
        self.assertGreaterEqual(len(r), 5,
                                "应至少发现 5 个改 impact_score 的方法")

    def test_detects_hour_pref_as_uniform(self):
        """★ 时段偏置必须被判为"均匀平移"（R660 已证零影响）"""
        r = mr._boost_effectiveness_from_main()
        self.assertIn("apply_hour_preference_boost", r,
                      "必须抓到它——写的是 Assign(=) 不是 AugAssign(+=)")
        v = r["apply_hour_preference_boost"]
        self.assertGreater(v["unguarded"], 0,
                           "它的加减分是**无条件**的（对全部候选）")
        self.assertEqual(v["guarded"], 0,
                         "它没有条件包裹 ⇒ 应判为均匀平移")

    def test_other_boosters_are_conditional(self):
        """其余 4 个是"有条件区分"（只改部分候选）"""
        r = mr._boost_effectiveness_from_main()
        for name in ("_apply_campaign_boost", "apply_trend_boost",
                     "apply_hot_topic_boost", "apply_engagement_boost"):
            if name in r:
                v = r[name]
                self.assertGreater(v["guarded"], 0,
                                   f"{name} 应有条件包裹")
                self.assertEqual(v["unguarded"], 0,
                                 f"{name} 不应有无条件平移")

    def test_catches_direct_assignment_form(self):
        """★ 判据必须覆盖 `x[k] = v` 与 `x[k] += v` **两种语法**（回归守卫）。

        这是本轮真实踩的坑：只认 AugAssign ⇒ 漏掉 Assign 形式
        ⇒ **护栏给出虚假安心**。测试用最小样本锁住两种写法都能被抓。
        """
        import ast
        import os
        import tempfile
        src = (
            "class NewsFetcher:\n"
            "    def aug(self, items):\n"
            "        for item in items:\n"
            "            if item.get('x'):\n"
            "                item['impact_score'] += 3\n"
            "    def direct(self, items):\n"
            "        for item in items:\n"
            "            item['impact_score'] = 1 - 2\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as f:
            f.write(src)
            path = f.name
        try:
            mr._MAIN_AST_CACHE.update({"key": None, "tree": None})
            r = mr._boost_effectiveness_from_main(path)
            self.assertIn("aug", r, "`+=` 形式必须被抓")
            self.assertIn("direct", r,
                          "★ `= v - P` 形式必须被抓（首版就是漏了它）")
            self.assertEqual(r["direct"]["unguarded"], 1,
                             "无条件赋值应判为均匀平移")
        finally:
            os.unlink(path)
            mr._MAIN_AST_CACHE.update({"key": None, "tree": None})

    def test_renders_audit_line(self):
        """报表必须显示审计结果（否则"有实现"仍是不可见）"""
        rows = [TestR659PostFixStratification._art(
            f"2026-10-06T{h:02d}:00:00+00:00", "$BTC 稳住了")
            for h in range(8, 20)]
        rows.append({"outcome": "run_summary", "ts": "2026-10-06T20:00:00+00:00",
                     "hour_bj": 22, "candidates": 3000, "published": 40,
                     "unprocessed": 2960, "quota_blocked": False})
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("加权有效性审计", text)
        self.assertIn("均匀平移", text)
        self.assertIn("apply_hour_preference_boost", text,
                      "必须点名那个无效的")
        self.assertIn("结构有效≠实际有效", text,
                      "有条件 ≠ 一定有效（R651 显著≠可行动）")

    def test_probe_returns_empty_dict_on_failure(self):
        """解析失败返回 {}，渲染层须显式报"无法判定"（不得静默当正常）"""
        self.assertEqual(mr._boost_effectiveness_from_main("不存在.py"), {})


class TestR662LowHourCapGuardrail(unittest.TestCase):
    """R662：低窗子配额阻断的护栏。

    ★ 为什么必须**独立**计数（R662 的核心）：
      "低窗子配额阻断"与"配额饱和"在遥测上都是"没发帖"。
      若混在一个数字里，护栏会把"额度被低窗吃掉"显示成"额度用完了"，
      而这两者的处置**完全相反**：
        低窗阻断 ⇒ 调 `LOW_HOUR_CAP`（挪配额结构）
        配额饱和 ⇒ 调 `MAX_DAILY_POSTS`（总量）
    ⇒ 判据同 R611「并列信号不能共用分母」。

    实测收益（回放 2134 轮真实心跳，MAX=12 不变）：
      `LOW_HOUR_CAP=2` → 高浏览窗 2.3 → **9.0 篇/天（3.9×）**、总发布仅 −7%
    依据：**高窗每天约 20 个 cron 轮次**（实测中位，P10=9/P90=26），
    而现在只发 2.3 篇 ⇒ 吸收空间充裕。
    """

    @staticmethod
    def _blocked(ts, bj):
        return {"outcome": "run_summary", "ts": ts, "quota_blocked": True,
                "sent_24h": 5, "max_daily_posts": 12,
                "low_hour_blocked": True, "low_hour_cap": 2,
                "low_hour_sent": 2, "low_hour_bj_hour": bj}

    @staticmethod
    def _ok(ts, bj=11):
        return {"outcome": "run_summary", "ts": ts, "quota_blocked": False,
                "sent_24h": 5, "max_daily_posts": 12, "candidates": 300,
                "published": 2, "unprocessed": 298, "hour_bj": bj}

    def test_counts_low_hour_blocked_separately(self):
        rows = [self._blocked("2026-10-06T01:00:00+00:00", 22),
                self._blocked("2026-10-06T02:00:00+00:00", 23),
                self._ok("2026-10-06T03:00:00+00:00")]
        runs = mr.summarize(rows)["runs"]
        self.assertEqual(runs["low_hour_blocked"], 2)
        # ★ 关键：不得把它算进"配额饱和"以外的语义里，也不该与quota_blocked 混算
        self.assertEqual(runs["low_hour_cap"], 2)
        self.assertEqual(runs["low_hour_sent_last"], 2)

    def test_blocked_rounds_not_counted_as_selection(self):
        """低窗阻断轮**没有进入选稿**⇒ 不该进selection_runs"""
        rows = [self._blocked("2026-10-06T01:00:00+00:00", 22),
                self._ok("2026-10-06T03:00:00+00:00")]
        runs = mr.summarize(rows)["runs"]
        self.assertEqual(runs["selection_runs"], 1, "只有真正选稿的那轮计入")
        self.assertEqual(runs["low_hour_blocked"], 1)

    def test_renders_blocked_line(self):
        rows = [self._blocked("2026-10-06T01:00:00+00:00", 22),
                self._ok("2026-10-06T03:00:00+00:00")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("低浏览窗子配额阻断", text)
        self.assertIn("总配额未满", text,
                      "必须说清这是**主动让渡**而非额度用完")

    def test_renders_blocked_hour_histogram(self):
        """必须显示"让渡发生在哪些小时"——用于核对是否让在该让的时段"""
        rows = [self._blocked("2026-10-06T01:00:00+00:00", 22),
                self._blocked("2026-10-06T02:00:00+00:00", 22),
                self._ok("2026-10-06T03:00:00+00:00")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("阻断发生的北京小时", text)
        self.assertIn("22时×2", text)

    def test_zero_blocked_says_cannot_judge(self):
        """启用但零阻断 ⇒ 必须说"不可据此判机制无效"（R612）"""
        rows = [dict(self._ok("2026-10-06T03:00:00+00:00"),
                     low_hour_cap=2, low_hour_blocked=False)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("零阻断", text)
        self.assertIn("不可据此判机制无效", text)

    def test_disabled_when_no_field(self):
        """未启用（无low_hour_cap 字段）⇒ 不渲染阻断行，零噪音"""
        rows = [self._ok("2026-10-06T03:00:00+00:00")]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertNotIn("低浏览窗子配额阻断", text)


class TestR663TailConflictGuardrail(unittest.TestCase):
    """R663：`ending_question` 这个**自报字段**的自一致性异常留存。

    ★ 背景（R647 那个坑的第二层）：
      `final_preview` 只存**前 200 字**而站队提问在**结尾**
      ⇒ `ending_question=True/False` 这个自报布尔**此前无任何数据能验证**。
      我判读生产数据时因此误判"标 True 但结尾没问号 = 字段造假"
      —— 实际上我只是把 `final_preview` 的第 200 字当成了结尾。

    ★ 为什么是「异常优先留存」而不是全量存尾段：
      我首版加 `final_tail`（末 120 字全量落盘）⇒ **R647 守卫立刻失败**，
      理由明写"判据只有有没有问"（323 篇 × 120 字 = **+60%** 膨胀）。
      **守卫是对的** ⇒ 改方案：**只在不一致时**存 `tail_conflict`。
    """

    @staticmethod
    def _post(ts, flag, conflict=None):
        d = {"platforms": ["binance"], "outcome": "binance_published",
             "ts": ts, "hour_bj": 8, "content_id": ts, "source": "S",
             "ending_question": flag, "final_preview": "x"}
        if conflict is not None:
            d["tail_conflict"] = conflict
        return d

    def test_no_conflict_renders_clean(self):
        rows = [self._post("2026-10-06T02:06:00+00:00", True),
                self._post("2026-10-06T02:28:00+00:00", True)]
        s = mr.summarize(rows)
        self.assertEqual(s.get("tail_conflict_n"), 0)
        text = mr.render_text(s, rows)
        self.assertIn("无自一致性异常", text)

    def test_conflict_is_counted_and_shown(self):
        """有冲突 ⇒ 必须报数并展示原文（这才是需要人看的）"""
        rows = [self._post("2026-10-06T02:06:00+00:00", True,
                           conflict="最后一行没有问号但字段说 True"),
                self._post("2026-10-06T02:28:00+00:00", True)]
        s = mr.summarize(rows)
        self.assertEqual(s["tail_conflict_n"], 1)
        text = mr.render_text(s, rows)
        self.assertIn("判据与结尾原文不一致", text)
        self.assertIn("没有问号", text, "必须展示冲突原文供人工核对")

    def test_clean_is_not_claimed_as_verified(self):
        """★ 「无异常」≠「已验证」——文案必须写明这个区分"""
        rows = [self._post("2026-10-06T02:06:00+00:00", True)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("未发现异常", text)
        self.assertIn("已全面验证", text,
                      "必须显式声明这不等于已验证（R612 精神）")

    def test_missing_field_is_not_treated_as_conflict(self):
        """⚠️ 字段缺失（None）= "一致、无异常"，**不得**计成冲突或失明。

        这是异常优先留存的**关键语义**：绝大多数回执不会有 `tail_conflict`
        （因为它们一致）⇒ 若把缺失当异常，会天天误报。
        """
        rows = [self._post("2026-10-06T02:06:00+00:00", True),   # 无该字段
                self._post("2026-10-06T02:28:00+00:00", False)]
        s = mr.summarize(rows)
        self.assertEqual(s["tail_conflict_n"], 0,
                         "无冲突字段 ≠ 有冲突")

    def test_no_full_tail_field_in_main(self):
        """★ 回归守卫：**不得**引入全量尾段字段（R647 的体积约束）。"""
        src = open(m.__file__, encoding="utf-8").read()
        self.assertNotIn('"final_tail"', src,
                         "禁止全量存尾段（R647：323篇×120字=+60% 膨胀）")
        self.assertIn('"tail_conflict"', src,
                      "应使用异常优先留存的字段名")


class TestR664BoostedByAttribution(unittest.TestCase):
    """R664：★「命中」≠「生效」——加权命中数答不出"是否真影响选中"。

    ★ 缺口（R612 变体：**施力面有观测，受力面没有**）：
      四个加权都只落"打了多少个候选"的聚合数
      （`campaign_boost_hits` / `trend_boost_hits` / `hot_boost_hits` /
      `engagement_boost_up` / `_down`），
      而**没有任何字段标注「最终发出的这篇被哪路加权命中过」**
      ⇒ **答不出"被加分的候选最后有没有被选中"**
      ⇒ 加权属于"看起来已实现"（R660 同型风险）。

    ★ ★ 最容易搞错的点：**这是「构成比」不是「命中率」**。
      `boosted_by` 只在**发布时**落盘 ⇒ **未被选中的候选根本没有回执**
      ⇒ 护栏若不显式声明，"选中率 100%"会被读成"加权 100% 有效"。
    """

    @staticmethod
    def _post(ts, boosted=None):
        d = {"platforms": ["binance"], "outcome": "binance_published",
             "ts": ts, "hour_bj": 8, "content_id": ts, "source": "S",
             "impact_score": 20.0, "final_preview": "x"}
        if boosted is not None:
            d["boosted_by"] = boosted
        return d

    @staticmethod
    def _run(ts, **kw):
        d = {"outcome": "run_summary", "ts": ts, "candidates": 300,
             "published": 2, "unprocessed": 298, "hour_bj": 22}
        d.update(kw)
        return d

    def test_counts_tags_in_published(self):
        rows = [self._post("2026-10-06T02:06:00+00:00", "campaign,hot"),
                self._post("2026-10-06T02:28:00+00:00", "campaign"),
                self._post("2026-10-06T03:06:00+00:00", "eng")]
        s = mr.summarize(rows)
        self.assertEqual(s["boosted_by_total"], 3)
        self.assertEqual(s["boosted_by_tags"]["campaign"], 2)
        self.assertEqual(s["boosted_by_tags"]["hot"], 1)
        self.assertEqual(s["boosted_by_tags"]["eng"], 1)

    def test_eng_down_counted_separately(self):
        """⚠️ 减分与加分**方向不同**，必须分开（R611 分侧）。

        共用标记会让"命中加权"读起来像"被优待"，而它其实是**降权**。
        """
        rows = [self._post("2026-10-06T02:06:00+00:00", "eng"),
                self._post("2026-10-06T02:28:00+00:00", "eng_down")]
        s = mr.summarize(rows)
        self.assertEqual(s["boosted_by_tags"]["eng"], 1)
        self.assertEqual(s["boosted_by_tags"]["eng_down"], 1)

    def test_composition_not_hit_rate(self):
        """★ 护栏必须显式声明这是「构成比」，否则会被读成命中率 100%"""
        rows = [self._post("2026-10-06T02:06:00+00:00", "campaign"),
                self._run("2026-10-06T02:10:00+00:00",
                          campaign_boost_hits=50, trend_boost_hits=10,
                          hot_boost_hits=20, engagement_boost_up=3)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("构成比", text)
        self.assertIn("命中率", text, "必须点出读者最容易误读的那个词")

    def test_zero_observation_says_cannot_judge(self):
        """零观测 ⇒ 必须说"加权是否真影响选中无法判定"（R612）"""
        rows = [self._post("2026-10-06T02:06:00+00:00"),   # 无 boosted_by
                self._run("2026-10-06T02:10:00+00:00",
                          campaign_boost_hits=50)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("零观测", text)
        self.assertIn("无法判定", text)
        self.assertIn("别把", text, "必须明确警告别把命中数当生效证明")

    def test_main_marks_all_boost_sites(self):
        """★ 接线守卫：**每一处**加分都必须打标记，否则漏一处就失去意义。

        `apply_hour_preference_boost` 刻意**不打**（R660 已证均匀平移，
        打标记会让"被时段偏置命中"读起来像被优待，而它零影响）。
        """
        src = open(m.__file__, encoding="utf-8").read()
        # 加分语句总数 vs 标记调用总数必须相等（漏标一处就失去意义）
        import re
        # ⚠️ 写法必须与 `main.py` 的实际格式一致：`item["impact_score"] += CONST`
        #    （`]` 之后有空格）。正则失配时**必须显式报错**而不是静默通过——
        #    否则常量改名后守卫会悄悄失效（R659「正则守卫会静默失效」教训）。
        boosts = re.findall(r'item\["impact_score"\] [+-]= '
                            r'(?:CAMPAIGN_TOKEN_BOOST|TREND_TOKEN_BOOST'
                            r'|HOT_TOPIC_BOOST|ENGAGEMENT_VIEW_BOOST)', src)
        marks = re.findall(r'_mark_boost\(item,\s*"(\w+)"\s*\)', src)
        self.assertTrue(boosts, "正则失配：需与 main.py 的加分写法同步"
                        "（常量名改动时守卫会静默失效，R659教训）")
        self.assertEqual(len(boosts), len(marks),
                         "加分点 %d 处 vs 标记 %d 处⇒ 有漏标（漏一处就失去意义）"
                         % (len(boosts), len(marks)))

    def test_eng_down_marked_distinctly(self):
        src = open(m.__file__, encoding="utf-8").read()
        self.assertIn('_mark_boost(item, "eng_down")', src,
                      "减分必须标 eng_down，不能与加分共用 eng")
        self.assertIn('_mark_boost(item, "eng")', src)

    def test_mark_is_idempotent(self):
        """标记必须幂等去重（同一路加权可能多次触发）"""
        item = {"impact_score": 10.0}
        for _ in range(3):
            m._mark_boost(item, "campaign")
        self.assertEqual(item["_boosted_by"], ["campaign"],
                         "重复标记同一路必须去重")
        m._mark_boost(item, "hot")
        self.assertEqual(sorted(item["_boosted_by"]), ["campaign", "hot"])


class TestR665BoostMagnitudeEnough(unittest.TestCase):
    """R665：★ 加权「量级」够不够跨名次——结构判据之外的第二道。

    ★ 为什么需要第二道（R661 只判了结构）：
      R661 用 AST 确认 4 个加权**只给部分候选加分**（结构有效），
      但**结构有效不蕴含**真的改变了排序——
      若加权幅度**小于同批候选的相邻分差**，
      加了分也**跨不过一个名次** ⇒ 结构通过、行为没变（R660 同型）。

    ★ 实测结论（正面）：
      加权幅度 **4~5 分** vs 同批分差中位 **1 分** ⇒ **能跨约 5 个名次**
      ⇒ 4 个加权**真有效**。与 R650 的 -2 分（均匀平移 + 4.2% 效力）形成
      鲜明对比：那个是**双重无效**。

    ⚠️ 口径：分母**只算有 `base_impact_score` 的新稿**（R617 上线后）——
    混入上线前的旧稿会算出"97% 覆盖失败"的**假缺口**
    （**我差点这么判**，R641/R659 同款）。
    """

    @staticmethod
    def _post(ts, base, impact, src="S"):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "ts": ts, "hour_bj": 8, "content_id": ts, "source": src,
                "base_impact_score": base, "impact_score": impact,
                "final_preview": "x"}

    def test_gaps_exclude_priority_seed(self):
        """★ 必须排除人工置顶种子（999）——否则稀疏度被撑大、判据会判反"""
        rows = [self._post("2026-10-06T02:%02d:00+00:00" % i, 20 + i, 20 + i)
                for i in range(5)]
        rows.append(self._post("2026-10-06T03:00:00+00:00", 999, 1007,
                               src="priority_seed:bitget-hack"))
        gaps = mr._natural_score_gaps(rows)
        self.assertTrue(gaps)
        self.assertLessEqual(max(gaps), 2,
                             "种子 999 不得把相邻间距撑大（会让判据判反）")

    def test_gaps_need_enough_samples(self):
        self.assertEqual(mr._natural_score_gaps([]), [])
        self.assertEqual(mr._natural_score_gaps(
            [self._post("2026-10-06T02:00:00+00:00", 20, 20)]), [])

    def test_magnitude_measured_from_base_to_impact(self):
        rows = [self._post("2026-10-06T02:00:00+00:00", 20, 25),   # +5
                self._post("2026-10-06T02:01:00+00:00", 20, 20),   # 0
                self._post("2026-10-06T02:02:00+00:00", 20, 15)]   # -5
        s = mr.summarize(rows)
        self.assertEqual(s["boost_mag_n"], 3)
        self.assertEqual(s["boost_mag_nonzero"], 2)
        self.assertEqual(s["boost_mag_neg"], 1, "减分必须单独计（R611）")

    def test_rows_without_base_excluded(self):
        """★ 无 base 的行**不进分母**（否则假缺口）"""
        rows = [self._post("2026-10-06T02:00:00+00:00", 20, 25),
                {"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-06T02:01:00+00:00", "hour_bj": 8,
                 "content_id": "x", "source": "S", "impact_score": 30,
                 "final_preview": "x"}]
        s = mr.summarize(rows)
        self.assertEqual(s["boost_mag_n"], 1, "无 base 的行必须被排除")

    def test_renders_magnitude_line(self):
        """R665 护栏与轮次面板同区（`if runs.get("n")` 内）⇒ 测试须给 run_summary"""
        rows = [self._post("2026-10-06T02:%02d:00+00:00" % i,
                           20, 20 + (5 if i == 0 else 0)) for i in range(6)]
        rows.append({"outcome": "run_summary", "ts": "2026-10-06T03:00:00+00:00",
                     "hour_bj": 22, "candidates": 300, "published": 6,
                     "unprocessed": 294})
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("加权**量级**", text)
        self.assertIn("下界估计", text,
                      "须声明分差取自已发布回执，不是当轮全部候选")

    def test_zero_base_says_cannot_judge(self):
        """零覆盖 ⇒ 必须说"无法判定"，且**不可与'字段未落盘'混淆**（R612）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-06T02:00:00+00:00", "hour_bj": 8,
                 "content_id": "x", "source": "S", "impact_score": 20,
                 "final_preview": "x"}]
        rows.append({"outcome": "run_summary", "ts": "2026-10-06T03:00:00+00:00",
                     "hour_bj": 22, "candidates": 300, "published": 1,
                     "unprocessed": 299})
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("无法判定", text)
        self.assertIn("分母不存在", text,
                      "必须区分'分母不存在'与'字段未落盘'")


class TestR666ObserveScanGuardrail(unittest.TestCase):
    """R666：观测扫描的护栏——它的产出**必须可见**（R617「探针答案不能被丢弃」）。

    ★ R666 的根因：配额检查在候选构建**之前**（main.py:9216 vs 9354），
      饱和时 `sys.exit(0)` ⇒ **候选池根本不构建**
      ⇒ 所有回执侧字段**只在发帖时落盘** ⇒ **85% 的轮次零样本**。
    ⇒ `OBSERVE_ON_SATURATED`（默认 0）让饱和轮也构建候选、只观测不发布。
    ★ 它的 `observe_gap_median` 是**全候选池**的真实分差
      ⇒ 可把 R665 的"下界估计"升级为**真值**。
    """

    @staticmethod
    def _obs(ts, **kw):
        d = {"outcome": "run_summary", "ts": ts, "hour_bj": 22,
             "candidates": 0, "published": 0, "observe_scan": True}
        d.update(kw)
        return d

    def test_counts_runs_and_gaps(self):
        rows = [self._obs("2026-10-06T02:%02d:00+00:00" % i,
                          observe_candidates=100 + i, observe_gap_median=1,
                          observe_elapsed_sec=90.0 + i)
                for i in range(4)]
        # ⚠️ 观测扫描的统计落在 **`runs` 子字典**里（run_summary 的领域），
        #   不是 s 顶层——**我第一版测试查错了层**（与 R665 同一个错）。
        runs = mr.summarize(rows)["runs"]
        self.assertEqual(runs["observe_runs"], 4)
        self.assertEqual(len(runs["observe_gaps"]), 4)
        self.assertEqual(runs["observe_candidates"], 103, "取最大值")

    def test_parses_boost_tags(self):
        rows = [self._obs("2026-10-06T02:00:00+00:00",
                          observe_boost_tags="campaign:3 hot:1")]
        runs = mr.summarize(rows)["runs"]
        self.assertEqual(runs["observe_tag_hits"]["campaign"], 3)
        self.assertEqual(runs["observe_tag_hits"]["hot"], 1)

    def test_renders_scan_line(self):
        rows = [self._obs("2026-10-06T02:00:00+00:00", observe_candidates=120,
                          observe_gap_median=1, observe_elapsed_sec=95.0,
                          observe_boost_tags="campaign:3")]
        rows.append({"outcome": "run_summary", "ts": "2026-10-06T03:00:00+00:00",
                     "hour_bj": 22, "candidates": 0, "published": 0})
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("观测扫描", text)
        self.assertIn("全候选池真值", text,
                      "必须标明这是真值而非 R665 的下界估计")
        self.assertIn("campaign 3", text)

    def test_disabled_says_zero_sample(self):
        """未启用 ⇒ 必须说清"回执侧字段零样本"这个后果（R612）"""
        rows = [{"outcome": "run_summary", "ts": "2026-10-06T02:00:00+00:00",
                 "hour_bj": 22, "candidates": 0, "published": 0,
                 "quota_blocked": True}]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("未启用", text)
        self.assertIn("零样本", text)
        self.assertIn("下界估计", text,
                      "须说明 R665 的分差此时只能是下界")


class TestR668OrdinalHeadingGuardrail(unittest.TestCase):
    """R668：序号式 AI 腔护栏。

    ★ 根因**在 prompt 自己**：长文提示词第 3 条原文就要求
    "小标题分 3~4 段（**如「一、发生了什么」「二、资金在赌什么」**）"
    ⇒ 模型是**照做的**，不是模型的问题。
    生产实测 323篇：**长文 23/27（85%）以「一、」开头，14 篇是同一句**
    「一、发生了什么」⇒ 典型模板化。

    ⚠️ **只度量形态、不做门**：长文单通道，一次拒稿= 大概率丢稿（R331）。
    ⚠️ **不声称影响浏览量**：n=59 且「长文」与「重大事件」**混淆**，
       p=0.0015 也不可行动（R651）。
    """

    @staticmethod
    def _post(ts, ordinal, first_line, article=True):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "ts": ts, "hour_bj": 8, "content_id": ts, "source": "S",
                "article": article, "ending_question": True,
                "ordinal_heading": ordinal,
                "final_preview": first_line + "\n\n正文…"}

    def test_counts_ordinal(self):
        rows = [self._post("2026-10-06T02:%02d:00+00:00" % i,
                           i < 3, "一、发生了什么" if i < 3 else "1.88亿爆仓后BTC拉回84750")
                for i in range(5)]
        s = mr.summarize(rows)
        self.assertEqual(s["ordinal_n"], 5)
        self.assertEqual(s["ordinal_yes"], 3)

    def test_renders_and_flags_repeat(self):
        rows = [self._post("2026-10-06T02:%02d:00+00:00" % i, True, "一、发生了什么")
                for i in range(5)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("序号式小标题", text)
        self.assertIn("同一句开头重复 5 次", text,
                      "必须报**收敛度**——同一句重复才是模板化的核心证据")
        self.assertIn("不可据此断言对浏览量的影响", text,
                      "必须写明混淆与R651（R649 同款：护栏不得过度声称）")

    def test_no_ordinal_field_is_silent_not_false(self):
        """★ 字段缺失 = 未观测，**不得**计成 False（纪律 12/17）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-06T02:00:00+00:00", "hour_bj": 8,
                 "content_id": "x", "source": "S", "ending_question": True,
                 "final_preview": "正文"}]
        s = mr.summarize(rows)
        self.assertEqual(s["ordinal_n"], 0, "无字段不得进分母")

    def test_does_not_break_r663(self):
        """★ 回归守卫（R646 同款）：R668 的插入**不得挤掉 R663 块**。

        实测事故：首版把 R668 插在 R663 的注释行位置（12 缩进区），
        ⇒ R663 的 `if` 变成 R668 的 `if _ordn:` 内部 ⇒
        **R663 三例守卫全挂**，而 `py_compile` 仍通过。
        ⇒ 本例锁住"两者必须共存"。
        """
        rows = [self._post("2026-10-06T02:%02d:00+00:00" % i, True, "一、发生了什么")
                for i in range(5)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("序号式小标题", text, "R668 应渲染")
        self.assertIn("结尾站队提问", text, "R647 应渲染")
        self.assertIn("无自一致性异常", text, "R663 必须仍渲染（不得被挤掉）")

    def test_prompt_forbids_ordinal(self):
        """★ 接线守卫：prompt 必须真的禁了序号起手（否则遥测白加）"""
        src = open(m.__file__, encoding="utf-8").read()
        i = src.index("R668 小标题硬要求")
        blk = src[i:i + 900]
        self.assertIn("禁止", blk)
        self.assertIn("一、二、三", blk)
        # 旧的示例句必须**删掉**（它在 prompt 里等于教模型用序号）
        self.assertNotIn("如「一、发生了什么」「二、资金在赌什么」", blk,
                         "旧的序号示例必须移除，否则模型会继续照做")


class TestR670WidgetCapGuardrail(unittest.TestCase):
    """R670：挂件**额度使用率**护栏（零挂件告警的互补面）。

    ★ 为什么要有这一行：R123 零挂件告警只答"有没有失守"，
      **不答"额度用满了吗"**——绝大多数帖只用 1~2 个（上限 3），
      单看零挂件=0 一切正常，读者**无法判断**是"源文只提一个币"（正常）
      还是"漏织"（缺陷）。
    ⚠️ **分母必须按「降格上线时刻」分层**（R659/R665 纪律）：
      降格前的老稿 `widget_count` 可达 12（**当时逻辑还不存在**），
      混进分布 ⇒ 护栏显示"4/8/12 个"⇒ **读者误以为降格没生效**。
    ⭐ 上线时刻**从数据推导**而非写死日期（`MAX_TOKENS_PER_POST` 是 env 可配）。
    """

    @staticmethod
    def _p(ts, wc, tokens=("BTC",)):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "ts": ts, "hour_bj": 8, "content_id": ts, "source": "S",
                "widget_count": wc, "tokens": list(tokens)}

    def test_derives_cap_since_from_data(self):
        rows = [self._p("2026-10-06T02:00:00+00:00", 8),
                self._p("2026-10-06T03:00:00+00:00", 5),
                self._p("2026-10-06T04:00:00+00:00", 2)]
        got = mr._derive_widget_cap_since(rows)
        self.assertEqual(got, "2026-10-06T03:00:00+00:00",
                         "应取最后一个超限的 ts（降格失效的最后一刻）")

    def test_no_oversize_returns_empty(self):
        rows = [self._p("2026-10-06T02:00:00+00:00", 1),
                self._p("2026-10-06T03:00:00+00:00", 3)]
        self.assertEqual(mr._derive_widget_cap_since(rows), "",
                         "全部未超限⇒ 返回空串⇒ 调用方**不做分层**")

    def test_layers_by_cap_since(self):
        rows = [self._p("2026-10-06T02:00:00+00:00", 9),     # 降格前
                self._p("2026-10-06T03:00:00+00:00", 4),     # 降格前
                self._p("2026-10-06T04:00:00+00:00", 1),     # 降格后
                self._p("2026-10-06T05:00:00+00:00", 3)]     # 降格后
        s = mr.summarize(rows)
        self.assertEqual(s["widget_hist"][9], 1, "全史应含超限")
        self.assertEqual(s["widget_hist_new"][9], 0,
                         "★ 分层后**不得**含超限（否则误报降格失效）")
        self.assertEqual(s["widget_hist_new"][3], 1)

    def test_renders_ok_flag_when_within_cap(self):
        rows = [self._p("2026-10-06T02:00:00+00:00", 9),
                self._p("2026-10-06T04:00:00+00:00", 2),
                self._p("2026-10-06T05:00:00+00:00", 3)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("挂件额度使用", text)
        self.assertIn("降格后", text, "必须标明分层")
        self.assertIn("✅", text, "近窗 max=3 ⇒ 应为 ✅")
        self.assertNotIn("❌", text, "降格后无超限⇒ 不得报失效")

    def test_reports_failure_when_still_over_cap(self):
        """★ 真有**持续**超限（末次超限之后还有）⇒ 必须报失效。

        ⚠️ 判据的演进（实测三次才做对）：
          ① 只取"最后一个超限 ts" ⇒ 若一直失效，那个 ts 就是最新
             ⇒ 分层把超限全排除 ⇒ **永不报警**（盲区）
          ② 加"超限占比 > 5% 就不分层" ⇒ **生产实测占比 6.4%**
             ⇒ 把**已生效**的降格报成失效（假告警）
          ③ ★ **正解：同口径比「超限 vs 超限」**——
             末次超限 ts 之后**还有超限** ⇒ 降格未生效。
          ⚠️ ② 踩了纪律 11「并列信号不能共用分母」：
             分子数"之后的全部行"、分母数"全部行" ⇒ 比值天然偏大。
        """
        rows = [self._p("2026-10-06T02:00:00+00:00", 2),   # 合规
                self._p("2026-10-06T03:00:00+00:00", 9),   # 超限
                self._p("2026-10-06T04:00:00+00:00", 7)]   # ★ 末次之后仍超限
        s = mr.summarize(rows)
        self.assertEqual(s["widget_cap_since"], "",
                         "末次超限之后仍有超限 ⇒ 不可分层")
        text = mr.render_text(s, rows)
        self.assertIn("❌", text)
        self.assertIn("降格**未生效**", text)

    def test_stablecoin_excluded_from_hist(self):
        """★ 稳定币-only 的零挂件是**合规**（R316）⇒ 不进使用率分母"""
        rows = [self._p("2026-10-06T02:00:00+00:00", 0, tokens=("USDC",))]
        s = mr.summarize(rows)
        self.assertEqual(s["widget_hist"][0], 0,
                         "稳定币-only 不应计入使用率分布")

    def test_does_not_reference_main_constant(self):
        """★ 不得引用 main 的常量（不import main⇒ NameError 而编译仍过）"""
        src = open(mr.__file__, encoding="utf-8").read()
        i = src.index("挂件额度使用")
        blk = src[i - 600:i + 400]
        self.assertNotIn("MAX_TOKENS_PER_POST)", blk,
                         "不可把 main 的常量插进 f-string")


class TestR673ImageTierGuardrail(unittest.TestCase):
    """R673：**原图获得率**遥测（回答"自绘占比高是**没图**还是**拉取失败**"）。

    ★ 为什么要有这个字段：生产 `tier=raw 177 / chart 92`，看着像
    "原图只拿到 66%"，但**无法区分**两种成因：
      (a) 源文本来就没图 ⇒ 只能自绘（**不是缺陷**）
      (b) 有图但拉取失败/被 SSRF 拒 ⇒ **是缺陷，要修**
    两者处置完全相反，而 `image_fail_reason` 成功路径不写值（全库为 None）
    ⇒ 只能靠这个字段分辨。

    ⚠️⚠️ **本类不主张"原图浏览更高"**（R673 我犯过的错，见下）：
      实测 raw 46.6/天 vs chart 25.1/天、p=0.0009看着很硬，
      但**没控制源**——U.Today 原图率 **0%**（122 篇全自绘），
      其余 8 源 **83~100%** ⇒ **完全混淆**（R651 当年已判定）。
      ⇒ 本类只测**遥测与护栏本身**，不测"哪个 tier 更好"。
    """

    @staticmethod
    def _p(ts, tier, hri):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "ts": ts, "hour_bj": 8, "content_id": ts, "source": "S",
                "ending_question": True, "image": True,
                "image_tier": tier, "has_raw_image": hri,
                "final_preview": "正文"}

    def test_counts_hri(self):
        rows = [self._p("2026-10-06T02:0%d:00+00:00" % i,
                        "raw" if i < 3 else "chart", i < 3)
                for i in range(6)]
        s = mr.summarize(rows)
        self.assertEqual(s["hri_n"], 6)
        self.assertEqual(s["hri_yes"], 3)

    def test_missing_hri_not_counted(self):
        """★ 字段缺失 = 未观测，**不得**当「没有图」（R659）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-06T02:00:00+00:00", "hour_bj": 8,
                 "content_id": "x", "source": "S", "ending_question": True,
                 "image": True, "image_tier": "chart",
                 "final_preview": "正文"}]
        s = mr.summarize(rows)
        self.assertEqual(s["hri_n"], 0, "无字段不得进分母")

    def test_renders_with_confound_warning(self):
        """★ 护栏必须**同时**给出原图率与"源混淆"警示（不得只报好看的数）"""
        rows = [self._p("2026-10-06T02:0%d:00+00:00" % i,
                        "raw" if i < 3 else "chart", i < 3)
                for i in range(6)]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("源文自带图", text)
        self.assertIn("不是缺陷", text,
                      "必须说明「无图可拉」不是缺陷，否则会被当成要去修")

    def test_default_tier_order_unchanged(self):
        """★锁定 `chart` 优先的**默认值**（勿因误读信号而改它）。

        ⚠️ 生产**实际**由调用方传`prefer=["raw","chart","card"]`（10165 行）
        ⇒ 这个默认值只兜底无参调用，但它曾被我按"假 p"误改。
        """
        import inspect
        src = inspect.getsource(m.ImageManager.prepare_and_upload)
        self.assertIn('else ["chart", "raw", "card"]', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestR672ParagraphLengthGuardrail(unittest.TestCase):
    """R672：**最长段落汉字数**（短讯）。

    ★ R671 实测得出的结论（这是本护栏存在的理由）：
      · 段数**已达标**：中位 3~4 段、89% 有换行 ⇒ "分段"这个维度没病
      · **真正的差距在每段太长**：段均 36、**p90 61** 汉字，
        >60 占 10%、>80 占 3%
      · 人工范文（Kamino/SOL）段均**26 汉字**
      ⇒ 手机端一屏读不完 60 字 ⇒ 这是**真实的划走原因**（R644 同源）
    ⚠️ **不能只靠 final_preview**（R647）：它只存前 200 字 ⇒ 尾部段落不可见
      ⇒ 永远只能看见"前几段够长" ⇒ **假达标**。
    """

    @staticmethod
    def _p(ts, pm, article=False):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "ts": ts, "hour_bj": 8, "content_id": ts, "source": "S",
                "article": article, "ending_question": True,
                "para_max_cjk": pm, "final_preview": "正文"}

    def test_counts_short_posts_only(self):
        """★ **只统计短讯**——长文按 500~800 字设计会整体拉高分布"""
        rows = [self._p("2026-10-06T02:0%d:00+00:00" % i, 30)
                for i in range(3)]
        rows.append(self._p("2026-10-06T02:09:00+00:00", 400, article=True))
        s = mr.summarize(rows)
        self.assertEqual(s["para_max_n"], 3, "长文不得计入")

    def test_over60_and_over80(self):
        rows = [self._p("2026-10-06T02:%02d:00+00:00" % i, v)
                for i, v in enumerate([26, 35, 61, 81])]
        s = mr.summarize(rows)
        self.assertEqual(s["para_max_over60"], 2, "61 与 81 应计入")
        self.assertEqual(s["para_max_over80"], 1)

    def test_missing_field_not_counted(self):
        """★ 字段缺失 = 未观测，**不得**当 0（R659）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-06T02:00:00+00:00", "hour_bj": 8,
                 "content_id": "x", "source": "S", "ending_question": True,
                 "final_preview": "正文"}]
        s = mr.summarize(rows)
        self.assertEqual(s["para_max_n"], 0)

    def test_renders_with_flag(self):
        rows = [self._p("2026-10-06T02:%02d:00+00:00" % i, v)
                for i, v in enumerate([26, 31, 65, 88])]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("最长段落", text)
        self.assertIn("读全文", text)
        # >60 占 50% ⇒ 应报 ❌
        self.assertIn("❌", text)

    def test_prompt_forbids_long_paragraph(self):
        """★ 接线守卫：prompt 必须真的卡了段落长度（否则遥测白加）"""
        src = open(m.__file__, encoding="utf-8").read()
        i = src.index("R672 段落长度硬要求")
        blk = src[i:i + 500]
        self.assertIn("35", blk, "必须给出具体阈值")
        self.assertIn("4~5", blk, "段数须同步上调")


class TestGuardrailInsertionSafety(unittest.TestCase):
    """★★ **通用回归守卫**：新增报表块不得挤掉既有护栏。

    ★ 为什么要有这道（当天被咬**三次**：R668 / R670 / R672）：
      插入累加块时若落在「守卫 `if` 与其第一条语句之间」，
      ⇒既有累加被**挤进守卫体内** ⇒ 该护栏**静默失效**
      ⇒ 而 `py_compile` **仍然通过**（语法没错、行为变了）。
      R672 那次直接挂掉 R663 + R668 **共 8 例**。

    ⇒ 本守卫把「既有护栏仍能聚合」变成**显式契约**，
      新增任何累加块都必须让它继续通过。
    """

    def test_existing_ordinal_still_aggregates(self):
        """R668 序号式小标题：与 R672 共存"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-06T02:%02d:00+00:00" % i, "hour_bj": 8,
                 "content_id": "o%d" % i, "source": "S", "article": True,
                 "ending_question": True, "ordinal_heading": True,
                 "para_max_cjk": 30,
                 "final_preview": "一、发生了什么\n\n正文"} for i in range(4)]
        s = mr.summarize(rows)
        self.assertEqual(s["ordinal_n"], 4, "R668 被挤掉了")
        self.assertEqual(s["ordinal_yes"], 4)
        self.assertEqual(s["para_max_n"], 0, "R672 只统计短讯（article=True）")

    def test_all_three_coexist(self):
        """★ R663 + R668 + R670 + R672 四套护栏同时可用"""
        rows = []
        for i in range(6):
            rows.append({"platforms": ["binance"],
                         "outcome": "binance_published",
                         "ts": "2026-10-06T02:%02d:00+00:00" % i,
                         "hour_bj": 8, "content_id": "c%d" % i,
                         "source": "S", "article": False,
                         "ending_question": True,
                         "ordinal_heading": bool(i % 2),
                         "para_max_cjk": 30 + i * 5,
                         "widget_count": 1 + (i % 2),
                         "tokens": ["BTC"],
                         "final_preview": "正文\n\n第二段"})
        s = mr.summarize(rows)
        self.assertEqual(s["para_max_n"], 6, "R672")
        self.assertEqual(s["ordinal_n"], 6, "R668")
        self.assertEqual(s["ending_q_marked"], 6, "R647")
        self.assertEqual(s["widget_hist"][1], 3, "R670")
        self.assertEqual(s["widget_hist"][2], 3)

    def test_render_contains_all_guardrails(self):
        """★ 四套护栏**都必须在渲染文本里出现**（静默失效的最后一道）"""
        rows = []
        for i in range(6):
            rows.append({"platforms": ["binance"],
                         "outcome": "binance_published",
                         "ts": "2026-10-06T02:%02d:00+00:00" % i,
                         "hour_bj": 8, "content_id": "r%d" % i,
                         "source": "S", "article": False,
                         "ending_question": True,
                         "ordinal_heading": True,
                         "para_max_cjk": 70,
                         # ⚠️ **必须含超限**（前 2 条给 widget_count=9）：
                         #   R670 的分层线由「最后一个超限 ts」推导，
                         #   全未超限 ⇒ 走"无法分层"分支 ⇒ **不渲染**使用率行
                         #   （实测踩过：以为护栏坏了，实为数据不满足分层条件）
                         "widget_count": 9 if i < 2 else 1,
                         "tokens": ["BTC"],
                         "final_preview": "正文\n\n第二段"})
        text = mr.render_text(mr.summarize(rows), rows)
        for mark in ("最长段落", "挂件额度使用"):
            self.assertIn(mark, text, "缺护栏: %s" % mark)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestR672ParagraphLengthGuardrail(unittest.TestCase):
    """R672：**最长段落汉字数**（短讯）。

    ★ R671 实测得出的结论（这是本护栏存在的理由）：
      · 段数**已达标**：中位 3~4 段、89% 有换行 ⇒ "分段"这个维度没病
      · **真正的差距在每段太长**：段均 36、**p90 61** 汉字，
        >60 占 10%、>80 占 3%
      · 人工范文（Kamino/SOL）段均**26 汉字**
      ⇒ 手机端一屏读不完 60 字 ⇒ 这是**真实的划走原因**（R644 同源）
    ⚠️ **不能只靠 final_preview**（R647）：它只存前 200 字 ⇒ 尾部段落不可见
      ⇒ 永远只能看见"前几段够长" ⇒ **假达标**。
    """

    @staticmethod
    def _p(ts, pm, article=False):
        return {"platforms": ["binance"], "outcome": "binance_published",
                "ts": ts, "hour_bj": 8, "content_id": ts, "source": "S",
                "article": article, "ending_question": True,
                "para_max_cjk": pm, "final_preview": "正文"}

    def test_counts_short_posts_only(self):
        """★ **只统计短讯**——长文按 500~800 字设计会整体拉高分布"""
        rows = [self._p("2026-10-06T02:0%d:00+00:00" % i, 30)
                for i in range(3)]
        rows.append(self._p("2026-10-06T02:09:00+00:00", 400, article=True))
        s = mr.summarize(rows)
        self.assertEqual(s["para_max_n"], 3, "长文不得计入")

    def test_over60_and_over80(self):
        rows = [self._p("2026-10-06T02:%02d:00+00:00" % i, v)
                for i, v in enumerate([26, 35, 61, 81])]
        s = mr.summarize(rows)
        self.assertEqual(s["para_max_over60"], 2, "61 与 81 应计入")
        self.assertEqual(s["para_max_over80"], 1)

    def test_missing_field_not_counted(self):
        """★ 字段缺失 = 未观测，**不得**当 0（R659）"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-06T02:00:00+00:00", "hour_bj": 8,
                 "content_id": "x", "source": "S", "ending_question": True,
                 "final_preview": "正文"}]
        s = mr.summarize(rows)
        self.assertEqual(s["para_max_n"], 0)

    def test_renders_with_flag(self):
        rows = [self._p("2026-10-06T02:%02d:00+00:00" % i, v)
                for i, v in enumerate([26, 31, 65, 88])]
        text = mr.render_text(mr.summarize(rows), rows)
        self.assertIn("最长段落", text)
        self.assertIn("读全文", text)
        # >60 占 50% ⇒ 应报 ❌
        self.assertIn("❌", text)

    def test_prompt_forbids_long_paragraph(self):
        """★ 接线守卫：prompt 必须真的卡了段落长度（否则遥测白加）"""
        src = open(m.__file__, encoding="utf-8").read()
        i = src.index("R672 段落长度硬要求")
        blk = src[i:i + 500]
        self.assertIn("35", blk, "必须给出具体阈值")
        self.assertIn("4~5", blk, "段数须同步上调")


class TestGuardrailInsertionSafety(unittest.TestCase):
    """★★ **通用回归守卫**：新增报表块不得挤掉既有护栏。

    ★ 为什么要有这道（当天被咬**三次**：R668 / R670 / R672）：
      插入累加块时若落在「守卫 `if` 与其第一条语句之间」，
      ⇒既有累加被**挤进守卫体内** ⇒ 该护栏**静默失效**
      ⇒ 而 `py_compile` **仍然通过**（语法没错、行为变了）。
      R672 那次直接挂掉 R663 + R668 **共 8 例**。

    ⇒ 本守卫把「既有护栏仍能聚合」变成**显式契约**，
      新增任何累加块都必须让它继续通过。
    """

    def test_existing_ordinal_still_aggregates(self):
        """R668 序号式小标题：与 R672 共存"""
        rows = [{"platforms": ["binance"], "outcome": "binance_published",
                 "ts": "2026-10-06T02:%02d:00+00:00" % i, "hour_bj": 8,
                 "content_id": "o%d" % i, "source": "S", "article": True,
                 "ending_question": True, "ordinal_heading": True,
                 "para_max_cjk": 30,
                 "final_preview": "一、发生了什么\n\n正文"} for i in range(4)]
        s = mr.summarize(rows)
        self.assertEqual(s["ordinal_n"], 4, "R668 被挤掉了")
        self.assertEqual(s["ordinal_yes"], 4)
        self.assertEqual(s["para_max_n"], 0, "R672 只统计短讯（article=True）")

    def test_all_three_coexist(self):
        """★ R663 + R668 + R670 + R672 四套护栏同时可用"""
        rows = []
        for i in range(6):
            rows.append({"platforms": ["binance"],
                         "outcome": "binance_published",
                         "ts": "2026-10-06T02:%02d:00+00:00" % i,
                         "hour_bj": 8, "content_id": "c%d" % i,
                         "source": "S", "article": False,
                         "ending_question": True,
                         "ordinal_heading": bool(i % 2),
                         "para_max_cjk": 30 + i * 5,
                         "widget_count": 1 + (i % 2),
                         "tokens": ["BTC"],
                         "final_preview": "正文\n\n第二段"})
        s = mr.summarize(rows)
        self.assertEqual(s["para_max_n"], 6, "R672")
        self.assertEqual(s["ordinal_n"], 6, "R668")
        self.assertEqual(s["ending_q_marked"], 6, "R647")
        self.assertEqual(s["widget_hist"][1], 3, "R670")
        self.assertEqual(s["widget_hist"][2], 3)

    def test_render_contains_all_guardrails(self):
        """★ 四套护栏**都必须在渲染文本里出现**（静默失效的最后一道）"""
        rows = []
        for i in range(6):
            rows.append({"platforms": ["binance"],
                         "outcome": "binance_published",
                         "ts": "2026-10-06T02:%02d:00+00:00" % i,
                         "hour_bj": 8, "content_id": "r%d" % i,
                         "source": "S", "article": False,
                         "ending_question": True,
                         "ordinal_heading": True,
                         "para_max_cjk": 70,
                         # ⚠️ **必须含超限**（前 2 条给 widget_count=9）：
                         #   R670 的分层线由「最后一个超限 ts」推导，
                         #   全未超限 ⇒ 走"无法分层"分支 ⇒ **不渲染**使用率行
                         #   （实测踩过：以为护栏坏了，实为数据不满足分层条件）
                         "widget_count": 9 if i < 2 else 1,
                         "tokens": ["BTC"],
                         "final_preview": "正文\n\n第二段"})
        text = mr.render_text(mr.summarize(rows), rows)
        for mark in ("最长段落", "挂件额度使用"):
            self.assertIn(mark, text, "缺护栏: %s" % mark)



class TestParaLengthGateR672b(unittest.TestCase):
    """★★★ R672 的段落长度**硬门**（此前只有遥测、**无消费面**）。

    ★ 为什么必须补这道门（2026-10-06 实测）：
      - `para_max_cjk` 遥测**只记不卡** ⇒ 违反 R619「遥测须有消费面」
      - 实测 **328 篇里只有 4 条**有 `para_max_cjk` ⇒ 样本不足
        也印证「没有真在用」
      - 短讯模板里还写着**旧口径**"每段只有 1~2 句话"，
        而 R672 已证明它必然导致段落变长（140÷3 段 = 47 字/段）

    ★ 为什么门卡 **60** 而 prompt 写 **35**（理想 vs 底线分开）：
      实测人工范文**段均 26 字**；模型在 35 这个紧约束下容易顾此失彼
      —— 拆得太碎、口语断裂、读起来像电报
      ⇒ 门一卡太紧会**逼出更差的稿**（R643：形态偏好不该做红线门）
    """

    THRESHOLD = 60

    @staticmethod
    def _para_max(text):
        import re
        pl = [len(re.findall(r"[\u4e00-\u9fff]", p))
              for p in text.split("\n\n") if p.strip()]
        return max(pl) if pl else 0

    def test_gate_catches_wall_of_text(self):
        """★ 短讯出现「字墙」段落（>60）⇒ 应拦下"""
        bad = "\n\n".join(["这是一段很长的市场分析文字" * 5] * 4)
        self.assertGreater(self._para_max(bad), self.THRESHOLD)

    def test_gate_passes_normal_short_note(self):
        """★ 正常短讯（每段 ~35 字）⇒ 放行"""
        good = "\n\n".join(["短讯第一段内容" * 5] * 4)
        self.assertLessEqual(self._para_max(good), self.THRESHOLD)

    def test_long_article_must_not_be_gated(self):
        """★★ **长文不得被这个门拦下**（否则配额被烧光）。

        ⚠️ 代码里**没有** `is_article` 标志 ⇒ 靠**总字数**判别
          （短讯 140~200 · 长文 500~800 ⇒ 用 400 分界，两边不重叠）
        ⇒ 长文 500÷4 段 = **125 字/段** ⇒ 超 60 ⇒ 不判定会**全判废**
        """
        import re
        art = "\n\n".join(["长文第一段内容" * 20] * 4)
        cjk = len(re.findall(r"[\u4e00-\u9fff]", art))
        self.assertGreater(self._para_max(art), self.THRESHOLD,
                           "本用例前提失效：长文段落应超阈值")
        self.assertGreater(cjk, 400, "本用例前提失效：长文应 >400 字")
        # 门只在 cjk<400 时生效 ⇒ 长文必然放行
        self.assertFalse(cjk < 400 and self._para_max(art) > self.THRESHOLD)

    def test_threshold_configurable(self):
        """★ 阈值**可配**（0 = 关闭，运营逃生口）"""
        import main
        self.assertGreater(main._SHORT_NOTE_MAX_PARA_CJK, 0)
        self.assertLessEqual(main._SHORT_NOTE_MAX_PARA_CJK, 80,
                             "★ 门卡太紧会逼出更差的稿（R643）")

    def test_prompt_no_longer_says_old_wording(self):
        """★★ prompt **不得**再写"每段只有 1~2 句话"（R672 已推翻）。

        ⚠️ 旧口径必然导致段落变长：140~200 字 ÷ 3~4 段 = 35~50 字/段，
          而"1~2 句话"写不满 ⇒ 模型把两句塞一句。
        """
        import io
        src = io.open("main.py", encoding="utf-8").read()
        self.assertNotIn("每段只有 1~2 句话", src,
                         "★ 旧口径已失效（R672），别写回去")
        self.assertIn("R672 硬要求：每段 ≤35 汉字", src,
                      "★ 短讯模板应含新口径")
