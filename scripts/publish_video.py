#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性视频发布脚本：把本地视频文件上传到币安广场并发帖（长文视频帖，contentType=3）。

用法（需设置 SQUARE_API_KEY 环境变量）：
  export SQUARE_API_KEY="你的Key"
  python scripts/publish_video.py <视频路径> [--title "标题"] [--body "正文"] [--cover 封面图] [--dry]

R383 修复：旧实现把视频当**图片**发——走 /image/presignedUrl 申请凭证，就绪回执里
取 imageUrl/videoUrl 当作视频托管 URL，再以 videoList=[URL] + contentType=2 发布。
但币安广场视频是 **contentType=3 + fileTicket** 关联（图片才用托管 URL），视频就绪
回执里根本没有 imageUrl/videoUrl，旧实现恒在「转码就绪: None」处失败（生产 workflow
run 36201217651 实测 100% 失败）。现改为复用 main.py 内经生产验证的
VideoManager.upload_to_binance（→ fileTicket）与 SquarePublisher.publish_video
（contentType=3），与每日定投 _maybe_post_daily_video 同源。

视频帖无独立 title 字段：标题作为文案首行（与 main._build_video_caption 同源）。
正文经 publish_video 内的短讯净化 + 织挂件 + 挂件保底 + 活动标签，保住 $挂件返佣生命线。
时长只取 ffprobe 实测秒数，拿不到就省略 videoTimeSeconds——绝不编造（红线：数字严禁编造）。
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as m  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 币安 S3 单文件上限的保守兜底：防止误传一个几百 MB 的文件把上传窗口耗光
MAX_VIDEO_BYTES = 200 * 1024 * 1024
# $挂件识别（返佣生命线）：ASCII 标的直接匹配；CJK 前后文不能用 \b（中文非单词边界），
# 用「$ + 2~10 位大写字母/数字」宽松扫描，抽出文案里已有的标的当作挂件保底 ensure_tokens。
_CASHTAG_RE = re.compile(r"\$([A-Z0-9]{2,10})")


def resolve_video_path(raw: str) -> str:
    """把命令行给的相对路径解析成真实路径。

    相对路径按**仓库根**解析而非当前工作目录：脚本常被 workflow 从别处调用，
    且手动 `cd scripts && python publish_video.py assets/x.mp4` 时 CWD 不是仓库根。"""
    if os.path.isabs(raw):
        return raw
    if os.path.exists(raw):
        return raw
    candidate = os.path.join(REPO_ROOT, raw)
    return candidate if os.path.exists(candidate) else raw


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


def record_sent(title: str, source: str = "video") -> bool:
    """把视频帖登记进 sent_cache（幂等去重 + 24h 配额都依赖这张表）。

    返回是否落盘成功。失败不阻断——帖子已经发出去了，这里只是让下一轮能看见它。"""
    try:
        cache = m.CacheManager(m.CACHE_FILE)
        news_id = "video-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
        ok = cache.record_sent(news_id, title, source, tokens=[])
        if not ok:
            print("⚠️ sent_cache 落盘失败：本次视频帖未登记，重跑可能双发（请手动核对）。")
        return ok
    except Exception as e:
        print(f"⚠️ 登记 sent_cache 异常（不影响已发布的帖子）: {e}")
        return False


def build_caption(title: str, body: str) -> str:
    """标题作首行 + 空行 + 正文（视频帖无独立 title 字段，与 main._build_video_caption 同源）。"""
    title = (title or "").strip()
    body = (body or "").strip()
    parts = [p for p in (title, body) if p]
    return "\n\n".join(parts) if parts else (title or body)


