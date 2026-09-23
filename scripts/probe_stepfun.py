#!/usr/bin/env python3
"""阶跃星辰订阅通道实弹探针：确认 step-5-preview 与 step-3.7-flash 在
/step_plan/v1（订阅 Credit 池）上均可用，并测量各自延迟——用于"你试一下"。

本引擎无处保存 STEPFUN_API_KEY（仅存于 GitHub Actions secrets），故此探针
需在持有该 key 的环境运行：

    STEPFUN_API_KEY=sk-xxx python scripts/probe_stepfun.py

请求与 main.py 主链路一致：同端点、chat.completions.create、同参数形态。
成功打印每个模型的延迟/首句/token 用量；任一模型报错则以非零码退出，
绝不臆造结果（数字/事件严禁编造）。/step_plan 前缀保留 = 走订阅额度。
"""
import os
import sys
import time

try:
    from openai import OpenAI
except ImportError:
    sys.exit("需要 openai 包：pip install openai")

BASE_URL = "https://api.stepfun.com/step_plan/v1"
MODELS = [
    os.getenv("STEPFUN_MODEL", "").strip() or "step-5-preview",
    os.getenv("STEPFUN_FLASH_MODEL", "").strip() or "step-3.7-flash",
]


def main() -> int:
    key = os.getenv("STEPFUN_API_KEY", "").strip()
    if not key:
        print("✗ 未设置 STEPFUN_API_KEY，无法实弹验证（不臆造结果）。")
        print("  用法：STEPFUN_API_KEY=sk-xxx python scripts/probe_stepfun.py")
        return 2

    client = OpenAI(api_key=key, base_url=BASE_URL, timeout=90.0)
    failed = []
    for model in MODELS:
        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是简洁的加密行情写手。"},
                    {"role": "user", "content": "用一句中文点评 BTC 今日行情。"},
                ],
                temperature=0.75,
                max_tokens=600,
            )
            latency = round(time.perf_counter() - t0, 2)
            msg = resp.choices[0].message if resp.choices else None
            content = ((msg.content if msg else "") or "").strip()
            usage = getattr(resp, "usage", None)
            tokens = getattr(usage, "total_tokens", None) if usage else None
            finish = getattr(resp.choices[0], "finish_reason", "") if resp.choices else ""
            status = "✓" if content else "✗ 空回"
            print(f"{status} {model}: {latency}s | token={tokens} | finish={finish}")
            print(f"    首句：{content[:80]!r}")
            if not content:
                failed.append(model)
        except Exception as exc:  # noqa: BLE001 —— 探针要如实报出上游报错
            latency = round(time.perf_counter() - t0, 2)
            print(f"✗ {model}: {latency}s 报错 -> {type(exc).__name__}: {exc}")
            failed.append(model)

    if failed:
        print(f"\n结论：以下模型不可用/需换名 -> {failed}")
        return 1
    print("\n结论：两个模型均在订阅端点可用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
