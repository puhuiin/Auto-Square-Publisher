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
        # helper 无 last_seen 时向后兼容（不带日期，不炸）
        self.assertEqual(
            mr._format_permanent_failures(s["permanent_failures"]).count("最近"), 0)

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
        self.assertEqual(mr._format_permanent_failures({}), "")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
