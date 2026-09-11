#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性视频发布脚本：把本地视频文件上传到币安广场 S3 并发帖。

用法（需设置 SQUARE_API_KEY 环境变量）：
  export SQUARE_API_KEY="你的Key"
  python scripts/publish_video.py <视频路径> [--body "正文文本"] [--dry]

上传流程复用 main.ImageManager 的 presigned URL 机制（探测确认端点对
视频文件名与图片文件名行为一致）；发布 payload 尝试 videoList 字段
（与 imageList 平行的官方字段名），失败时降级 imageList + 附件说明。

正文与标题由调用方提供（或用内置默认文案），走 _sanitize_content
合规清洗后发布。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as m  # noqa: E402


def check_duplicate(title: str) -> bool:
    """R111：双发守卫——sent_cache 里已有同标题的帖子时警告（不阻断，由人决定）。
    视频是一次性手动操作，误触重跑是最常见的双发场景。"""
    try:
        cache = m.CacheManager(m.CACHE_FILE)
        for item in cache.cached_items:
            if isinstance(item, dict) and item.get("title", "").strip() == title.strip():
                return True
    except Exception:
        pass
    return False


def upload_video(api_key: str, video_path: str) -> str | None:
    """上传视频到币安 S3，返回托管 URL（None = 失败）。"""
    with open(video_path, "rb") as f:
        video_bytes = f.read()
    size_mb = len(video_bytes) / 1024 / 1024
    print(f"📤 上传视频 ({size_mb:.1f} MB) 到币安 S3...")

    # 复用 presigned URL 三步流程；视频转码轮询给更长窗口（视频比图片慢）
    headers = {
        "X-Square-OpenAPI-Key": api_key,
        "Content-Type": "application/json",
        "clienttype": "binanceSkill",
        "User-Agent": "BinanceSquareAutoPosterPro/3.0",
    }
    # 步骤 1：申请凭证
    res = m.http_post(m.ImageManager.PRESIGNED_URL_API, headers=headers,
                      json={"imageName": "video.mp4"}, timeout=15, retries=1)
    if res is None or res.status_code != 200:
        print(f"❌ 获取上传凭证失败: {'网络错误' if res is None else f'HTTP {res.status_code}'}")
        return None
    res_json = res.json()
    if res_json.get("code") != "000000":
        print(f"❌ 凭证接口业务异常: {res_json.get('message')}")
        return None
    data = res_json.get("data") or {}
    presigned_url = data.get("presignedUrl")
    file_ticket = data.get("fileTicket")
    if not presigned_url or not file_ticket:
        print("❌ 未提取到 presignedUrl/fileTicket")
        return None

    # 步骤 2：PUT 上传二进制
    s3_res = m.http_request("PUT", presigned_url,
                            headers={"Content-Type": "video/mp4"},
                            data=video_bytes, timeout=120, retries=1)
    if s3_res is None or s3_res.status_code not in (200, 204):
        print(f"❌ S3 上传失败: {'网络错误' if s3_res is None else f'HTTP {s3_res.status_code}'}")
        return None
    print("✅ 视频已送达 S3，等待转码...")

    # 步骤 3：轮询转码状态（视频给 15 次 × 4 秒 = 60 秒窗口）
    for i in range(15):
        time.sleep(4)
        stat = m.http_post(m.ImageManager.IMAGE_STATUS_API, headers=headers,
                           json={"fileTicket": file_ticket}, timeout=10, retries=1)
        if stat is not None and stat.status_code == 200:
            sj = stat.json().get("data") or {}
            status = sj.get("status")
            if status == 1:
                url = sj.get("imageUrl") or sj.get("videoUrl")
                print(f"🎉 转码就绪: {url}")
                return url
            if status == 2:
                print(f"❌ 审核未通过: {sj.get('failedReason')}")
                return None
        print(f"  等待转码... ({i + 1}/15)")
    print("⚠️ 转码轮询超时（60s），视频可能仍在处理中")
    return None


