#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
==============================================================================
币安广场（Binance Square）加密热点自动发帖机器人
==============================================================================
功能：
1. 多数据源自动抓取加密货币最新热点快讯（支持 BlockTempo、Cointelegraph、CoinDesk 等）。
2. 本地 sent_cache.json 去重，避免重复发帖。
3. 调用兼容 OpenAI 规范的 LLM（如 DeepSeek）进行结构化提炼、点评、$TOKEN 提取与互动问答生成。
4. 调用币安广场创作者 OpenAPI (X-Square-OpenAPI-Key) 实现全自动发布。
5. 专为 GitHub Actions 定时任务设计，0 服务器成本、状态通过 Git 自动回写持久化。
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
logger = logging.getLogger("SquareBot")

# ---------------------------------------------------------------------------
# 常量与环境配置
# ---------------------------------------------------------------------------
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_cache.json")
MAX_CACHE_SIZE = 500  # 缓存最大保留记录数，防止文件膨胀

# 币安广场 OpenAPI 端点
BINANCE_SQUARE_API_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"

# 环境变量读取
SQUARE_API_KEY = os.getenv("SQUARE_API_KEY", "").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat").strip()
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "1"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

# 优质免费 RSS 数据源清单（按优先级排序）
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
    """管理已处理新闻的本地 JSON 缓存，实现跨运行周期的高效去重"""

    def __init__(self, cache_path: str = CACHE_FILE):
        self.cache_path = cache_path
        self.cached_items: List[Dict[str, Any]] = self._load_cache()
        self.cached_ids = {item["id"] for item in self.cached_items if "id" in item}

    def _load_cache(self) -> List[Dict[str, Any]]:
        """从文件读取已发送历史缓存"""
        if not os.path.exists(self.cache_path):
            logger.info(f"缓存文件不存在，将初始化新缓存: {self.cache_path}")
            return []
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict) and "sent_ids" in data:
                    return data["sent_ids"]
                return []
        except Exception as e:
            logger.warning(f"读取缓存文件失败 ({e})，将使用空缓存初始化。")
            return []

    def is_cached(self, news_id: str) -> bool:
        """判断新闻 ID 是否已被处理过"""
        return news_id in self.cached_ids

    def record_sent(self, news_id: str, title: str, source: str):
        """记录一条成功发布的新闻，并持久化到本地文件"""
        new_record = {
            "id": news_id,
            "title": title,
            "source": source,
            "sent_at": datetime.utcnow().isoformat() + "Z",
        }
        self.cached_items.append(new_record)
        self.cached_ids.add(news_id)

        # 限制缓存容量，保留最新记录
        if len(self.cached_items) > MAX_CACHE_SIZE:
            self.cached_items = self.cached_items[-MAX_CACHE_SIZE:]

        self._save_cache()

    def _save_cache(self):
        """将缓存写入磁盘文件"""
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.cached_items, f, ensure_ascii=False, indent=2)
            logger.info(f"缓存已持久化，当前记录数: {len(self.cached_items)}")
        except Exception as e:
            logger.error(f"保存缓存文件失败: {e}")


# ---------------------------------------------------------------------------
# 模块二：数据源抓取与解析 (NewsFetcher)
# ---------------------------------------------------------------------------
class NewsFetcher:
    """多源加密资讯抓取器"""

    @staticmethod
    def clean_html(raw_html: str) -> str:
        """清洗 HTML 标签，提取干净的纯文本"""
        if not raw_html:
            return ""
        # 移除 scripts 和 style
        clean_text = re.sub(r"<(script|style).*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
        # 移除所有 html 标签
        clean_text = re.sub(r"<[^>]+>", " ", clean_text)
        # 替换常见实体
        clean_text = clean_text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
        # 合并多个空白字符
        clean_text = re.sub(r"\s+", " ", clean_text).strip()
        return clean_text

    @staticmethod
    def generate_news_id(entry: Dict[str, Any], feed_name: str) -> str:
        """
        基于 entry id、link、title 生成确定性唯一哈希 ID
        """
        raw_identifier = entry.get("id") or entry.get("link") or entry.get("title", "")
        clean_title = entry.get("title", "").strip().lower()
        unique_seed = f"{feed_name}::{clean_title}::{raw_identifier}"
        return hashlib.sha256(unique_seed.encode("utf-8")).hexdigest()[:16]

    def fetch_candidates(self, cache_manager: CacheManager, limit_per_feed: int = 5) -> List[Dict[str, Any]]:
        """
        轮询配置的 RSS 源，抓取未处理的热点快讯
        """
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
        }

        candidates = []

        for feed_config in RSS_FEEDS:
            name = feed_config["name"]
            url = feed_config["url"]
            lang = feed_config.get("lang", "en")

            logger.info(f"正在拉取数据源: [{name}] ({url})...")
            try:
                # 使用 requests 获取内容以设置超时和自定义 UA，避免被防爬拦截
                resp = requests.get(url, headers=headers, timeout=12)
                if resp.status_code != 200:
                    logger.warning(f"数据源 [{name}] 响应异常: HTTP {resp.status_code}")
                    continue

                feed = feedparser.parse(resp.content)
                if not feed.entries:
                    logger.info(f"数据源 [{name}] 未解析到有效条目。")
                    continue

                logger.info(f"数据源 [{name}] 成功获取 {len(feed.entries)} 条资讯。")

                for entry in feed.entries[:limit_per_feed]:
                    title = entry.get("title", "").strip()
                    if not title:
                        continue

                    news_id = self.generate_news_id(entry, name)
                    if cache_manager.is_cached(news_id):
                        logger.debug(f"已处理过，跳过: {title}")
                        continue

                    # 提取正文内容或摘要
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
                        "summary": clean_summary[:800],  # 截取前 800 字提供给 AI
                        "link": link,
                        "source": name,
                        "lang": lang,
                        "published": published,
                    })

            except Exception as e:
                logger.warning(f"拉取或解析数据源 [{name}] 出错: {e}")

        logger.info(f"数据源扫描完成，共筛选出 {len(candidates)} 条未处理热点。")
        return candidates


