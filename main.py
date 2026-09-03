#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
==============================================================================
币安广场（Binance Square）全币种热点·山寨爆款·创作者活动智能变现系统 (Ultimate Edition)
==============================================================================
核心能力升级：
1. 🌐 全币种流量雷达（主流 + 热门山寨 Altcoins + Meme 币 + 新币/次新币）：
   - 覆盖主流币 ($BTC, $ETH, $BNB, $SOL) 以及全网高流量山寨币 ($PEPE, $WIF, $DOGE, $SHIB, $SUI, $TAO, $RENDER, $NEAR, $APT 等)。
   - 扩展 9 大全球加密媒体源（涵盖 CryptoPotato, U.Today, DailyHodl, CryptoSlate 等山寨/Meme 爆款阵地）。
2. 资深实战交易员人设与去AI套路引擎 (Organic Trader Persona)：
   - 彻底告别千篇一律的死板模板，以资深操盘手口吻进行第一性原理深度拆解。
   - 包含：事件穿透本质、资金盘面与庄家博弈推演、实战交易应对思路、接地气的高手互动讨论。
3. 🎯 币安官方活动与激励感知 (AI Campaign Scanner & Analyzer)：
   - 自动扫描币安官方最新竞赛（Catalog 93）、合约上线（Catalog 48）、新币/理财（Catalog 49）。
   - AI 提取当期重点扶持币种与官方流量标签（#Write2Earn 等），使每篇发帖紧扣官方奖励。
4. 📊 实时盘面与全网情绪注入 (Live Market & Sentiment Context)：
   - 自动抓取全网恐慌与贪婪指数（Fear & Greed Index）。
   - 自动动态查询任意涉及代币在币安的实时 24H 盘面行情（价格、涨跌幅）。
5. 🔥 重磅热点与暴涨山寨价值打分器 (Breaking News Impact Scorer)：
   - 引入山寨爆款、Meme 热度、新币上线、大额解锁、资金异动等加权算法，优先捕捉流量最大的热点。
6. ✅ 动态全币种交易标的防幻觉校验器 (Symbol & Widget Validator)：
   - 自动校验提取的 $TOKEN 是否为币安真实交易对，确保 100% 触发 Write to Earn 交易挂件与返佣。
7. 🔄 多 LLM 模型池与自动故障转移 (Auto-Failover)：
   - 支持 OpenRouter (minimax-m3:free), B.ai (glm-5.3-flash), xkiro, aihubmix, inferera, TokenRouter, DeepSeek, 硅基流动等。
8. 🚨 多渠道异常报警系统 (Notifier)：
   - 支持微信 (Server酱/PushPlus)、Bark iOS、Telegram、通用 Webhook 实时通知与崩溃告警。