def extract_ensure_tokens(text: str) -> list:
    """从文案里抽出已有 $标的（去 $ 前缀）当作挂件保底，剔除强制剥离词（ETF/USDT 等）。
    没抽到就返回空——publish_video 仍会按其自身逻辑处理，正文里的 $挂件不受影响。"""
    strip = set(getattr(m.SquarePublisher, "FORCE_STRIP_CASHTAGS", []))
    seen, out = set(), []
    for sym in _CASHTAG_RE.findall(text or ""):
        if sym in strip or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def upload_video(api_key: str, video_path: str) -> "str | None":
    """上传视频到币安 S3，返回 **fileTicket**（None = 失败）。

    委托 main.VideoManager.upload_to_binance：/video/preSign {fileName,size} → S3 PUT →
    轮询 imageStatus 就绪 → 返回 fileTicket。视频以 fileTicket 关联发布，不是托管 URL。"""
    try:
        size = os.path.getsize(video_path)
    except OSError as e:
        print(f"❌ 无法读取视频文件属性: {e}")
        return None
    if size > MAX_VIDEO_BYTES:
        print(f"❌ 视频过大 ({size / 1024 / 1024:.1f} MB)，上限 {MAX_VIDEO_BYTES // 1024 // 1024} MB")
        return None
    with open(video_path, "rb") as f:
        video_bytes = f.read()
    print(f"📤 上传视频 ({len(video_bytes) / 1024 / 1024:.1f} MB) 到币安 S3（视频通道 /video/preSign）...")
    file_ticket = m.VideoManager.upload_to_binance(api_key, video_bytes, os.path.basename(video_path))
    if file_ticket:
        print(f"🎉 视频就绪: fileTicket={file_ticket}")
    else:
        print("❌ 视频上传/转码失败（未取得 fileTicket）")
    return file_ticket


def extract_cover_frame(video_path: str) -> "str | None":
    """尽力用 ffmpeg 抽首帧做封面；ffmpeg 不可用/失败一律返回 None（无封面照样能发）。
    argument-array 调用、无 shell、有界超时。"""
    try:
        fd, cover_path = tempfile.mkstemp(suffix="-cover.jpg")
        os.close(fd)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vf", "thumbnail", "-frames:v", "1", cover_path],
            capture_output=True, text=True, timeout=60, shell=False,
        )
        if proc.returncode == 0 and os.path.exists(cover_path) and os.path.getsize(cover_path) > 0:
            return cover_path
    except Exception as e:
        print(f"ℹ️ 封面抽帧跳过（不影响发布）: {e}")
    return None


def upload_cover(api_key: str, cover_path: str) -> "str | None":
    """上传封面图，返回托管 URL（失败返回 None，转为无封面发布）。"""
    try:
        with open(cover_path, "rb") as f:
            cover_bytes = f.read()
    except OSError as e:
        print(f"ℹ️ 读取封面失败（将无封面发布）: {e}")
        return None
    url = m.ImageManager.upload_to_binance(api_key, cover_bytes, "video-cover-v.jpg", "image/jpeg")
    if url:
        print(f"🖼️ 封面已上传: {url}")
    else:
        print("ℹ️ 封面上传失败，将以无封面发布。")
    return url


# ---- LP 流动性池讲解的默认文案（交易员人设风格，$挂件 + 看法 + 活动标签） ----
DEFAULT_TITLE = "3分钟搞懂LP流动性池：给DEX当庄家，你赚的到底是谁的钱？"
DEFAULT_BODY = """做了一段3分钟的视频，专门讲 LP 流动性池到底怎么运作、普通人下场当"庄家"该注意什么。

先说原理：你往池子里存一对代币，别人来 swap 就付手续费给你。听着像躺赚，但有三个坑不搞明白迟早交学费。

第一，无常损失。只要两个币的相对价格一动，池子里的比例就被套利者重新配平，跟单纯拿现货比，单边行情里你大概率是少赚的。

第二，手续费收益跟真实交易量强相关，跟标称的那个大 APY 关系不大。池子越冷门，摊到你头上的费越薄，选池子别只盯年化数字。

第三，合约风险是实打实的，池子被攻击本金可能直接归零，这不是吓唬人。

说说我的看法：真要下场当 LP，我更愿意在交易深度和真实使用量都扎实的生态里做主流对，比如 $BNB 这类链上 DEX 活跃度摆在明面上的资产，安全边际比去追高年化的土狗池子高不少——高 APY 常常是拿无常损失和跑路风险换来的。

觉得有用扣个1，想看某个具体协议的 LP 拆解扣2。

#Write2Earn #BinanceSquare"""

