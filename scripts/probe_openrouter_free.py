#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性探针：拉 OpenRouter 实时模型目录，筛出 $0 模型。

为什么要实时拉而不用搜索结果：免费模型名轮换极快（本项目 R263 已两次踩坑：
minimax-m3:free 404、tokenrouter glm-5.3-free 悄然消失），第三方聚合站
（freellm.net 等）的清单是二手快照，日期可能滞后数周。**只有官方 /api/v1/models
是事实源**。
用法: python scripts/probe_openrouter_free.py
"""
import json
import os
import sys
import urllib.request

URL = "https://openrouter.ai/api/v1/models"


def fetch(timeout=25):
    req = urllib.request.Request(URL, headers={"User-Agent": "Auto-Square-Publisher/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main():
    try:
        data = fetch()
    except Exception as e:  # 网络/解析失败：打印并以非零退出，不静默返回空
        print("拉取 OpenRouter 目录失败: %s: %s" % (type(e).__name__, e))
        return 2
    models = data.get("data") or []
    free = [m for m in models
            if str((m.get("pricing") or {}).get("prompt")) in ("0", "0.0", 0)
            and str((m.get("pricing") or {}).get("completion")) in ("0", "0.0", 0)]
    print("OpenRouter 目录模型数: %d   定价 $0 的: %d" % (len(models), len(free)))
    # 只列带 :free 后缀或官方聚合路由的（站点真正的"免费档"）
    tagged = [m for m in free
              if str(m.get("id", "")).endswith(":free") or m.get("id") == "openrouter/free"]
    print("其中带 :free 后缀/ 官方聚合路由: %d" % len(tagged))
    print()
    rows = []
    for m in sorted(tagged, key=lambda x: str(x.get("id"))):
        mid = str(m.get("id"))
        ctx = m.get("context_length") or 0
        rows.append((mid, ctx))
    for mid, ctx in rows:
        print("  %-56s ctx=%s" % (mid, ctx if ctx else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
