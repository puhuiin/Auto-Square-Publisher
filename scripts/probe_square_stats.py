# -*- coding: utf-8 -*-
"""R790 探针：币安广场 OpenAPI 是否有「内容统计/列表」端点（浏览/互动数据）。

背景（为什么值得探）：engagement loop（R608-R611「按真实浏览调」）的数据源
一直是**用户手工导出**创作者中心 CSV，第七批之后断档数周 ⇒ 排序加权用的
token_engagement 停在 10-11 的快照。若 OpenAPI 暴露了内容统计/列表端点，
机器人就能自刷新，engagement loop 从「等人工导出」变成持续闭环。

纪律：**只读探针**（GET/POST 到币安自家 bapi，不带任何写操作），全部结果
落 step summary + 日志；不猜业务语义，只回答「哪些路径存在、返回什么形状」。
无 key 时安静跳过；任何异常都不允许影响发帖主流程（探针在独立 step 跑）。
"""
import json
import os
import sys
import urllib.error
import urllib.request

KEY = (os.environ.get("SQUARE_API_KEY") or "").strip()
TIMEOUT = 15

# 候选端点（按 bapi 命名惯例列，不预设哪个存在——探针的目的就是问这个问题）
CANDIDATES = [
    ("POST", "/bapi/composite/v1/public/pgc/openApi/content/list",
     {"pageNo": 1, "pageSize": 5}),
    ("POST", "/bapi/composite/v1/public/pgc/openApi/content/query",
     {"pageNo": 1, "pageSize": 5}),
    ("POST", "/bapi/composite/v2/public/pgc/openApi/content/list",
     {"pageNo": 1, "pageSize": 5}),
    ("GET", "/bapi/composite/v1/public/pgc/openApi/content/list?pageNo=1&pageSize=5", None),
    ("POST", "/bapi/composite/v1/public/pgc/openApi/content/statistics", {}),
    ("POST", "/bapi/composite/v2/public/pgc/openApi/content/statistics", {}),
    ("POST", "/bapi/composite/v1/public/pgc/openApi/author/statistics", {}),
    ("GET", "/bapi/composite/v1/public/pgc/openApi/content/detail?contentId=probe", None),
]

HEADERS = {
    "X-Square-OpenAPI-Key": KEY,
    "Content-Type": "application/json",
    "clienttype": "binanceSkill",
    "User-Agent": "BinanceSquareAutoPosterPro/3.0",
}


def probe(method, path, body):
    url = "https://www.binance.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, raw[:600]
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", "replace")[:400] if e.fp else "")
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def main():
    if not KEY:
        print("PROBE_SKIP: 无 SQUARE_API_KEY")
        return
    lines = ["## R790 探针：币安广场 OpenApi 内容统计端点", ""]
    interesting = []
    for method, path, body in CANDIDATES:
        status, snippet = probe(method, path, body)
        tag = "?"
        if status == 200:
            try:
                payload = json.loads(snippet)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                code = str(payload.get("code", ""))
                success = payload.get("success")
                if code in ("000000", "0") or success is True:
                    tag = "HIT"
                    interesting.append((method, path, snippet[:400]))
                else:
                    tag = "code=%s" % (code or success)
        print("%-4s %-70s -> %s %s" % (method, path[:70], status, tag))
        lines.append("- `%s %s` → HTTP %s %s" % (method, path, status, tag))
    lines.append("")
    if interesting:
        lines.append("### 命中（返回体片段，需人工判断语义）")
        for method, path, snip in interesting:
            lines.append("```json\n%s %s\n%s\n```" % (method, path, snip))
    else:
        lines.append("未命中：候选端点均不存在/无权限/返回非成功码 ⇒ engagement loop "
                     "继续走用户 CSV 导出路径（R740 的 import_content_stats.py）。")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    print("\n".join(lines[-6:]))


if __name__ == "__main__":
    sys.exit(main())
