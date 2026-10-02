#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性探针：核对 provider 池里各站默认模型名是否仍在线（防 R263 僵尸名）。

背景（R263）：免费模型名随站点轮换频繁失效——minimax-m3:free 被 OpenRouter 下架、
tokenrouter 的 glm-5.3-free 悄然消失。每次都要人工逐站核验，既慢又会漏。
本探针把「默认名是否还活着」变成一次可重复的检查：直接打各站公开目录，
把池内默认名逐一对照，404/不在目录里的一眼可见。

只发 GET 列举请求，不做生成——不消耗额度、不产生费用。

用法: python scripts/probe_provider_models.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 20

# (站点名, 目录 URL, 池内默认名, 是否走目录全量比对)
# 说明：部分站点（xAI/兼容网关）目录需要 key 才能列，此时降级为"仅连通性检查"，
# 但仍区分 401/403（key 缺失或无效）与 404（端点不存在）——前者是配置问题，
# 后者是集成问题，不能混为一谈。
SITES = [
    ("openrouter", "https://openrouter.ai/api/v1/models",
     "openrouter/free", False),
    ("google", "https://generativelanguage.googleapis.com/v1beta/models",
     "gemini-3-flash-preview", True),
]


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Auto-Square-Publisher/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def check_openrouter(default_model):
    data = _get_json("https://openrouter.ai/api/v1/models")
    ids = {str(m.get("id")) for m in (data.get("data") or [])}
    free = sorted(i for i in ids if i.endswith(":free") or i == "openrouter/free")
    return {
        "site": "openrouter",
        "ok": default_model in ids,
        "default": default_model,
        "total": len(ids),
        "free_count": len(free),
        "free_models": free,
    }


def check_google(default_model):
    key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not key:
        return {"site": "google", "ok": None, "default": default_model,
                "note": "未设 GOOGLE_API_KEY，跳过（CI 里由 secret 注入）"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    data = _get_json(url)
    ids = set()
    for m in (data.get("models") or []):
        name = str(m.get("name") or "")
        methods = m.get("supportedGenerationMethods") or []
        if "generateContent" in methods:
            ids.add(name.split("/")[-1])
    gen = sorted(i for i in ids if "flash" in i.lower())
    return {
        "site": "google",
        "ok": default_model in ids,
        "default": default_model,
        "total": len(ids),
        "flash_models": gen,
    }


def main():
    print("=== provider 池默认模型名存活核对（R263 僵尸名检查）===")
    results = []
    for site, _url, default, _full in SITES:
        try:
            if site == "openrouter":
                results.append(check_openrouter(default))
            elif site == "google":
                results.append(check_google(default))
        except urllib.error.HTTPError as e:
            results.append({"site": site, "ok": None, "default": default,
                            "note": f"目录不可达 HTTP {e.code}（401/403=key 问题，404=端点问题）"})
        except Exception as e:
            results.append({"site": site, "ok": None, "default": default,
                            "note": f"{type(e).__name__}: {e}"})
    for r in results:
        print()
        print(f"[{r['site']}] 默认模型: {r['default']}")
        if r.get("note"):
            print(f"  ⚠️ {r['note']}")
            continue
        print(f"  目录模型数: {r.get('total')}  默认名存活: {'是' if r['ok'] else '否 ← 僵尸名，需换'}")
        if r.get("free_models"):
            print(f"  当前免费模型（{r['free_count']} 个）:")
            for m in r["free_models"]:
                print(f"    {m}")
        if r.get("flash_models"):
            print("  当前 Flash 系可生成模型:")
            for m in r["flash_models"]:
                print(f"    {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())