def publish(api_key: str, body: str, video_url: str | None, title: str | None = None) -> bool:
    """发布到币安广场。"""
    # 走既有净化管线（合规清洗+挂件+标签）
    content = m.SquarePublisher._sanitize_content(body)
    headers = {
        "X-Square-OpenAPI-Key": api_key,
        "Content-Type": "application/json",
        "clienttype": "binanceSkill",
        "User-Agent": "BinanceSquareAutoPosterPro/3.0",
    }
    payload = {"bodyTextOnly": content}
    if title:
        payload["contentType"] = 2
        payload["title"] = title[:80]
        if video_url:
            payload["cover"] = video_url
    else:
        payload["contentType"] = 1
        if video_url:
            payload["videoList"] = [video_url]
    print(f"📝 发布 payload 键: {list(payload.keys())}")
    res = m.http_post(m.BINANCE_SQUARE_API_URL, headers=headers, json=payload,
                      timeout=20, retries=1)
    if res is None or res.status_code != 200:
        print(f"❌ 发布失败: {'网络错误' if res is None else f'HTTP {res.status_code} {res.text[:200]}'}")
        return False
    rj = res.json()
    if rj.get("code") == "000000" or rj.get("success"):
        cid = (rj.get("data") or {}).get("contentId")
        print(f"🎉 发布成功！Content ID: {cid}")
        if cid:
            print(f"   帖子链接: https://www.binance.com/zh-CN/square/post/{cid}")
        return True
    print(f"❌ 业务错误: {rj.get('message')} (code={rj.get('code')})")
    # R111：videoList 被拒时用 imageList 重试（任何错误都试一次降级，
    # 不只匹配 "field" 关键词——API 错误消息格式不可预测）
    if "videoList" in payload:
        print("💡 尝试降级：移除 videoList，只用 imageList...")
        payload.pop("videoList", None)
        payload["imageList"] = [video_url]
        res2 = m.http_post(m.BINANCE_SQUARE_API_URL, headers=headers, json=payload,
                           timeout=20, retries=1)
        if res2 is not None and res2.status_code == 200:
            rj2 = res2.json()
            if rj2.get("code") == "000000" or rj2.get("success"):
                cid2 = (rj2.get("data") or {}).get("contentId")
                print(f"🎉 降级发布成功！Content ID: {cid2}")
                if cid2:
                    print(f"   帖子链接: https://www.binance.com/zh-CN/square/post/{cid2}")
                return True
            print(f"❌ 降级也失败: {rj2.get('message')} (code={rj2.get('code')})")
    return False


# ---- LP 流动性池讲解的默认文案（交易员人设风格） ----
DEFAULT_TITLE = "3分钟搞懂LP流动性池：你给DEX当庄家，赚的是谁的钱？"
DEFAULT_BODY = """做了一段3分钟的视频，把 LP 流动性池的运作机制掰开讲透了。

简单说：你往池子里放一对代币（比如 $BNB + USDT），别人来交易时付手续费给你。听起来像躺着赚，但这里面有三个坑必须知道。

第一是无常损失。币价一波动，你池子里的资产比例就变，对比单纯拿着，你可能少赚甚至亏钱。视频里用具体数字算了这笔账。

第二是费率收益其实跟交易量挂钩。池子越大费率越薄，冷门池子交易少赚的也少。选池子不能只看APY那个大数字。

第三是智能合约风险。池子被黑了，你的钱就没了。这不是理论风险，每年都有大案子。

视频里把这三点用动画演示了一遍，看完你就知道什么币适合做LP，什么情况该撤。

觉得有用的话扣个1，想看某个具体协议的LP分析扣2。

#Write2Earn #BinanceSquare #LP"""


def main() -> int:
    parser = argparse.ArgumentParser(description="一次性视频发布到币安广场")
    parser.add_argument("video", help="视频文件路径 (.mp4)")
    parser.add_argument("--title", default=DEFAULT_TITLE, help="帖子标题（长文模式）")
    parser.add_argument("--body", default=DEFAULT_BODY, help="正文文本")
    parser.add_argument("--dry", action="store_true", help="DRY 模式：只上传不发帖")
    parser.add_argument("--force", action="store_true", help="跳过双发守卫强制发布")
    args = parser.parse_args()

    api_key = os.getenv("SQUARE_API_KEY", "").strip()
    if not api_key:
        print("❌ 未设置 SQUARE_API_KEY 环境变量。")
        print("   本地运行: export SQUARE_API_KEY=\"你的Key\"")
        print("   或在 GitHub Secrets 中已配置，用 workflow 触发。")
        return 1

    if not os.path.exists(args.video):
        print(f"❌ 视频文件不存在: {args.video}")
        return 1

    print(f"🎬 视频发布准备: {args.video}")
    print(f"   标题: {args.title[:40]}...")
    print(f"   正文: {args.body[:40]}...")

    # R111：双发守卫——同标题帖子已存在时警告（不阻断，--force 跳过）
    if check_duplicate(args.title):
        print(f"\n⚠️ 警告: sent_cache 中已有同标题「{args.title[:30]}…」的帖子。")
        if not args.force:
            print("   可能是重复发布。加 --force 跳过此检查继续发布。\n")
            return 1
        print("   --force 已指定，继续发布。\n")
    print()

    # 上传视频
    video_url = upload_video(api_key, args.video)
    if not video_url:
        print("❌ 视频上传失败，无法发布")
        return 1

    if args.dry:
        print(f"🏁 DRY 模式：视频已上传 ({video_url})，跳过发布")
        return 0

    # 发布
    ok = publish(api_key, args.body, video_url, title=args.title)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
