#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
==============================================================================
币安广场（Binance Square）加密热点与创作者活动智能变现系统 (Ultimate Edition)
==============================================================================
核心升级与全网前沿技术融合：
1. 🎯 币安官方活动与激励感知 (AI Campaign Scanner & Analyzer)：
   - 自动扫描币安官方最新竞赛（Catalog 93）、合约上线（Catalog 48）、新币/理财（Catalog 49）。
   - AI 提取当期重点扶持币种（$BNB、$SOL、竞赛币等）与官方流量标签（#Write2Earn 等），使每篇发帖紧扣官方奖励。
2. 📊 实时盘面与全网情绪注入 (Live Market & Sentiment Context)：
   - 自动抓取全网恐慌与贪婪指数（Fear & Greed Index）。
   - 自动拉取币安官方实时 24H 盘面行情（价格、涨跌幅、成交量），为 AI 分析提供真实数据支撑，大幅提升专业度与转化率。
3. 🔥 重磅热点价值打分器 (Breaking News Impact Scorer)：
   - 引入市场冲击力关键词加权算法（ETF、SEC、美联储、降息、上线、Launchpool、爆仓、突破等），优先筛选最具吸睛力的新闻。
4. ✅ 币安交易标的防幻觉校验器 (Symbol & Widget Validator)：
   - 自动校验提取的 $TOKEN 是否为币安真实交易对，确保 100% 触发 Write to Earn 交易组件与返佣。
5. 🔄 多 LLM 模型池与自动故障转移 (Auto-Failover)：
   - 支持 OpenRouter (minimax-m3:free), B.ai (glm-5.3-flash), xkiro, aihubmix, inferera, TokenRouter, DeepSeek, 硅基流动等。
6. 0 服务器成本：基于 GitHub Actions 定时触发，通过 Git 状态回写持久化。
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
# 免费与公开 RSS 数据源列表
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    {
        "name": "BlockTempo (动区动趋中文)",
        "url": "https://www.blocktempo.com/feed/",
        "lang": "zh",
    },
    {
        "name": "Cointelegraph",
        "url": "https://cointelegraph.com/rss",
        "lang": "en",
    },
    {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "lang": "en",
    },
    {
        "name": "Decrypt",
        "url": "https://decrypt.co/feed",
        "lang": "en",
    },
    {
        "name": "Bitcoin Magazine",
        "url": "https://bitcoinmagazine.com/.rss/full/",
        "lang": "en",
    },
]

# 重磅热点打分关键词加权字典
IMPACT_KEYWORDS = {
    "etf": 12,
    "sec": 10,
    "fed": 10,
    "美联储": 10,
    "降息": 10,
    "加息": 8,
    "launchpool": 12,
    "megadrop": 12,
    "listing": 10,
    "上线": 10,
    "突破": 8,
    "新高": 9,
    "ath": 9,
    "暴涨": 7,
    "暴跌": 7,
    "爆仓": 9,
    "清算": 9,
    "options": 10,
    "选择权": 10,
    "期权": 9,
    "bstocks": 10,
    "airdrop": 8,
    "空投": 8,
    "融资": 7,
    "合作": 6,
    "主网": 7,
    "升级": 6,
    "黑客": 8,
    "whale": 7,
    "巨鲸": 7,
}


# ---------------------------------------------------------------------------
# 模块一：实时行情与全网情绪提供器 (MarketDataProvider)
# ---------------------------------------------------------------------------
class MarketDataProvider:
    """获取加密货币全网宏观情绪与币安实时 24H 盘面价格数据"""

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
        """批量获取币安实时价格与 24H 涨跌幅数据"""
        results = []
        for symbol in symbols[:3]:  # 取前 3 个标的
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
                    # 格式化价格格式
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

        valid_set = {"BTC", "ETH", "BNB", "SOL", "DOGE", "XRP", "PEPE", "SUI", "NEAR", "APT", "AVAX", "LINK", "TRX", "ADA", "SHIB"}
        try:
            r = requests.get("https://api.binance.com/api/v3/exchangeInfo?permissions=SPOT", timeout=5)
            if r.status_code == 200:
                data = r.json()
                for s in data.get("symbols", []):
                    if s.get("status") == "TRADING" and s.get("quoteAsset") in ("USDT", "FDUSD", "USDC"):
                        valid_set.add(s.get("baseAsset", "").upper())
                cls._valid_symbols_cache = valid_set
                logger.info(f"成功加载币安 {len(valid_set)} 个有效交易标的。")
        except Exception as e:
            logger.warning(f"获取币安交易标的列表失败 ({e})，使用基础标的池。")
            cls._valid_symbols_cache = valid_set

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
        """根据市场冲击力关键词计算新闻热度分值"""
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

        # 按照市场影响力分值降序排列（重大热点优先发布）
        candidates.sort(key=lambda x: x["impact_score"], reverse=True)
        logger.info(f"扫描完毕，共筛选出 {len(candidates)} 条未处理热点（已按热度加权排序）。")
        return candidates


