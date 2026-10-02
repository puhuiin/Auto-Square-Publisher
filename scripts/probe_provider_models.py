#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""provider 池默认模型名存活核对（防 R263 僵尸名）+ 结果落遥测（R617）。

背景（R263）：免费模型名随站点轮换频繁失效——minimax-m3:free 被 OpenRouter 下架、
tokenrouter 的 glm-5.3-free 悄然消失、xkiro 的 qwen3.8-max 全目录已无条目。
每次都要人工逐站核验，既慢又会漏。

R617 修的是R615 探针的三个结构性缺口（按严重度递进）：

- **L1 覆盖面对drift**：R615 把站点硬编码在 SITES 里，只列了 2 站，而池内实际有
  12 站。**历史 3 次僵尸名事件有 2 次发生在探针盲区**（tokenrouter / xkiro）——
  探针存在却恰好漏掉了它要防的那类事故。现在改为用 AST 直接解析 main.py 的
  `extra_keys`，覆盖面对齐池定义，**新增站点自动纳入检查**（结构上不可能漂移）。
- **L2 结论无消费面**：R615 只 `print` 到 Actions 日志。这是 R612 原则
  「程序在用≠ 人在看」的极端形态——而 R614 已确认本项目通知渠道 0 个，
  日志里的话**根本不会到达任何人**。探针在跑，但答案被丢弃。现在把结论写入
  metrics.jsonl，由 scripts/metrics_report.py 消费并在体检里成行。
- **L3 探针自身失效不可见**：`|| echo` 兜底让"探针挂了"与"检查通过"在日志里
  同貌。���在每轮都写一行 `provider_probe` 遥测（含 probe_error 字段），
  报表据此区分「检查通过」/「默认名已死」/「探针自己没跑成」。

只发 GET 列举请求，不做生成——不消耗额度、不产生费用。