# ---------------------------------------------------------------------------
# 模块三：AI 内容提炼与广场格式化 (AISummarizer)
# ---------------------------------------------------------------------------
class AISummarizer:
    """使用兼容 OpenAI 规范的 API 进行加密新闻提炼与币安广场定制化文案生成"""

    SYSTEM_PROMPT = """你是一名精通加密货币市场的资深分析师与币安广场（Binance Square）顶级创作者。
你的任务是将给定的最新加密货币新闻/快讯，转化为极具吸引力、高互动率且符合币安广场风格的快讯短贴。

【严格排版与内容规范】：
1. 💡 核心事实（80~150字）：用精炼有力且通俗的中文提炼事件核心要点，拒绝废话与AI套话，突出关键数字、主体与事件。
2. 📊 市场点评（1句话）：简明指出该事件对市场的潜在影响（看涨/看跌/生态格局/流动性变化）。
3. 🪙 关联代币（必须且只能提取 1~2 个最相关代币）：格式严格为大写加美元符号，例如 $BTC 、$ETH 、$SOL 。代币标签用于触发币安平台的 Write to Earn 交易组件。如果事件无特定代币，使用最相关的核心资产（如 $BTC）。
4. 💬 互动问答（1个开放式问题）：结尾抛出一个简短犀利的提问，吸引读者在评论区讨论。

【输出样式参考】：
📌 【快讯标题/核心提炼】
...（核心事实内容）...

🔍 观察与点评：...（1句话影响分析）...

🏷️ 焦点资产：$BTC $ETH

💬 你怎么看？...（互动讨论问题）...

【注意事项】：
- 仅输出格式化后的正文，不要输出任何额外的开场白或元说明（如“好的，这是为您生成的文案”等）。
- 语言统一为地道专业的简体中文。"""

    def __init__(self, api_key: str, base_url: str, model: str):
        if not api_key:
            logger.warning("未检测到 LLM_API_KEY，AI 提炼模块将无法正常发起在线请求！")
        self.client = OpenAI(
            api_key=api_key or "dummy_key",
            base_url=base_url,
        )
        self.model = model

    def summarize(self, news_item: Dict[str, Any]) -> Optional[str]:
        """
        调用 LLM 生成适合币安广场发布的快讯内容
        """
        user_prompt = f"""请将以下加密新闻提炼为一条高质量的币安广场快讯短贴：

【新闻来源】：{news_item.get('source', '未知')}
【原始标题】：{news_item.get('title', '')}
【原始内容】：{news_item.get('summary', '')}

请严格按照规范生成内容："""

        try:
            logger.info(f"正在调用 LLM ({self.model}) 提炼新闻: {news_item.get('title')}")
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.7,
                max_tokens=600,
            )

            content = response.choices[0].message.content.strip()
            # 校验是否包含代币标签（$开头的代币），若缺失则自动兜底补齐 $BTC
            if not re.search(r"\$[A-Z0-9]{2,10}", content):
                content += "\n\n🏷️ 焦点资产：$BTC"

            return content

        except Exception as e:
            logger.error(f"调用 LLM 接口失败: {e}")
            return None