9. 0 服务器成本：基于 GitHub Actions 定时触发，通过 Git 状态回写持久化。
==============================================================================
"""

import os
import re
import sys
import json
import time
import random
import hashlib
import logging
from typing import List, Dict, Any, Optional, Set
from datetime import datetime

import requests
import feedparser
from openai import OpenAI

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SquarePosterUltimate")

# ---------------------------------------------------------------------------
# 常量与路径
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "sent_cache.json")
CAMPAIGN_INTEL_FILE = os.path.join(BASE_DIR, "campaign_intel.json")
MAX_CACHE_SIZE = 500
INTEL_EXPIRE_HOURS = 12  # 活动情报缓存有效期 12 小时

# 币安广场 OpenAPI 官方端点
BINANCE_SQUARE_API_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"

# ---------------------------------------------------------------------------
# 常用预置模型提供商模板
# ---------------------------------------------------------------------------
PRESET_PROVIDERS = {
    "openrouter": {
        "name": "OpenRouter (免费模型池)",
        "base_url": "https://openrouter.ai/api/v1",
        "default_models": [
            "minimax/minimax-m3:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-r1:free",
            "google/gemini-2.0-flash-exp:free",
        ],
    },
    "b.ai": {
        "name": "B.ai",
        "base_url": "https://api.b.ai/v1",
        "default_models": [
            "glm-5.3-flash",
            "deepseek-v4-flash",
            "qwen3.8-flash",
            "deepseek-v4-flash-vision-exp",
        ],
    },
    "deepseek": {
        "name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com",
        "default_models": ["deepseek-chat"],
    },
    "xkiro": {
        "name": "xkiro (免费模型)",
        "base_url": "https://api.xkiro.com/v1",
        "default_models": [
            "qwen/qwen3.8-max:free",
            "minimax/minimax-m3:free",
        ],
    },
    "aihubmix": {
        "name": "aihubmix (免费模型)",
        "base_url": "https://aihubmix.com/v1",
        "default_models": [
            "coding-glm-5.3-flash-free",
            "gemini-3.7-flash-free",
            "minimax-m3-free",
            "coding-kimi-k3-free",
        ],
    },
    "inferera": {
        "name": "inferera (免费模型)",
        "base_url": "https://api.inferera.com/v1",
        "default_models": [
            "coding-kimi-k3-free",
            "gemini-3.7-flash-free",
            "minimax-m3-free",
            "coding-glm-5.3-flash-free",
        ],
    },
    "tokenrouter": {
        "name": "TokenRouter (免费模型)",
        "base_url": "https://api.tokenrouter.com/v1",
        "default_models": [
            "qwen/qwen3.8-max-free",
            "z-ai/glm-5.3-free",
        ],
    },
    "siliconflow": {
        "name": "SiliconFlow (硅基流动)",
        "base_url": "https://api.siliconflow.cn/v1",
        "default_models": [
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen2.5-7B-Instruct",
            "THUDM/glm-4-9b-chat",
        ],
    },
}

# ---------------------------------------------------------------------------
# 全球主流 + 山寨币/Meme/新叙事 RSS 数据源列表
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    {
        "name": "CryptoPotato (山寨币/Meme热点)",
        "url": "https://cryptopotato.com/feed/",
        "lang": "en",
    },
    {
        "name": "U.Today (Meme币/DOGE/SHIB/SOL/XRP热点)",
        "url": "https://u.today/rss",
        "lang": "en",
    },
    {
        "name": "DailyHodl (山寨异动与百倍币叙事)",
        "url": "https://dailyhodl.com/feed/",
        "lang": "en",
    },
    {
        "name": "CryptoSlate (新赛道与代币经济)",
        "url": "https://cryptoslate.com/feed/",
        "lang": "en",
    },
    {
        "name": "BlockTempo (动区动趋中文)",
        "url": "https://www.blocktempo.com/feed/",
        "lang": "zh",
    },
    {
        "name": "Cointelegraph (全球综合快讯)",
        "url": "https://cointelegraph.com/rss",
        "lang": "en",
    },
    {
        "name": "CoinDesk (权威宏观与机构)",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "lang": "en",
    },
    {
        "name": "Decrypt (Web3/AI/Meme)",
        "url": "https://decrypt.co/feed",
        "lang": "en",
    },
    {
        "name": "Bitcoin Magazine (比特币核心)",
        "url": "https://bitcoinmagazine.com/.rss/full/",
        "lang": "en",
    },
]

# 重磅热点与高流量山寨打分关键词加权字典
IMPACT_KEYWORDS = {
    # 爆款山寨与 Meme 赛道
    "meme": 10,
    "memecoin": 10,
    "pepe": 10,
    "doge": 10,
    "shib": 10,
    "wif": 10,
    "bonk": 10,
    "floki": 10,
    "popcat": 10,
    "solana": 9,
    "sui": 9,
    "ton": 9,
    "ai": 9,
    "depin": 8,
    "rwa": 8,
    "layer2": 7,
    # 爆发性与行情异动
    "暴涨": 10,
    "暴跌": 10,
    "surge": 9,
    "plunge": 9,
    "rally": 9,
    "skyrocket": 10,
    "crash": 9,
    "突破": 8,
    "新高": 9,
    "ath": 9,
    "爆仓": 9,
    "清算": 9,
    "翻倍": 9,
    "10x": 9,
    "100x": 9,
    # 上线、新币与空投
    "launchpool": 12,
    "megadrop": 12,
    "listing": 10,
    "上线": 10,
    "新币": 10,
    "airdrop": 9,
    "空投": 9,
    "unlock": 9,
    "解锁": 9,
    "staking": 7,
    "质押": 7,
    "burn": 8,
    "销毁": 8,
    # 监管与宏观
    "etf": 12,
    "sec": 10,
    "fed": 10,
    "美联储": 10,
    "降息": 10,
    "options": 9,
    "期权": 9,
    # 资金与大户
    "whale": 8,
    "巨鲸": 8,
    "融资": 7,
    "黑客": 8,
}


# ---------------------------------------------------------------------------
# 模块一：实时行情与全网情绪提供器 (MarketDataProvider)
# ---------------------------------------------------------------------------
class MarketDataProvider:
    """获取加密货币全网宏观情绪与任意代币币安实时 24H 盘面价格数据"""

    @staticmethod
    def get_fear_and_greed() -> str:
        """获取全网恐慌与贪婪指数"""
        try:
            r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=4)
            if r.status_code == 200:
                data = r.json().get("data", [{}])[0]
                val = data.get("value", "50")
                cls = data.get("value_classification", "Neutral")
                return f"{val}/100 ({cls})"
        except Exception:
            pass
        return "50/100 (中立)"

    @staticmethod
    def get_token_market_data(symbols: List[str]) -> str:
        """动态批量获取指定代币（主流或山寨）在币安的实时价格与 24H 涨跌幅数据"""
        results = []
        for symbol in symbols[:4]:
            clean_sym = symbol.replace("$", "").upper()
            pair = f"{clean_sym}USDT"
            try:
                url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={pair}"
                r = requests.get(url, timeout=4)
                if r.status_code == 200:
                    d = r.json()
                    price = float(d.get("lastPrice", 0))
                    chg = float(d.get("priceChangePercent", 0))
                    sign = "+" if chg > 0 else ""
                    if price > 100:
                        price_str = f"${price:,.2f}"
                    elif price > 1:
                        price_str = f"${price:.4f}"
                    else:
                        price_str = f"${price:.6f}"
                    results.append(f"${clean_sym}: {price_str} (24H: {sign}{chg:.2f}%)")
            except Exception:
                pass
        return " | ".join(results) if results else ""


# ---------------------------------------------------------------------------
# 模块二：币安交易标的有效性校验器 (SymbolValidator)
# ---------------------------------------------------------------------------
class SymbolValidator:
    """校验提取的代币是否在币安真实上线，防止幻觉生成假标的"""

    _valid_symbols_cache: Optional[Set[str]] = None

    @classmethod
    def get_valid_symbols(cls) -> Set[str]:
        if cls._valid_symbols_cache is not None:
            return cls._valid_symbols_cache

        valid_set = {
            "BTC", "ETH", "BNB", "SOL", "DOGE", "XRP", "PEPE", "SHIB", "WIF", "SUI", 
            "NEAR", "APT", "AVAX", "LINK", "TRX", "ADA", "TAO", "RENDER", "FET", "POPCAT",
            "BONK", "FLOKI", "SEI", "TIA", "ENA", "NOT", "DOGS", "TURBO", "NEIRO", "PNUT",
            "BOME", "MEME", "ORDI", "SATS", "LTC", "BCH", "DOT", "UNI", "AAVE", "AR", "FIL"
        }
        cls._valid_symbols_cache = valid_set

        try:
            r = requests.get("https://api.binance.com/api/v3/exchangeInfo?permissions=SPOT", timeout=5)
            if r.status_code == 200:
                data = r.json()
                for s in data.get("symbols", []):
                    if s.get("status") == "TRADING" and s.get("quoteAsset") in ("USDT", "FDUSD", "USDC"):
                        valid_set.add(s.get("baseAsset", "").upper())
                logger.info(f"成功加载币安 {len(valid_set)} 个有效交易标的（含全部山寨币与 Meme 币）。")
            else:
                logger.warning(f"币安 exchangeInfo 返回 HTTP {r.status_code}，使用内置基础标的池。")
        except Exception as e:
            logger.warning(f"获取币安交易标的列表异常 ({e})，使用内置基础标的池。")

        return cls._valid_symbols_cache

    @classmethod
    def filter_valid_tokens(cls, tokens: List[str]) -> List[str]:
        valid_set = cls.get_valid_symbols()
        filtered = [t for t in tokens if t.upper() in valid_set]
        return filtered if filtered else ["BTC"]


# ---------------------------------------------------------------------------
# 模块三：本地去重缓存管理 (CacheManager)
# ---------------------------------------------------------------------------
class CacheManager:
    """管理已发送历史，保障去重持久化"""

    def __init__(self, cache_path: str = CACHE_FILE):
        self.cache_path = cache_path
        self.cached_items: List[Dict[str, Any]] = self._load_cache()
        self.cached_ids = {item["id"] for item in self.cached_items if isinstance(item, dict) and "id" in item}

    def _load_cache(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.cache_path):
            logger.info(f"缓存文件不存在，将初始化: {self.cache_path}")
            return []
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [x for x in data if isinstance(x, dict)]
                elif isinstance(data, dict) and "sent_ids" in data:
                    return data["sent_ids"]
                return []
        except Exception as e:
            logger.warning(f"读取缓存文件异常 ({e})，使用空缓存。")
            return []

    def is_cached(self, news_id: str) -> bool:
        return news_id in self.cached_ids

    def record_sent(self, news_id: str, title: str, source: str):
        record = {
            "id": news_id,
            "title": title,
            "source": source,
            "sent_at": datetime.utcnow().isoformat() + "Z",
        }
        self.cached_items.append(record)
        self.cached_ids.add(news_id)

        if len(self.cached_items) > MAX_CACHE_SIZE:
            self.cached_items = self.cached_items[-MAX_CACHE_SIZE:]

        self._save_cache()

    def _save_cache(self):
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.cached_items, f, ensure_ascii=False, indent=2)
            logger.info(f"缓存已持久化，当前条数: {len(self.cached_items)}")
        except Exception as e:
            logger.error(f"保存缓存失败: {e}")


# ---------------------------------------------------------------------------
# 模块四：多源热点抓取、清洗与价值打分 (NewsFetcher & Scorer)
# ---------------------------------------------------------------------------
class NewsFetcher:
    """多源资讯抓取与重磅热点打分排序"""

    @staticmethod
    def clean_html(raw_html: str) -> str:
        if not raw_html:
            return ""
        clean_text = re.sub(r"<(script|style).*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
        clean_text = re.sub(r"<[^>]+>", " ", clean_text)
        clean_text = clean_text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
        return re.sub(r"\s+", " ", clean_text).strip()

    @staticmethod
    def generate_news_id(entry: Dict[str, Any], feed_name: str) -> str:
        raw_id = entry.get("id") or entry.get("link") or entry.get("title", "")
        clean_title = entry.get("title", "").strip().lower()
        seed = f"{feed_name}::{clean_title}::{raw_id}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def calculate_impact_score(title: str, summary: str) -> int:
        """根据市场冲击力与山寨/Meme热点关键词计算分值"""
        combined = (title + " " + summary).lower()
        score = 0
        for kw, weight in IMPACT_KEYWORDS.items():
            if kw in combined:
                score += weight
        return score

    def fetch_candidates(self, cache_mgr: CacheManager, limit_per_feed: int = 5) -> List[Dict[str, Any]]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
        }
        candidates = []

        for feed_cfg in RSS_FEEDS:
            name = feed_cfg["name"]
            url = feed_cfg["url"]
            lang = feed_cfg.get("lang", "en")

            logger.info(f"正在拉取数据源: [{name}]")
            try:
                resp = requests.get(url, headers=headers, timeout=12)
                if resp.status_code != 200:
                    logger.warning(f"数据源 [{name}] 响应异常: HTTP {resp.status_code}")
                    continue

                feed = feedparser.parse(resp.content)
                if not feed.entries:
                    continue

                logger.info(f"数据源 [{name}] 抓取到 {len(feed.entries)} 条新闻。")

                for entry in feed.entries[:limit_per_feed]:
                    title = entry.get("title", "").strip()
                    if not title:
                        continue

                    news_id = self.generate_news_id(entry, name)
                    if cache_mgr.is_cached(news_id):
                        continue

                    summary = ""
                    if "summary" in entry:
                        summary = entry.summary
                    elif "content" in entry and entry.content:
                        summary = entry.content[0].value
                    elif "description" in entry:
                        summary = entry.description

                    clean_summary = self.clean_html(summary)
                    link = entry.get("link", "")
                    published = entry.get("published", "") or entry.get("updated", "")
                    impact_score = self.calculate_impact_score(title, clean_summary)

                    candidates.append({
                        "id": news_id,
                        "title": title,
                        "summary": clean_summary[:1000],
                        "link": link,
                        "source": name,
                        "lang": lang,
                        "published": published,
                        "impact_score": impact_score,
                    })
            except Exception as e:
                logger.warning(f"拉取数据源 [{name}] 出错: {e}")

        # 按照市场影响力分值降序排列（山寨暴涨、Meme爆款与重大热点优先）
        candidates.sort(key=lambda x: x["impact_score"], reverse=True)
        logger.info(f"扫描完毕，共筛选出 {len(candidates)} 条未处理热点（已按全币种热度加权排序）。")
        return candidates


# ---------------------------------------------------------------------------
# 模块五：资深交易员全币种原创风格多模型 AI 引擎 (MultiLLMEngine)
# ---------------------------------------------------------------------------
class LLMProviderConfig:
    """单个 LLM 模型提供商配置"""

    def __init__(self, name: str, base_url: str, api_key: str, model: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def __repr__(self):
        masked_key = (self.api_key[:6] + "..." + self.api_key[-4:]) if len(self.api_key) > 10 else "***"
        return f"<Provider: {self.name} | Model: {self.model} | BaseURL: {self.base_url} | Key: {masked_key}>"


class MultiLLMEngine:
    """
    智能多模型池提炼引擎：
    - 支持配置多个提供商（OpenRouter, B.ai, DeepSeek, xkiro, aihubmix, inferera 等）
    - 遇到 Rate Limit / 429 / 欠费 / 超时时，自动平滑 failover 至下一个提供商
    """

    SYSTEM_PROMPT = """你是一名在币圈实盘交易多年的野生操盘手、币安广场顶级原生创作者。
