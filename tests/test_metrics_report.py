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
        # R628：文案带分母与覆盖率——「N 篇有记录」会被读成"共 N 篇"
        self.assertIn("内容数据（6/6 篇已发布帖有浏览数据（100%））", text)
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

    def test_r602_style_dimension_view_attribution(self):
        """R602：文风维度 × 浏览——R521/R288/R130/R592 轮换的开场钩子/人设/结尾/
        实操角度各自的真实浏览量要能分桶，否则这些文风旋钮即便拿到互动数据也无从
        判断「哪种套路带流量」。按已落盘的轮换标签分桶、空标签不进分母。"""
        style_rows = [
            {"platforms": ["binance"], "outcome": "binance_published", "content_id": "s1",
             "hour_bj": 21, "article": False, "source": "U.Today",
             "opening_hook": "反差冲击", "persona": "毒舌老韭菜",
             "ending_style": "灵魂拷问", "trade_cta_style": "失效位优先"},
            {"platforms": ["binance"], "outcome": "binance_published", "content_id": "s2",
             "hour_bj": 22, "article": False, "source": "U.Today",
             "opening_hook": "反差冲击", "persona": "数据拆解派",
             "ending_style": "灵魂拷问", "trade_cta_style": "风险先说"},
            {"platforms": ["binance"], "outcome": "binance_published", "content_id": "s3",
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
        self.assertEqual(sorted(summ["stats_by_hook"]["反差冲击"]), [100, 300])
        self.assertEqual(summ["stats_by_hook"]["悬念设问"], [800])
        self.assertEqual(sorted(summ["stats_by_persona"]["毒舌老韭菜"]), [100, 800])
        self.assertEqual(summ["stats_by_ending"]["灵魂拷问"], [100, 300])
        self.assertEqual(summ["stats_by_cta"]["失效位优先"], [100])
        self.assertNotIn("", summ["stats_by_cta"], "缺失标签不得建空桶")
        self.assertEqual(len(summ["stats_by_cta"]), 2, "s3 无 trade_cta_style 不进 CTA 分母")
        text = mr.render_text(summ, style_rows)
        self.assertIn("开场钩子均浏览", text)
        self.assertIn("人设均浏览", text)
        self.assertIn("结尾套路均浏览", text)
        self.assertIn("实操角度均浏览", text)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