# ---------------------------------------------------------------------------
# 模块四：币安广场 OpenAPI 发布 (SquarePublisher)
# ---------------------------------------------------------------------------
class SquarePublisher:
    """封装币安广场 OpenAPI 发帖接口"""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def publish(self, content: str) -> bool:
        """
        调用官方 OpenAPI 发布图文/短帖内容至币安广场
        Endpoint: POST https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add
        Header: X-Square-OpenAPI-Key, clienttype: binanceSkill
        Body: {"bodyTextOnly": content}
        """
        if not self.api_key:
            logger.error("未配置 SQUARE_API_KEY，无法发布到币安广场！")
            return False

        headers = {
            "X-Square-OpenAPI-Key": self.api_key,
            "Content-Type": "application/json",
            "clienttype": "binanceSkill",
            "User-Agent": "BinanceSquareAutoPoster/1.0",
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
                logger.error(f"发帖失败！HTTP 状态码: {status_code}, 响应内容: {resp_text}")
                return False

            try:
                resp_json = response.json()
            except Exception:
                logger.error(f"解析币安 API 响应 JSON 失败，原始内容: {resp_text}")
                return False

            # 币安 BAPI 通常返回 code: "000000" 或 success: true
            code = resp_json.get("code")
            success = resp_json.get("success", False)

            if code == "000000" or success is True or code == 0:
                data = resp_json.get("data") or {}
                content_id = data.get("contentId") or data.get("id") or "未知ID"
                logger.info(f"🎉 成功发布到币安广场！Content ID: {content_id}")
                return True
            else:
                logger.error(f"币安广场业务返回错误！响应详情: {json.dumps(resp_json, ensure_ascii=False)}")
                return False

        except Exception as e:
            logger.error(f"请求币安广场接口发生网络或未知异常: {e}")
            return False


# ---------------------------------------------------------------------------
# 模块五：主执行流程
# ---------------------------------------------------------------------------
def main():
    logger.info("==================================================")
    logger.info("🚀 币安广场加密热点自动化发帖机器人启动")
    logger.info(f"   运行时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info(f"   运行模式: {'【DRY_RUN 试运行 (不真实发帖)】' if DRY_RUN else '【正式运行】'}")
    logger.info(f"   LLM 模型: {LLM_MODEL} (Base URL: {LLM_BASE_URL})")
    logger.info(f"   单次最大发帖数: {MAX_POSTS_PER_RUN}")
    logger.info("==================================================")

    # 1. 检查必要环境变量
    if not DRY_RUN and not SQUARE_API_KEY:
        logger.error("错误: 缺少 SQUARE_API_KEY 环境变量！在生产模式下必须提供。")
        sys.exit(1)

    if not LLM_API_KEY:
        logger.warning("提示: 缺少 LLM_API_KEY 环境变量，请确保已在 GitHub Secrets 或本地配置。")

    # 2. 初始化核心组件
    cache_mgr = CacheManager(CACHE_FILE)
    fetcher = NewsFetcher()
    summarizer = AISummarizer(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, model=LLM_MODEL)
    publisher = SquarePublisher(api_key=SQUARE_API_KEY)

    # 3. 抓取候选热点
    candidates = fetcher.fetch_candidates(cache_mgr)

    if not candidates:
        logger.info("✅ 本次未发现新的未发布资讯（全部已在缓存中或暂无新新闻），安全退出。")
        sys.exit(0)

    # 4. 遍历候选新闻并发布（受 MAX_POSTS_PER_RUN 限制）
    posted_count = 0

    for news_item in candidates:
        if posted_count >= MAX_POSTS_PER_RUN:
            logger.info(f"已达到本次最大发帖上限 ({MAX_POSTS_PER_RUN} 篇)，停止发帖。")
            break

        news_id = news_item["id"]
        title = news_item["title"]
        source = news_item["source"]

        logger.info(f"--------------------------------------------------")
        logger.info(f"正在处理第 {posted_count + 1} 篇热点: [{source}] {title}")

        # 4.1 AI 提炼
        post_content = summarizer.summarize(news_item)
        if not post_content:
            logger.warning(f"AI 生成失败，跳过该条新闻: {title}")
            continue

        logger.info("生成文案预览:\n" + post_content)

        # 4.2 发帖 (或 DRY_RUN 模拟)
        if DRY_RUN:
            logger.info(f"【DRY_RUN 模式】模拟发帖成功，记录到缓存: {news_id}")
            cache_mgr.record_sent(news_id, title, source)
            posted_count += 1
        else:
            success = publisher.publish(post_content)
            if success:
                cache_mgr.record_sent(news_id, title, source)
                posted_count += 1
                logger.info(f"成功记录新闻至已发送缓存: {news_id}")
            else:
                logger.error(f"发帖失败，本次暂不记录到缓存以供下次重试: {title}")

        # 避免过于频繁请求，多条之间短暂休眠
        if posted_count < MAX_POSTS_PER_RUN:
            time.sleep(3)

    logger.info("==================================================")
    logger.info(f"🎯 本次任务完成！实际发布/处理篇数: {posted_count}")
    logger.info("==================================================")


if __name__ == "__main__":
    main()
