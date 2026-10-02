#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性探针：验证 GOOGLE_API_KEY 在 Google AI Studio 免费层的可用模型。

为什么不查文档而直接打接口：Gemini 免费层模型名与免费额度变动极快（2.5 Pro /
3.1 Pro 已于 2026-04-01 移除免费层），文档快照必然滞后。**只有 /v1beta/models
返回的目录是事实源**，与本项目 R263「免费模型名会静默失效」的教训一致。

只发 GET 列举请求，不做生成——不消耗用户额度，不产生费用。

用法: GOOGLE_API_KEY=xxx python scripts/probe_google_free.py
"""
import json
import os
import sys
import urllib.request

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
KEY = os.getenv("GOOGLE_API_KEY", "").strip()


def fetch(timeout=25):
    url = f"{BASE}?key={KEY}"
    req = urllib.request.Request(url, headers={"User-Agent": "Auto-Square-Publisher/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main():
    if not KEY:
        print("GOOGLE_API_KEY 未设置（本地探针可跳过；CI 里由 secret 注入）")
        return 2
    try:
        data = fetch()
    except Exception as e:  # 失败只报原因，不静默——探针的价值就在于区分"没key"与"没模型"
        print("拉取 Gemini 模型目录失败: %s: %s" % (type(e).__name__, e))
        return 2
    models = data.get("models") or []
    print("Gemini 目录模型数: %d" % len(models))
    print()
    print("=== 支持 generateContent 的模型 ===")
    rows = []
    for m in models:
        name = str(m.get("name") or "")
        methods = m.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        short = name.split("/")[-1]
        in_tier = m.get("inputTokenLimit") or 0
        out_tier = m.get("outputTokenLimit") or 0
        rows.append((short, in_tier, out_tier))
    for short, i, o in sorted(rows):
        print("  %-44s in=%-9s out=%s" % (short, i or "-", o or "-"))
    print()
    print("建议候选（免费层 Flash 系，剔除非 generateContent 的嵌入/检索模型）")
    for short, _i, _o in sorted(rows):
        if "flash" in short.lower():
            print("  gemini-%s" % short if not short.startswith("gemini-") else short)
    return 0


if __name__ == "__main__":
    sys.exit(main())