用法: python scripts/probe_provider_models.py
"""
import ast
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

TIMEOUT = 20

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
MAIN_PY = os.path.join(_ROOT, "main.py")
METRICS_FILE = os.path.join(_ROOT, "metrics.jsonl")

# 目录需要 key 才能列的站点：把key 拼进 query 而不是 Authorization 头。
# 实测（2026-10-02）z.ai / siliconflow / stepfun 无 key 均返回 401。
# stepfun 走 /step_plan/v1 订阅端点，不能删前缀（否则静默落入按量计费通道），
# 故只在末尾追加 /models 走同一条订阅面。
QUERY_KEY_SITES = {
    "google": "GOOGLE_API_KEY",
    "zai": "ZAI_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "stepfun": "STEPFUN_API_KEY",
    "stepfun-flash": "STEPFUN_API_KEY",
}

# 目录 URL 拼接方式：绝大多数 OpenAI 兼容站是 {base}/models。
MODELS_PATH = "/models"


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Auto-Square-Publisher/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def extract_pool(main_path=MAIN_PY):
    """R617：从 main.py 的 extra_keys 静态提取 (站名, base_url, 默认模型名)。

    为什么用 AST 而不是 import main 或正则：
      - import main 会执行模块级代码（建目录/读配置/起线程），探针不该有副作用；
      - 正则匹配 `"name": (` 这类字面量，main.py 改个缩进或换成变量就静默漏站，
        而"漏站"恰恰是这套机制要防的失败模式；
      - AST 对语法错误/结构变化是**响亮失败**（抛异常），不会静默返回空列表。

    模型名表达式统一是 `os.getenv("X_MODEL", "").strip() or "默认名"`，
    故取 or 右侧的字符串常量；取不到就返回 None 交给上层标"需人工核对"，
    **不猜**（猜名=制造僵尸名，正是 R263 的成因）。
    """
    with open(main_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "extra_keys" for t in node.targets):
            continue
        dct = node.value
        if not isinstance(dct, ast.Dict):
            continue
        for k, v in zip(dct.keys, dct.values):
            if not (isinstance(k, ast.Constant) and isinstance(v, ast.Tuple)
                    and len(v.elts) == 3):
                continue
            site = str(k.value)
            url_node, model_node = v.elts[1], v.elts[2]
            base = url_node.value if isinstance(url_node, ast.Constant) else None
            default = None
            if isinstance(model_node, ast.BoolOp):  # `a or b`
                default = model_node.values[-1].value \
                    if isinstance(model_node.values[-1], ast.Constant) else None
            out.append({"site": site, "base": str(base) if base else None,
                        "default": default})
    return sorted(out, key=lambda x: x["site"])


def _model_ids(payload):
    """从目录响应里取模型 id 集合，兼容 OpenAI(data[]) 与 Google(models[]) 两种形状。"""
    ids = set()
    if isinstance(payload, dict):
        for m in (payload.get("data") or []):
            if isinstance(m, dict) and m.get("id") is not None:
                ids.add(str(m.get("id")))
        for m in (payload.get("models") or []):
            if isinstance(m, dict) and m.get("name") is not None:
                ids.add(str(m.get("name")).split("/")[-1])
    return ids


def check_site(entry):
    """核对单站默认名。返回 dict，必含 site / default / ok 三键。

    ok 的三态（**未知不等于通过**，与 R613 同一方向）：
      True  = 默认名在目录里
      False = 默认名不在目录里 → 僵尸名，需换
      None  = 本次无法判定（无 key / 目录不可达 / 配置无法静态解析）→ 未知，
              报表按"未核实"渲染，不与"通过"混同
    """
    site, base, default = entry["site"], entry["base"], entry["default"]
    if not base or not default:
        return {"site": site, "default": default, "ok": None,
                "note": "无法从 main.py 静态解析 base_url/默认名，需人工核对"}
    url = base.rstrip("/") + MODELS_PATH
    key_env = QUERY_KEY_SITES.get(site)
    if key_env:
        key = os.getenv(key_env, "").strip()
        if not key:
            return {"site": site, "default": default, "ok": None,
                    "note": f"未设 {key_env}，跳过（CI 里由 secret 注入）"}
        url = f"{url}?key={key}"
    payload = _get_json(url)
    ids = _model_ids(payload)
    out = {"site": site, "default": default, "ok": default in ids,
           "total": len(ids)}
    free = sorted(i for i in ids
                  if i.endswith(":free") or i.endswith("-free")
                  or i.endswith(":free") or "-free" in i.rsplit("/", 1)[-1]
                  or i == "openrouter/free")
    if free:
        out["free_models"] = free[:40]
        out["free_count"] = len(free)
    if site == "google":
        out["flash_models"] = sorted(i for i in ids if "flash" in i.lower())[:20]
    return out


def write_telemetry(results, metrics_file=METRICS_FILE, elapsed_sec=None):
    """R617：把探针结论写进 metrics.jsonl，供 metrics_report.py 消费。

    为什么必须落盘而不是 print：R614 实测通知渠道 0 个，Actions 日志不会到达
    任何人。print 出去的结论等于不存在（R612：程序在用≠ 人在看）。

    单行一条 `provider_probe` 记录，字段设计服从「缺失即 None」（原则 4）：
      - probe_ok      : 探针是否把所有站都判定完（存活+未核实 == 总数）
      - probe_error   : 探针级失败原因（网络/解析失败），无则不写
      - zombie_count / zombie_sites : 默认名**已确证**不在目录里的站，逗号分隔
      - unknown_sites : 逗号分隔的未核实站名（含无 key / 目录不可达）
      - sites_total / sites_ok / sites_unknown

    R618：zombie_* 与 unknown_* **必须是两组独立字段**。首版只有 unknown，
    读侧用 probe_ok 单判据渲染"探针未完成"，结果生产实锤的 aihubmix 僵尸名
    （同轮有 8 站缺 key 未核实）被整条吞掉——**已确证的事实不该被"另一批
    未知"稀释**，两者的处置动作也完全不同（换名 vs 补 key）。
    写侧只写不读，故此字段可缺失；读侧缺失按 0 渲染（见 metrics_report）。
    写失败只warn 不抛——探针是旁路组件，绝不能因遥测落盘失败带崩主发帖（原则 5）。
    """
    total = len(results)
    ok = sum(1 for r in results if r.get("ok") is True)
    unknown = [str(r.get("site")) for r in results if r.get("ok") is None]
    zombies = [str(r.get("site")) for r in results if r.get("ok") is False]
    rec = {
        "outcome": "provider_probe",
        "sites_total": total,
        "sites_ok": ok,
        "sites_unknown": len(unknown),
    }
    if elapsed_sec is not None:
        rec["probe_elapsed_sec"] = round(float(elapsed_sec), 2)
    if unknown:
        rec["unknown_sites"] = ",".join(unknown)
    if zombies:
        rec["zombie_count"] = len(zombies)
        rec["zombie_sites"] = ",".join(zombies)
    # probe_ok=False 表示"本轮结论不可信"（有站因异常未判定或池解析为空），
    # 与"全部通过"严格区分。
    rec["probe_ok"] = bool(total) and ok + len(unknown) == total
    if not rec["probe_ok"]:
        rec["probe_error"] = ("池解析为空" if not total
                              else f"{len(unknown)} 站未核实")
    try:
        bj = datetime.now(timezone(timedelta(hours=8)))
        base = {"ts": datetime.now(timezone.utc).isoformat(),
                "hour_bj": bj.hour, "weekday_bj": bj.weekday()}
        base.update({k: v for k, v in rec.items() if v is not None})
        with open(metrics_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(base, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 - 旁路组件不得阻塞主流程（原则 5）
        print(f"  ⚠️ 探针遥测落盘失败（不影响检查结论）: {type(e).__name__}: {e}")


def main():
    print("=== provider 池默认模型名存活核对（R263 僵尸名 / R617 全池覆盖）===")
    pool = extract_pool()
    print(f"从 main.py extra_keys 解析到 {len(pool)} 站："
          f"{', '.join(p['site'] for p in pool) or '—'}")
    results = []
    for entry in pool:
        try:
            results.append(check_site(entry))
        except urllib.error.HTTPError as e:
            results.append({"site": entry["site"], "default": entry["default"],
                            "ok": None,
                            "note": f"目录不可达 HTTP {e.code}"
                                    f"（401/403=key 问题，404=端点问题）"})
        except Exception as e:  # noqa: BLE001
            results.append({"site": entry["site"], "default": entry["default"],
                            "ok": None, "note": f"{type(e).__name__}: {e}"})

    for r in results:
        print()
        print(f"[{r['site']}] 默认模型: {r.get('default')}")
        if r.get("note"):
            print(f"  ⚠️ {r['note']}")
            continue
        print(f"  目录模型数: {r.get('total')}  "
              f"默认名存活: {'是' if r.get('ok') else '否 ← 僵尸名，需换'}")
        if r.get("free_models"):
            print(f"  当前免费模型（{r.get('free_count')} 个，列前 10）:")
            for m in r["free_models"][:10]:
                print(f"    {m}")
        if r.get("flash_models"):
            print("  当前 Flash 系可生成模型:")
            for m in r["flash_models"]:
                print(f"    {m}")

    write_telemetry(results)

    zombies = [str(r["site"]) for r in results if r.get("ok") is False]
    unknown = [str(r["site"]) for r in results if r.get("ok") is None]
    print()
    print(f"汇总: 共 {len(results)} 站 / 存活 "
          f"{sum(1 for r in results if r.get('ok') is True)} / 僵尸名 {len(zombies)}"
          f" / 未核实 {len(unknown)}")
    if zombies:
        print(f"  💀 僵尸名（需换 *_MODEL 或撤 preset）: {', '.join(zombies)}")
    if unknown:
        print(f"  ℹ️ 未核实（非通过）: {', '.join(unknown)}")
    print("  结论已写入 metrics.jsonl（outcome=provider_probe），"
          "体检报表会成行展示。")
    # 退出码恒0：僵尸名是**运营待办**而非探针故障，带崩主发帖没有好处
    # （workflow 亦有|| echo 兜底）。真正的信号走遥测，不走退出码。
    return 0


if __name__ == "__main__":
    sys.exit(main())