你日常混迹于各大加密社区与微信群，说话直接、犀利、毒舌、极具网感，深谙韭菜心理与庄家操盘套路。

【写作宗旨：100% 模拟真人野生交易员动态，彻底剔除所有 AI 腔调】：
1. 🚫 【绝对禁用的 AI 假大空与套话】：
   - 严禁出现小标题（如“【快讯】”、“【事件要点】”、“【深度分析】”、“【总结】”）。
   - 严禁使用破折号“——”与冒号解释长句，多用口语化短句。
   - 严禁使用 AI 常见烂梗句式：“拉到聚光灯下”、“老韭菜都知道这意味着什么”、“另外一个细节值得注意”、“总而言之”、“毋庸置疑”、“这到底是A还是B让我们拭目以待”、“综上所述”。
   - 严禁假装客观当骑墙派（不要“一方面...另一方面...”）。真人都有鲜明态度：要么提示诱多风险，要么看好突破，要么吐槽韭菜追高。
   - 严禁在非代币名词前加美元符号（绝对不要写 $ETF、$SEC、$AI、$CEO、$NFT、$USD、$CEX）。只在真实代币前加 $（如 $BTC, $ETH, $SOL, $XRP, $DOGE, $PEPE）。

2. 📏 【字数与排版规范（移动端极简短句流）】：
   - 全文严格控制在 160 ~ 240 字以内！手机屏幕一屏就能快速读完，绝不长篇大论。
   - 分成 3 到 4 个短段落，段与段之间空一行。每段只有 1~2 句话，短小精炼，节奏明快。

