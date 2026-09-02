#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
==============================================================================
币安广场（Binance Square）加密热点多模型自动发帖机器人 (Pro 增强版)
==============================================================================
核心优势与特性：
1. 多源热点监听：BlockTempo、Cointelegraph、CoinDesk、Decrypt、Bitcoin Magazine 等。
2. 多 LLM 模型池与自动故障转移 (Auto-Failover)：
   - 原生支持 OpenRouter, B.ai, xkiro, TokenRouter, aihubmix, inferera, DeepSeek, 硅基流动等。
   - 当某个免费模型或接口遇到 Rate Limit (429)、欠费或超时时，自动平滑切换至下一个可用提供商。
3. 币安广场 Write to Earn 深度适配：
   - 自动提取标准大写代币标签（如 $BTC、$SOL），精准触发币安交易组件与返佣。
   - 包含快讯事实提炼、1句话深度影响点评、互动话题提问。
   - 内置敏感词与合规风控过滤（避免“带单/稳赚”等违规词）。
4. 0 服务器成本：专为 GitHub Actions 定时执行设计，通过 Git 回写 sent_cache.json 去重。
5. 可选多渠道通知：支持 Telegram / 钉钉 / 飞书 / 企业微信 / Discord Webhook 实时通知发帖结果。
==============================================================================
"""

import os
import re
import sys
import json
import time
import hashlib
import logging
from typing import List, Dict, Any, Optional
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
logger = logging.getLogger("SquarePosterPro")

# ---------------------------------------------------------------------------
# 常量与路径
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "sent_cache.json")
MAX_CACHE_SIZE = 500

# 币安广场 OpenAPI 官方端点
BINANCE_SQUARE_API_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"

# ---------------------------------------------------------------------------
# 常用预置模型提供商模板（特别适配免费模型与常用接口）
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


# ---------------------------------------------------------------------------
# 模块一：本地去重缓存管理 (CacheManager)
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
# 模块二：多源热点抓取与解析 (NewsFetcher)
# ---------------------------------------------------------------------------
class NewsFetcher:
    """热点新闻抓取与清洗"""

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

                    candidates.append({
                        "id": news_id,
                        "title": title,
                        "summary": clean_summary[:1000],
                        "link": link,
                        "source": name,
                        "lang": lang,
                        "published": published,
                    })
            except Exception as e:
                logger.warning(f"拉取数据源 [{name}] 出错: {e}")

        logger.info(f"扫描完毕，发现 {len(candidates)} 条未处理热点。")
        return candidates


# ---------------------------------------------------------------------------
# 模块三：多模型故障转移 AI 提炼引擎 (MultiLLMEngine)
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

    SYSTEM_PROMPT = """你是一名精通加密货币市场的资深分析师与币安广场（Binance Square）顶级内容创作者。
你的任务是将给定的最新加密货币新闻/快讯，转化为极具吸引力、高互动率且符合币安广场风格的快讯短贴。

【严格排版与内容规范】：
1. 💡 核心事实（80~150字）：用精炼有力且通俗的中文提炼事件核心要点，拒绝废话与AI套话，突出关键数字、主体与事件。
2. 📊 市场点评（1句话）：简明指出该事件对市场的潜在影响（看涨/看跌/生态格局/流动性变化）。
3. 🪙 关联代币（必须且只能提取 1~2 个最相关代币）：格式严格为大写加美元符号，例如 $BTC 、$ETH 、$SOL 。代币标签用于触发币安平台的 Write to Earn 交易组件。如果事件无特定代币，使用最相关的核心资产（如 $BTC）。
4. 💬 互动问答（1个开放式问题）：结尾抛出一个简短犀利的提问，吸引读者在评论区讨论。
5. ⚠️ 合规要求：禁止出现“稳赚”、“必涨”、“带单”等违法违规或保证收益字眼，结尾可自然带上“（观点仅供参考，DYOR）”。

【输出样式参考】：
📌 【快讯标题/核心提炼】
...（核心事实内容）...

🔍 观察与点评：...（1句话影响分析）...

🏷️ 焦点资产：$BTC $ETH