def main() -> int:
    parser = argparse.ArgumentParser(description="一次性视频发布到币安广场 (contentType=3)")
    parser.add_argument("video", help="视频文件路径 (.mp4/.mov/.webm/.mkv)")
    parser.add_argument("--title", default=DEFAULT_TITLE, help="标题（作为文案首行）")
    parser.add_argument("--body", default=DEFAULT_BODY, help="正文文本")
    parser.add_argument("--cover", default="", help="封面图路径（留空则尝试 ffmpeg 抽首帧，失败则无封面）")
    parser.add_argument("--dry", action="store_true", help="DRY 模式：只上传不发帖")
    parser.add_argument("--force", action="store_true", help="跳过双发守卫强制发布")
    args = parser.parse_args()

    api_key = os.getenv("SQUARE_API_KEY", "").strip()
    if not api_key:
        print("❌ 未设置 SQUARE_API_KEY 环境变量。")
        print("   本地运行: export SQUARE_API_KEY=\"你的Key\"")
        print("   或在 GitHub Secrets 中已配置，用 workflow 触发。")
        return 1

    video_path = resolve_video_path(args.video)
    if not os.path.exists(video_path):
        print(f"❌ 视频文件不存在: {args.video}（按仓库根解析为 {video_path}）")
        return 1

    caption = build_caption(args.title, args.body)
    # 双发守卫的标题键：与 record_sent 登记的一致（用首行标题，稳定可比）
    dedup_key = (args.title or caption.split("\n", 1)[0]).strip()

    print(f"🎬 视频发布准备: {video_path}")
    print(f"   标题: {dedup_key[:40]}...")
    print(f"   文案首段: {caption[:40]}...")

    # R111：双发守卫——同标题帖子已存在时警告（不阻断，--force 跳过）
    if check_duplicate(dedup_key):
        print(f"\n⚠️ 警告: sent_cache 中已有同标题「{dedup_key[:30]}…」的帖子。")
        if not args.force:
            print("   可能是重复发布。加 --force 跳过此检查继续发布。\n")
            return 1
        print("   --force 已指定，继续发布。\n")
    print()

    # 上传视频 → fileTicket（视频以 fileTicket 关联，不是托管 URL）
    file_ticket = upload_video(api_key, video_path)
    if not file_ticket:
        print("❌ 视频上传失败，无法发布")
        return 1

    if args.dry:
        print(f"🏁 DRY 模式：视频已上传 (fileTicket={file_ticket})，跳过发布")
        return 0

    # 封面：显式 --cover 优先；否则尽力 ffmpeg 抽首帧；都没有就无封面发布
    cover_url = None
    cover_src = args.cover.strip() or extract_cover_frame(video_path)
    if cover_src and os.path.exists(cover_src):
        cover_url = upload_cover(api_key, cover_src)

    # 时长：ffprobe 实测，拿不到则省略（绝不编造）
    video_seconds = m.VideoManager.probe_duration_seconds(video_path)

    # $挂件返佣生命线：把文案里已有的标的抽出来当保底 ensure_tokens
    ensure_tokens = extract_ensure_tokens(caption) or None

    publisher = m.SquarePublisher(api_key)
    ok = publisher.publish_video(
        caption, file_ticket, cover_url,
        video_seconds=video_seconds, ensure_tokens=ensure_tokens, campaign_intel=None,
    )
    if ok:
        cid = getattr(publisher, "last_content_id", None)
        if cid:
            print(f"🎉 发布成功！Content ID: {cid}")
            print(f"   帖子链接: https://www.binance.com/zh-CN/square/post/{cid}")
        else:
            print("🎉 发布成功（未返回 Content ID，可能 504 已入队）")
        # 登记去重缓存：R111 双发守卫与 24h 配额唯一的数据来源
        if record_sent(dedup_key):
            print("🧾 已登记 sent_cache（workflow 会随状态回写提交，重跑将被守卫拦下）")
    else:
        print(f"❌ 发布失败: {getattr(publisher, 'last_error', '未知错误')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