3. 💬 【真人口吻与结构】：
   - **第 1 段（开门见山）**：一句话爆出今天最刺激的行情或消息，带出核心标的（如 $XRP 或 $DOGE）。
   - **第 2 段（拆解博弈真相）**：讲大白话、讲庄家人性。结合盘面异动或情绪，戳破利好背后的资金意图（是借利好出货？还是深度洗盘完毕？）。
   - **第 3 段（实在的实操建议）**：说一句不装逼的真话（分批挂单别追高、把止损带好别抗单、现货拿住别被插针洗下车）。
   - **第 4 段（极简站队互动）**：用“看多冲前高的扣 1，觉得是诱多出货的扣 2”等极简站队提问，刺激评论区开喷互动。
   - **文末标签**：只带 3 个标签：#Write2Earn #BinanceSquare #核心代币名。

【真人实战范文对照（请严格模仿这种口吻、长度与节奏）】：
---
范文一：
这波 XRP 动静属实不小，4.7 亿 ETF 增量资金直接把盘面砸活了。

很多人在喊冲 2 块，我说句得罪人的大实话：全网贪婪指数都 65 了，现在无脑追高，纯粹是去给老外机构当出货流动性。

主力这波明显是在借消息拉高换手，真想参与的别着急上头，等一波日线级别的放量回踩确认支撑再考虑。现货拿稳别慌，合约把杠杆降到最低，千万别被洗盘插针带走。

兄弟们，你觉得这次 $XRP 是真突破还是诱多出货？
看好破前高的打 1
觉得要暴跌洗盘的打 2

#Write2Earn #BinanceSquare #XRP
---
范文二：
今天 Meme 板块集体异动，DOGE、PEPE、SHIB 都在蠢蠢欲动。

炒 Meme 这么多年，亏钱的永远是同一批人：行情初期不敢上，涨到山顶了抵押房子冲进去，最后一套就是大半年。

现在盘面明显是情绪后半场的补涨，追高性价比极低。手痒想玩的，最多拿 5% 仓位去以小博大，翻倍立马把本金抽出来，用利润去博上限，心态才不会崩。

手里的代币都浮盈了吗？
这波你重仓了哪个？评论区报个代码，我挑两个盘面帮大家把把脉。