💬 你怎么看？...（互动讨论问题）...

【注意事项】：
- 仅输出格式化后的正文，不要输出任何额外的开场白或元说明（如“好的，这是为您生成的文案”等）。
- 语言统一为地道专业的简体中文。"""

    def __init__(self):
        self.providers: List[LLMProviderConfig] = self._build_provider_chain()

    def _build_provider_chain(self) -> List[LLMProviderConfig]:
        """构建提供商备份链"""
        chain: List[LLMProviderConfig] = []

        # 1. 优先读取高级 JSON 配置: LLM_PROVIDERS_CONFIG
        # 格式示例: [{"name": "b.ai", "base_url": "https://api.b.ai/v1", "api_key": "sk-xxx", "model": "deepseek-v4-flash"}]
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

        # 3. 检查是否有单独配置的常见平台 Key (多提供商自动探测)
        # 例如 OPENROUTER_API_KEY, BAI_API_KEY, XKIRO_API_KEY, SILICONFLOW_API_KEY 等
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

    def summarize(self, news_item: Dict[str, Any]) -> Optional[str]:
        """
        带自动故障转移的 AI 提炼
        """
        if not self.providers:
            logger.error("没有任何可用的 LLM 提供商配置！")
            return None

        user_prompt = f"""请将以下加密新闻提炼为一条高质量的币安广场快讯短贴：

【新闻来源】：{news_item.get('source', '未知')}
【原始标题】：{news_item.get('title', '')}
【原始内容】：{news_item.get('summary', '')}

请严格按照规范生成内容："""

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
                    max_tokens=600,
                )

                content = response.choices[0].message.content.strip()

                # 代币标签保底校验：如果没有检测到 $TOKEN，自动补齐 $BTC
                if not re.search(r"\$[A-Z0-9]{2,10}", content):
                    content += "\n\n🏷️ 焦点资产：$BTC"

                logger.info(f"🎉 模型 [{provider.name}] 生成成功！")
                return content

            except Exception as e:
                err_msg = str(e)
                logger.warning(f"提供商 [{provider.name}] 请求失败: {err_msg}")
                # 若还有后续提供商，则继续重试；否则返回 None
                if index < len(self.providers) - 1:
                    logger.info(f"正在自动切换至下一个备用提供商...")
                    time.sleep(1)

        logger.error("所有已配置的 LLM 提供商均调用失败！")
        return None


# ---------------------------------------------------------------------------
# 模块四：币安广场 OpenAPI 客户端 (SquarePublisher)
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
            "User-Agent": "BinanceSquareAutoPosterPro/2.0",
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
# 模块五：可选外部通知组件 (Notifier)
# ---------------------------------------------------------------------------
class Notifier:
    """支持 Telegram Bot 或通用 Webhook（钉钉/飞书/企微/Discord）通知"""

    @staticmethod
    def send_notification(title: str, message: str):
        # 1. Telegram 通知
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

        # 2. 通用 Webhook (钉钉 / 飞书 / 企微 / Discord)
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
    logger.info("🚀 币安广场加密热点自动化发帖机器人 (Pro 版) 启动")
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

    # 3. 获取待发布热点候选
    candidates = fetcher.fetch_candidates(cache_mgr)
    if not candidates:
        logger.info("✅ 未检测到新的未发布热点，安全退出。")
        sys.exit(0)

    # 4. 执行发帖循环
    posted_count = 0
    for item in candidates:
        if posted_count >= max_posts:
            logger.info(f"已达到本次最大发帖数 ({max_posts})，退出循环。")
            break

        news_id = item["id"]
        title = item["title"]
        source = item["source"]

        logger.info(f"--------------------------------------------------")
        logger.info(f"正在处理第 {posted_count + 1} 条热点: [{source}] {title}")

        # AI 提炼
        post_content = llm_engine.summarize(item)
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

        if posted_count < max_posts:
            time.sleep(3)

    logger.info("==================================================")
    logger.info(f"🎯 任务完成！本次成功处理/发布: {posted_count} 篇")
    logger.info("==================================================")


if __name__ == "__main__":
    main()