# ---------------------------------------------------------------------------
# 模块五：多模型故障转移 AI 引擎 (MultiLLMEngine)
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

    SYSTEM_PROMPT = """你是一名精通加密货币交易与币安广场（Binance Square）Write to Earn 收益机制的顶级内容创作者。
你的目标是创作【高阅读量、高互动率、强交易转化、符合币安创作者活动与返佣规范】的高质量快讯短帖。

【核心排版与内容规范】：
1. 📌 【核心提炼与事实】（80~150字）：
   - 用精炼有力的中文提炼事件核心要点，突出关键数据、机构动向、资金规模与事件本质。
2. 🔍 【深度解析与盘面影响】（1~2句话）：
   - 结合实时行情与全网情绪数据，从流动性、资金博弈、短线情绪或关键支撑/阻力角度给出专业点评，激发读者的交易与观察兴趣。
3. 🎯 【标的交易对与合约信息】（核心转化触发点）：
   - 提取 1~2 个最相关的币安真实交易标的，必须严格包含大写代币标签（如 $BTC 、$ETH 、$SOL 、$BNB ），并标明推荐观察的交易类型（如：`$BTC (现货 / USDT永续合约)`）。
   - 若新闻涉及链上新代币、Meme、Launchpool、Megadrop 或特定项目且包含合约地址(CA)，请清晰列出：`链上合约(CA): 0x... / Solana地址` 及主网网络（如无具体地址则注明“币安主板已上线”）。
4. 💬 【互动思考】（1个开放式问题）：
   - 提出一个引发多空激辩或后市预测的犀利问题，吸引读者在评论区留言（拉升币安广场推荐流权重）。
5. 🏷️ 【官方创作者活动与话题标签】（不可缺失）：
   - 必须带上官方返佣活动标签：#Write2Earn #BinanceSquare #热点解析 以及提取出的代币话题（如 #BTC #ETH）。

【输出格式样式规范】：
📌 【快讯】[吸睛标题/核心要点]
...（80~150字事实提炼）...

🔍 盘面解析与潜在影响：
...（结合实时行情与情绪的 1~2 句专业点评）...

🎯 标的与合约信息：
- 核心标的：$BTC (现货 / USDT永续合约)
- 链上合约/网络：（如有合约地址则列出，无则注明“币安主板已上线”）

💬 互动思考：
...（刺激评论区互动的开放性问题）...

👇 点击下方代币标签直达盘面交易，关注我获取第一手快讯与行情策略！
#Write2Earn #BinanceSquare #热点解析 #BTC

【严格注意事项】：
- 仅输出格式化后的正文，不要输出任何开场白或解释性说明。
- 严禁出现“稳赚”、“包赚”、“100%暴涨”等绝对化违规词汇，遵守平台合规要求。"""

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
        single_base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").strip()
        single_model = os.getenv("LLM_MODEL", "deepseek-chat").strip()

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
                os.getenv("OPENROUTER_MODEL", "minimax/minimax-m3:free").strip(),
            ),
            "b.ai": (
                os.getenv("BAI_API_KEY", "").strip(),
                "https://api.b.ai/v1",
                os.getenv("BAI_MODEL", "glm-5.3-flash").strip(),
            ),
            "xkiro": (
                os.getenv("XKIRO_API_KEY", "").strip(),
                "https://api.xkiro.com/v1",
                os.getenv("XKIRO_MODEL", "qwen/qwen3.8-max:free").strip(),
            ),
            "aihubmix": (
                os.getenv("AIHUBMIX_API_KEY", "").strip(),
                "https://aihubmix.com/v1",
                os.getenv("AIHUBMIX_MODEL", "coding-glm-5.3-flash-free").strip(),
            ),
            "inferera": (
                os.getenv("INFERERA_API_KEY", "").strip(),
                "https://api.inferera.com/v1",
                os.getenv("INFERERA_MODEL", "coding-kimi-k3-free").strip(),
            ),
            "tokenrouter": (
                os.getenv("TOKENROUTER_API_KEY", "").strip(),
                "https://api.tokenrouter.com/v1",
                os.getenv("TOKENROUTER_MODEL", "qwen/qwen3.8-max-free").strip(),
            ),
            "siliconflow": (
                os.getenv("SILICONFLOW_API_KEY", "").strip(),
                "https://api.siliconflow.cn/v1",
                os.getenv("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V3").strip(),
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

        # 组织活动情报提示词
        intel_section = ""
        if campaign_intel:
            active_tags = " ".join(campaign_intel.get("active_tags", ["#Write2Earn", "#BinanceSquare", "#热点解析"]))
            incentivized_tokens = " ".join(campaign_intel.get("incentivized_tokens", ["$BNB", "$BTC"]))
            strategy = campaign_intel.get("strategy_guidance", "优先关联主流现货与USDT永续合约。")
            intel_section = f"""
【币安官方当期重点活动情报与策略指导】：
- 官方当期核心活动标签：{active_tags}
- 官方当期重点奖励/交易代币池：{incentivized_tokens}
- 收益策略指导：{strategy}
"""

        market_section = ""
        if market_context:
            market_section = f"\n【实时盘面与市场情绪参考】：\n{market_context}\n"

        user_prompt = f"""请将以下加密新闻提炼为一条高质量的币安广场快讯短贴：

【新闻来源】：{news_item.get('source', '未知')}
【原始标题】：{news_item.get('title', '')}
【原始内容】：{news_item.get('summary', '')}
{market_section}{intel_section}
请结合上述币安当期活动情报、实时行情与规范生成最利于收益转化的文案："""

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
                    temperature=0.7,
                    max_tokens=700,
                )

                content = response.choices[0].message.content.strip()

                # 1. 提取并使用币安有效标的过滤
                raw_tokens = re.findall(r"\$([A-Z0-9]{2,10})", content)
                valid_tokens = SymbolValidator.filter_valid_tokens(raw_tokens)
                if not raw_tokens or not valid_tokens:
                    content += "\n\n🎯 核心标的：$BTC (现货 / USDT永续合约)"
                    valid_tokens = ["BTC"]

                # 2. 创作者活动话题与标签保底处理
                campaign_tags_env = os.getenv("CAMPAIGN_TAGS", "").strip()
                if not campaign_tags_env and campaign_intel:
                    campaign_tags_env = " ".join(campaign_intel.get("active_tags", ["#Write2Earn", "#BinanceSquare", "#热点解析"]))
                if not campaign_tags_env:
                    campaign_tags_env = "#Write2Earn #BinanceSquare #热点解析"

                if not re.search(r"#Write2Earn", content, re.IGNORECASE):
                    token_hashtags = " ".join([f"#{t}" for t in valid_tokens[:2] if f"#{t}" not in content])
                    cta_footer = (
                        f"\n\n👇 点击上方代币标签直达盘面交易，关注我获取第一手快讯与行情策略！\n"
                        f"{campaign_tags_env} {token_hashtags}".strip()
                    )
                    content += cta_footer

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

    def publish(self, content: str) -> bool:
        if not self.api_key:
            logger.error("未配置 SQUARE_API_KEY，无法发布到币安广场！")
            return False

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
# 模块八：可选外部通知组件 (Notifier)
# ---------------------------------------------------------------------------
class Notifier:
    """支持 Telegram Bot 或通用 Webhook（钉钉/飞书/企微/Discord）通知"""

    @staticmethod
    def send_notification(title: str, message: str):
        tg_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if tg_bot_token and tg_chat_id:
            try:
                tg_url = f"https://api.telegram.org/bot{tg_bot_token}/sendMessage"
                text = f"📢 *{title}*\n\n{message}"
                requests.post(tg_url, json={"chat_id": tg_chat_id, "text": text, "parse_mode": "Markdown"}, timeout=8)
                logger.info("已发送 Telegram 状态通知。")
            except Exception as e:
                logger.warning(f"发送 Telegram 通知失败: {e}")

        webhook_url = os.getenv("WEBHOOK_URL", "").strip()
        if webhook_url:
            try:
                payload = {"msgtype": "text", "text": {"content": f"【{title}】\n{message}"}, "content": f"**{title}**\n\n{message}"}
                requests.post(webhook_url, json=payload, timeout=8)
                logger.info("已发送 Webhook 状态通知。")
            except Exception as e:
                logger.warning(f"发送 Webhook 通知失败: {e}")