#Write2Earn #BinanceSquare #PEPE
---"""

    def __init__(self):
        self.providers: List[LLMProviderConfig] = self._build_provider_chain()

    def _build_provider_chain(self) -> List[LLMProviderConfig]:
        """构建提供商备份链"""
        chain: List[LLMProviderConfig] = []

        # 1. 优先读取高级 JSON 配置: LLM_PROVIDERS_CONFIG
        providers_json = os.getenv("LLM_PROVIDERS_CONFIG", "").strip()
        if providers_json:
            try:
                items = json.loads(providers_json)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and item.get("api_key"):
                            cfg = LLMProviderConfig(
                                name=item.get("name", "Custom-JSON"),
                                base_url=item.get("base_url", "https://api.deepseek.com"),
                                api_key=item.get("api_key", ""),
                                model=item.get("model", "deepseek-chat"),
                            )
                            chain.append(cfg)
                    if chain:
                        logger.info(f"成功从 LLM_PROVIDERS_CONFIG 加载了 {len(chain)} 个模型提供商。")
                        return chain
            except Exception as e:
                logger.warning(f"解析 LLM_PROVIDERS_CONFIG 失败 ({e})，将回退至标准环境变量。")

        # 2. 读取标准单一环境变量
        single_api_key = os.getenv("LLM_API_KEY", "").strip()
        single_base_url = os.getenv("LLM_BASE_URL", "").strip() or "https://api.deepseek.com"
        single_model = os.getenv("LLM_MODEL", "").strip() or "deepseek-chat"

        if single_api_key:
            chain.append(LLMProviderConfig(
                name="Primary-LLM",
                base_url=single_base_url,
                api_key=single_api_key,
                model=single_model,
            ))

        # 3. 检查是否有单独配置的常见平台 Key
        extra_keys = {
            "openrouter": (
                os.getenv("OPENROUTER_API_KEY", "").strip(),
                "https://openrouter.ai/api/v1",
                os.getenv("OPENROUTER_MODEL", "").strip() or "minimax/minimax-m3:free",
            ),
            "b.ai": (
                os.getenv("BAI_API_KEY", "").strip(),
                "https://api.b.ai/v1",
                os.getenv("BAI_MODEL", "").strip() or "glm-5.3-flash",
            ),
            "xkiro": (
                os.getenv("XKIRO_API_KEY", "").strip(),
                "https://api.xkiro.com/v1",
                os.getenv("XKIRO_MODEL", "").strip() or "qwen/qwen3.8-max:free",
            ),
            "aihubmix": (
                os.getenv("AIHUBMIX_API_KEY", "").strip(),
                "https://aihubmix.com/v1",
                os.getenv("AIHUBMIX_MODEL", "").strip() or "coding-glm-5.3-flash-free",
            ),
            "inferera": (
                os.getenv("INFERERA_API_KEY", "").strip(),
                "https://api.inferera.com/v1",
                os.getenv("INFERERA_MODEL", "").strip() or "coding-kimi-k3-free",
            ),
            "tokenrouter": (
                os.getenv("TOKENROUTER_API_KEY", "").strip(),
                "https://api.tokenrouter.com/v1",
                os.getenv("TOKENROUTER_MODEL", "").strip() or "qwen/qwen3.8-max-free",
            ),
            "siliconflow": (
                os.getenv("SILICONFLOW_API_KEY", "").strip(),
                "https://api.siliconflow.cn/v1",
                os.getenv("SILICONFLOW_MODEL", "").strip() or "deepseek-ai/DeepSeek-V3",
            ),
        }

        for name, (k, url, m) in extra_keys.items():
            if k and not any(p.api_key == k for p in chain):
                chain.append(LLMProviderConfig(name=f"Preset-{name}", base_url=url, api_key=k, model=m))

        if not chain:
            logger.warning("未检测到有效的 LLM API Key，AI 提炼模块将无法正常发起在线请求！")

        return chain

    def summarize(
        self,
        news_item: Dict[str, Any],
        campaign_intel: Optional[Dict[str, Any]] = None,
        market_context: str = "",
    ) -> Optional[str]:
        """
        结合最新币安官方活动情报与实时行情进行高收益转化提炼
        """
        if not self.providers:
            logger.error("没有任何可用的 LLM 提供商配置！")
            return None

        # 组织活动背景提示（仅作为潜意识背景，避免生搬硬套非相关代币）
        intel_section = ""
        if campaign_intel and campaign_intel.get("strategy_guidance"):
            intel_section = f"【官方活动风向参考】：{campaign_intel.get('strategy_guidance')}（若与本条新闻无关则切勿生硬提及）。\n"

        market_section = ""
        if market_context:
            market_section = f"【实时盘面情绪参考】：{market_context}\n"

        user_prompt = f"""请将以下新闻提炼为一条极具穿透力、短小精悍的真人交易员动态：

【新闻标题】：{news_item.get('title', '')}
【新闻摘要】：{news_item.get('summary', '')}
{market_section}{intel_section}
【核心要求】：
1. 彻底去 AI 味！模仿真人老韭菜/交易员在社区发帖的极简口吻。
2. 篇幅严格控制在 160~240 字之间，分 3~4 个短段落，短句为主，每段 1~2 句话。
3. 只能给 1~2 个真实代币加 $（如 $XRP 或 $DOGE，严禁在 ETF/SEC/AI/CEO/FED 等非代币词前加 $）。
4. 结尾设计一句极简的站队提问（如“看多的扣1，看空的扣2”），最后附带 3 个标签：#Write2Earn #BinanceSquare #核心代币。
直接输出正文，不要任何开场白或多余解释："""

        # 遍历提供商链进行容灾尝试
        for index, provider in enumerate(self.providers):
            logger.info(f"[{index + 1}/{len(self.providers)}] 正在尝试使用提供商 [{provider.name}] (模型: {provider.model})...")
            try:
                client = OpenAI(
                    api_key=provider.api_key,
                    base_url=provider.base_url,
                    timeout=25.0,
                )

                response = client.chat.completions.create(
                    model=provider.model,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.75,
                    max_tokens=600,
                )

                content = response.choices[0].message.content.strip()

                # 1. 提取代币并清洗非代币伪标的
                raw_tokens = re.findall(r"\$([A-Z0-9]{2,10})", content)
                valid_tokens = SymbolValidator.filter_valid_tokens(raw_tokens)
                if not valid_tokens:
                    valid_tokens = ["BTC"]

                # 2. 标签保底处理（仅保留干净的 3 个标签，绝不附带机械化广告标语）
                if not re.search(r"#Write2Earn", content, re.IGNORECASE):
                    primary_token = valid_tokens[0]
                    content += f"\n\n#Write2Earn #BinanceSquare #{primary_token}"

                logger.info(f"🎉 模型 [{provider.name}] 生成成功！")
                return content

            except Exception as e:
                err_msg = str(e)
                logger.warning(f"提供商 [{provider.name}] 请求失败: {err_msg}")
                if index < len(self.providers) - 1:
                    logger.info(f"正在自动切换至下一个备用提供商...")
                    time.sleep(1)

        logger.error("所有已配置的 LLM 提供商均调用失败！")
        return None


# ---------------------------------------------------------------------------
# 模块六：币安官方创作者活动智能扫描与理解 (CampaignScanner)
# ---------------------------------------------------------------------------
class CampaignScanner:
    """自动扫描币安官方最新活动、竞赛与上线公告，并交由 AI 理解提炼活动策略"""

    OFFICIAL_CATALOGS = [
        {"id": 93, "name": "最新活动与交易竞赛"},
        {"id": 48, "name": "合约与衍生品上线活动"},
        {"id": 49, "name": "新币挖矿与理财活动"},
    ]

    @staticmethod
    def fetch_raw_campaigns() -> List[str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        campaign_titles = []
        for catalog in CampaignScanner.OFFICIAL_CATALOGS:
            cid = catalog["id"]
            url = f"https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query?catalogId={cid}&pageNo=1&pageSize=6"
            try:
                resp = requests.get(url, headers=headers, timeout=8)
                if resp.status_code == 200:
                    data = resp.json()
                    articles = data.get("data", {}).get("articles", [])
                    for a in articles:
                        title = a.get("title", "").strip()
                        if title and title not in campaign_titles:
                            campaign_titles.append(title)
            except Exception as e:
                logger.warning(f"拉取币安官方活动分类 [{catalog['name']}] 失败: {e}")
        return campaign_titles

    @staticmethod
    def analyze_with_ai(llm_engine: MultiLLMEngine, raw_titles: List[str]) -> Dict[str, Any]:
        """让 AI 深度理解币安官方活动列表，提炼结构化活动情报"""
        if not raw_titles:
            return {
                "active_tags": ["#Write2Earn", "#BinanceSquare", "#热点解析"],
                "incentivized_tokens": ["$BTC", "$ETH", "$BNB", "$SOL"],
                "strategy_guidance": "优先关联主流现货与USDT永续合约，吸引读者点击交易组件以赚取返佣。",
                "last_updated": datetime.utcnow().isoformat() + "Z",
            }

        titles_text = "\n".join([f"- {t}" for t in raw_titles[:15]])
        prompt = f"""你是一名精通币安创作者激励与生态活动的策略总监。
以下是币安官方最新正在进行的活动、竞赛与上线公告列表：

{titles_text}

请深度分析这些活动，输出 JSON 格式的创作者发帖情报：
1. "active_tags": 3~5 个当前最有流量、最匹配官方活动的标签（必须包含 #Write2Earn #BinanceSquare，以及 1~3 个当期活动词如 #Futures #TradingTournament #Megadrop 等）；
2. "incentivized_tokens": 4~8 个当期有活动奖励、交易竞赛或新上线的焦点代币（大写加$，如 $BNB, $SOL, $BTC 等）；
3. "strategy_guidance": 1~2 句话指导发帖机器人如何将日常快讯与当前币安官方活动/合约/产品结合以最大化获取曝光和 Write to Earn 交易返佣。

请严格仅返回纯 JSON 字符串（不要输出 markdown 代码块）：
{{
  "active_tags": ["#Write2Earn", "#BinanceSquare", "#热点解析"],
  "incentivized_tokens": ["$BNB", "$BTC", "$SOL"],
  "strategy_guidance": "结合当期新合约与交易竞赛，引导读者参与交易获取返佣。"
}}"""

        try:
            logger.info("正在使用 AI 深度分析币安官方当期活动情报...")
            for provider in llm_engine.providers:
                try:
                    client = OpenAI(api_key=provider.api_key, base_url=provider.base_url, timeout=25.0)
                    resp = client.chat.completions.create(
                        model=provider.model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.3,
                        max_tokens=400,
                    )
                    raw_res = resp.choices[0].message.content.strip()
                    clean_res = re.sub(r"^```json\s*", "", raw_res, flags=re.IGNORECASE)
                    clean_res = re.sub(r"^```\s*", "", clean_res)
                    clean_res = re.sub(r"\s*```$", "", clean_res).strip()
                    data = json.loads(clean_res)
                    if isinstance(data, dict):
                        data["last_updated"] = datetime.utcnow().isoformat() + "Z"
                        logger.info(f"🎉 币安活动情报分析完成: {data.get('strategy_guidance')}")
                        return data
                except Exception as e:
                    logger.warning(f"使用提供商 [{provider.name}] 分析活动失败: {e}")
        except Exception as e:
            logger.warning(f"AI 理解活动异常: {e}")

        return {
            "active_tags": ["#Write2Earn", "#BinanceSquare", "#热点解析"],
            "incentivized_tokens": ["$BTC", "$ETH", "$BNB", "$SOL"],
            "strategy_guidance": "优先关联主流现货与USDT永续合约，吸引读者点击交易组件以赚取返佣。",
            "last_updated": datetime.utcnow().isoformat() + "Z",
        }

    @staticmethod
    def get_campaign_intel(llm_engine: MultiLLMEngine) -> Dict[str, Any]:
        """获取或更新活动情报缓存"""
        if os.path.exists(CAMPAIGN_INTEL_FILE):
            try:
                with open(CAMPAIGN_INTEL_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                    last_updated = cached.get("last_updated", "")
                    if last_updated:
                        updated_time = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                        now = datetime.now(updated_time.tzinfo)
                        if (now - updated_time).total_seconds() < INTEL_EXPIRE_HOURS * 3600:
                            logger.info(f"使用现存有效的币安活动情报 (更新于 {last_updated})")
                            return cached
            except Exception as e:
                logger.warning(f"读取 campaign_intel.json 异常: {e}")

        logger.info("活动情报已过期或不存在，正在重新扫描币安官方活动...")
        raw_titles = CampaignScanner.fetch_raw_campaigns()
        intel = CampaignScanner.analyze_with_ai(llm_engine, raw_titles)
        try:
            with open(CAMPAIGN_INTEL_FILE, "w", encoding="utf-8") as f:
                json.dump(intel, f, ensure_ascii=False, indent=2)
            logger.info("最新币安活动情报已写入本地文件: campaign_intel.json")
        except Exception as e:
            logger.error(f"保存 campaign_intel.json 失败: {e}")

        return intel


# ---------------------------------------------------------------------------
# 模块七：币安广场 OpenAPI 客户端 (SquarePublisher)
# ---------------------------------------------------------------------------
class SquarePublisher:
    """币安广场发布组件"""

    def __init__(self, api_key: str):
        self.api_key = api_key

    @staticmethod
    def _sanitize_content(content: str) -> str:
        """
        全自动化内容精细清洗与去 AI 味保障：
        1. 清洗非代币误加的 $（如 $ETF -> ETF, $SEC -> SEC, $AI -> AI, $CEO -> CEO 等）
        2. 剔除生硬的破折号“——”
        3. 币安限制单篇 Hashtag 数量（上限为 3~4 个，超过将报错 220094），严格保留最多 3 个核心标签
        """
        # 1. 清洗非代币伪标的
        non_token_words = [
            "ETF", "SEC", "FED", "CEO", "NFT", "AI", "USD", "USDT", "USDC",
            "CEX", "DEX", "API", "CAGR", "APR", "APY", "ATH", "BAPI", "NEWS", "MEME"
        ]
        for word in non_token_words:
            content = re.sub(rf"\${word}\b", word, content, flags=re.IGNORECASE)

        # 2. 移除生硬破折号
        content = content.replace("——", "，")

        # 3. 严格限制 Hashtag 数量（最多 3 个）
        hashtags = re.findall(r"#[^\s#]+", content)
        if len(hashtags) > 3:
            for tag in hashtags[3:]:
                content = content.replace(tag, tag.lstrip("#"), 1)

        return content.strip()

    def publish(self, content: str) -> bool:
        if not self.api_key:
            logger.error("未配置 SQUARE_API_KEY，无法发布到币安广场！")
            return False

        # 严格清洗与合规处理
        content = self._sanitize_content(content)

        headers = {
            "X-Square-OpenAPI-Key": self.api_key,
            "Content-Type": "application/json",
            "clienttype": "binanceSkill",
            "User-Agent": "BinanceSquareAutoPosterPro/3.0",
        }

        payload = {
            "bodyTextOnly": content,
        }

        try:
            logger.info("正在向币安广场 OpenAPI 提交发帖请求...")
            response = requests.post(
                BINANCE_SQUARE_API_URL,
                headers=headers,
                json=payload,
                timeout=15,
            )

            status_code = response.status_code
            resp_text = response.text
            logger.info(f"币安广场 API 响应状态码: {status_code}")

            if status_code != 200:
                logger.error(f"发帖失败！HTTP {status_code}, 响应: {resp_text}")
                return False

            try:
                resp_json = response.json()
            except Exception:
                logger.error(f"解析币安响应 JSON 失败: {resp_text}")
                return False

            code = resp_json.get("code")
            success = resp_json.get("success", False)

            if code == "000000" or success is True or code == 0:
                data = resp_json.get("data") or {}
                content_id = data.get("contentId") or data.get("id") or "成功"
                logger.info(f"🎉 成功发布到币安广场！Content ID: {content_id}")
                return True
            else:
                logger.error(f"币安广场返回业务错误: {json.dumps(resp_json, ensure_ascii=False)}")
                return False

        except Exception as e:
            logger.error(f"发帖网络请求异常: {e}")
            return False


# ---------------------------------------------------------------------------
# 模块八：多渠道通知与异常报警系统 (Notifier)
# ---------------------------------------------------------------------------
class Notifier:
    """
    支持多渠道状态与错误报警通知：
    1. 微信通知：Server酱 (SERVERCHAN_KEY) 或 PushPlus推送加 (PUSHPLUS_TOKEN)
    2. 苹果 iOS 推送：Bark (BARK_KEY)
    3. Telegram Bot (TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID)
    4. 团队群机器人：通用 Webhook (钉钉 / 飞书 / 企微 / Discord)
    """

    @staticmethod
    def send_notification(title: str, message: str, is_error: bool = False):
        prefix = "🚨 【异常报警】" if is_error else "📢 【发帖成功】"
        full_title = f"{prefix} {title}"

        # 1. 微信推送：Server酱 (Turbo版)
        serverchan_key = os.getenv("SERVERCHAN_KEY", "").strip()
        if serverchan_key:
            try:
                url = f"https://sctapi.ftqq.com/{serverchan_key}.send"
                requests.post(url, data={"title": full_title, "desp": message}, timeout=8)
                logger.info("已发送 Server酱 微信通知。")
            except Exception as e:
                logger.warning(f"发送 Server酱 失败: {e}")

        # 2. 微信推送：PushPlus (推送加)
        pushplus_token = os.getenv("PUSHPLUS_TOKEN", "").strip()
        if pushplus_token:
            try:
                url = "http://www.pushplus.plus/send"
                requests.post(url, json={"token": pushplus_token, "title": full_title, "content": message, "template": "markdown"}, timeout=8)
                logger.info("已发送 PushPlus 微信通知。")
            except Exception as e:
                logger.warning(f"发送 PushPlus 失败: {e}")

        # 3. iOS 推送：Bark
        bark_key = os.getenv("BARK_KEY", "").strip()
        if bark_key:
            try:
                bark_url = f"https://api.day.app/{bark_key}/{full_title}/{message}"
                requests.get(bark_url, timeout=8)
                logger.info("已发送 Bark iOS 推送。")
            except Exception as e:
                logger.warning(f"发送 Bark 推送失败: {e}")

        # 4. Telegram 通知
        tg_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if tg_bot_token and tg_chat_id:
            try:
                tg_url = f"https://api.telegram.org/bot{tg_bot_token}/sendMessage"
                text = f"*{full_title}*\n\n{message}"
                requests.post(tg_url, json={"chat_id": tg_chat_id, "text": text, "parse_mode": "Markdown"}, timeout=8)
                logger.info("已发送 Telegram 状态通知。")
            except Exception as e:
                logger.warning(f"发送 Telegram 通知失败: {e}")

        # 5. 通用 Webhook (钉钉 / 飞书 / 企微 / Discord)
        webhook_url = os.getenv("WEBHOOK_URL", "").strip()
        if webhook_url:
            try:
                payload = {"msgtype": "text", "text": {"content": f"{full_title}\n\n{message}"}, "content": f"**{full_title}**\n\n{message}"}
                requests.post(webhook_url, json=payload, timeout=8)
                logger.info("已发送 Webhook 状态通知。")
            except Exception as e:
                logger.warning(f"发送 Webhook 通知失败: {e}")


# ---------------------------------------------------------------------------
# 主流程入口
# ---------------------------------------------------------------------------
def main():
    try:
        _run_main()
    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()
        logger.critical(f"💥 程序发生未捕获的致命异常: {e}\n{err_detail}")
        Notifier.send_notification("币安发帖机器人运行崩溃", f"错误原因: {str(e)}\n\n堆栈详情:\n{err_detail[:600]}", is_error=True)
        sys.exit(1)


def _run_main():
    square_api_key = os.getenv("SQUARE_API_KEY", "").strip()
    max_posts_raw = os.getenv("MAX_POSTS_PER_RUN", "").strip() or "1"
    max_posts = int(max_posts_raw) if max_posts_raw.isdigit() else 1
    dry_run = os.getenv("DRY_RUN", "false").strip().lower() in ("true", "1", "yes")

    logger.info("==================================================")
    logger.info("🚀 币安广场全币种·山寨爆款与活动智能变现系统 (Ultimate 版) 启动")
    logger.info(f"   运行时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info(f"   运行模式: {'【DRY_RUN 试运行 (不真实发帖)】' if dry_run else '【正式发布模式】'}")
    logger.info(f"   单次最大发帖数: {max_posts}")
    logger.info("==================================================")

    # 1. 生产模式必要参数检查
    if not dry_run and not square_api_key:
        logger.error("错误: 未配置 SQUARE_API_KEY 环境变量！")
        sys.exit(1)

    # 2. 初始化核心组件
    cache_mgr = CacheManager(CACHE_FILE)
    fetcher = NewsFetcher()
    llm_engine = MultiLLMEngine()
    publisher = SquarePublisher(api_key=square_api_key)

    # 3. 获取全网恐慌贪婪指数与币安市场行情基准
    fng_index = MarketDataProvider.get_fear_and_greed()
    logger.info(f"📊 当前全网情绪指数: {fng_index}")

    # 4. 智能扫描与理解币安官方当期活动情报
    campaign_intel = CampaignScanner.get_campaign_intel(llm_engine)
    logger.info(f"💡 当期币安重点活动标签: {campaign_intel.get('active_tags')}")
    logger.info(f"🪙 当期重点扶持代币池: {campaign_intel.get('incentivized_tokens')}")

    # 5. 获取待发布热点候选（按冲击力与山寨/Meme热度打分排序）
    candidates = fetcher.fetch_candidates(cache_mgr)
    if not candidates:
        logger.info("✅ 未检测到新的未发布热点，安全退出。")
        sys.exit(0)

    # 6. 执行发帖循环
    posted_count = 0
    valid_symbols = SymbolValidator.get_valid_symbols() or set()
    IGNORE_WORDS = {"THE", "AND", "FOR", "WITH", "NEW", "TOP", "USD", "EUR", "SEC", "ETF", "FED", "CEO", "ALL", "NOW", "KEY", "NFT", "DAO", "DEX", "CEX", "API", "POS", "POW", "ATH", "APR", "APY"}

    for item in candidates:
        if posted_count >= max_posts:
            logger.info(f"已达到本次最大发帖数 ({max_posts})，退出循环。")
            break

        news_id = item["id"]
        title = item["title"]
        source = item["source"]
        score = item.get("impact_score", 0)

        logger.info(f"--------------------------------------------------")
        logger.info(f"正在处理第 {posted_count + 1} 条热点 (热度分: {score}): [{source}] {title}")

        # 动态全币种识别：提取标题与摘要中的所有潜在币种（主流 + 山寨 + Meme）
        combined_text = title + " " + item["summary"]
        raw_words = re.findall(r"\b[A-Za-z0-9]{2,10}\b", combined_text)
        detected_tokens = []
        for w in raw_words:
            upper_w = w.upper()
            if upper_w not in IGNORE_WORDS and upper_w in valid_symbols and upper_w not in detected_tokens:
                detected_tokens.append(upper_w)

        if not detected_tokens:
            detected_tokens = ["BTC"]

        live_market_data = MarketDataProvider.get_token_market_data(detected_tokens[:3])
        market_context_str = f"全网情绪指数: {fng_index}\n涉及标的实时盘面: {live_market_data if live_market_data else '链上/全市场热点'}"

        # AI 结合活动情报与实时盘面进行高质量提炼
        post_content = llm_engine.summarize(item, campaign_intel, market_context=market_context_str)
        if not post_content:
            logger.warning(f"AI 生成失败，跳过: {title}")
            continue

        logger.info("生成内容预览:\n" + post_content)

        # 发布或模拟
        if dry_run:
            logger.info(f"【DRY_RUN 模式】模拟发布成功，记录缓存: {news_id}")
            cache_mgr.record_sent(news_id, title, source)
            posted_count += 1
        else:
            success = publisher.publish(post_content)
            if success:
                cache_mgr.record_sent(news_id, title, source)
                posted_count += 1
                Notifier.send_notification("币安广场自动发帖成功", f"新闻: {title}\n来源: {source}\n\n{post_content[:200]}...")
            else:
                logger.error(f"发帖失败，本次暂不记录缓存以供下次重试: {title}")
                Notifier.send_notification("币安发帖失败", f"新闻: {title}\n发布接口返回异常，已跳过并将在下次重试。", is_error=True)

        # 模拟自然人工操作延迟
        if posted_count < max_posts:
            delay = random.randint(3, 8)
            time.sleep(delay)

    logger.info("==================================================")
    logger.info(f"🎯 任务完成！本次成功处理/发布: {posted_count} 篇")
    logger.info("==================================================")


if __name__ == "__main__":
    main()