# ---------------------------------------------------------------------------
# 主流程入口
# ---------------------------------------------------------------------------
def main():
    square_api_key = os.getenv("SQUARE_API_KEY", "").strip()
    max_posts = int(os.getenv("MAX_POSTS_PER_RUN", "1"))
    dry_run = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

    logger.info("==================================================")
    logger.info("🚀 币安广场加密热点与活动智能变现系统 (Ultimate 版) 启动")
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

    # 5. 获取待发布热点候选（按冲击力热度打分排序）
    candidates = fetcher.fetch_candidates(cache_mgr)
    if not candidates:
        logger.info("✅ 未检测到新的未发布热点，安全退出。")
        sys.exit(0)

    # 6. 执行发帖循环
    posted_count = 0
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

        # 动态提取相关币种并拉取实时盘面数据
        mentioned_tokens = re.findall(r"\b(BTC|ETH|BNB|SOL|DOGE|XRP|PEPE|SUI|NEAR|APT|AVAX|LINK|TRX)\b", (title + " " + item["summary"]).upper())
        target_tokens = list(dict.fromkeys(mentioned_tokens + ["BTC"]))[:3]
        live_market_data = MarketDataProvider.get_token_market_data(target_tokens)
        market_context_str = f"全网情绪指数: {fng_index}\n实时盘面数据: {live_market_data}"

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

        # 模拟自然人工操作延迟
        if posted_count < max_posts:
            delay = random.randint(3, 8)
            time.sleep(delay)

    logger.info("==================================================")
    logger.info(f"🎯 任务完成！本次成功处理/发布: {posted_count} 篇")
    logger.info("==================================================")


if __name__ == "__main__":
    main()
