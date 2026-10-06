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

# 目录需要 key 才能列的站点 → key env 名。
#
# R619修正：R617 只列了 4 站，且**一律用 `?key=` 拼query**。生产实跑暴露两个
# 问题——「已注入 key 却报未核实」的 7 站里有 5 站其实卡在这里：
#   - tokenrouter / b.ai：只认 `Authorization: Bearer <key>`，用 ?key= 拼
#     实测拿回 `{"code":30014,...,"message":"Token is invalid."}` ——认证
#     方式错了，不是 key 错了；
#   - stepfun：走 step_plan 订阅端点，同样吃 Bearer。
# 判据：**认证方式不能猜**，逐站实测过（见下方 AUTH_MODE 的注释）。
#
# 为什么不能对全池无脑发 key：多数站的/models 公开，附带凭据只是把 key 放进
# 一次外部请求；只有确认需要认证的站才带。
AUTH_MODE = {
    # 站名→ (key env 名, 认证方式)
    "google":("GOOGLE_API_KEY", "query"),   # ?key=
    "zai":        ("ZAI_API_KEY", "query"),        # ?key=
    "siliconflow":("SILICONFLOW_API_KEY", "query"),  # ?key= 实测可用
    "stepfun":("STEPFUN_API_KEY", "bearer"),  # Bearer（订阅端点）
    "stepfun-flash": ("STEPFUN_API_KEY", "bearer"),
    "tokenrouter":("TOKENROUTER_API_KEY", "bearer"),  # Bearer 实测
    # R626：b.ai 已弃用（无免费额度），main.py 的 extra_keys 删掉了该条目。
    # 探针的覆盖面从 extra_keys 的 AST 解析来，**不会**再遍历到这里——
    # 保留一条悬空配置等于给未来的读者一个"这站还在"的错误信号，故同步删除。
    # b.ai 吃 Bearer 这条实测结论保留在上面的注释里，恢复通道时直接用。
}

# 目录 URL 拼接方式：绝大多数 OpenAI 兼容站是 {base}/models。
MODELS_PATH = "/models"

# R626：**目录端点不等于 base_url** —— OpenAI 兼容的 chat 端点常常没有 /models。
#
# Google AI Studio 是实证案例：main.py 里的 base_url 是
#   https://generativelanguage.googleapis.com/v1beta/openai
# 它是 Gemini 的 **OpenAI 兼容层**（R615 选它正是为了零适配层），只提供
# chat/completions；而模型目录在**原生**端点
#   https://generativelanguage.googleapis.com/v1beta/models
# 探针按 `{base}/models` 拼接 → `.../v1beta/openai/models` → **HTTP 404**，
# 于是每轮都把 google 记进 unknown_sites，看起来像"key 配了但通道有问题"。
#
# **误报比不报更坏**（R619 纪律）：会把人引去重置一个没坏的 key。而
# 实际是探针自己的 URL 构造错了。**这不是 Google 的故障，是探针的盲区**，
# 且它已经连续 6 小时（18 轮探针）把这条通道标成未核实。
#
# 语义：key=**要查目录的 URL**（不是 base_url），缺省回落到 {base}/models。
MODELS_URL_OVERRIDE = {
    # OpenAI 兼容层无 /models，目录在原生 v1beta（认证仍走 ?key=，已实测）
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
}


def _get_json(url, bearer=None):
    headers = {"User-Agent": "Auto-Square-Publisher/1.0"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, headers=headers)
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
            # R695：取第0 个元素（key）里的 env **名**，供"有 key 却没登记认证
            # 方式"的判定用。⚠️ 必须从 AST 取而非手抄表——手抄会随 main.py
            # 改 env 名而静默过期（R693workflow env 名错配同族）。
            key_env = None
            key_node = v.elts[0]
            # 真实形状是 `os.getenv("X_API_KEY", "").strip()` ⇒ 外层是
            # **零参数** `.strip()` 调用（`args=[]`），内层才是带 env 名的 getenv。
            # ⚠️ 判据不能写 `and key_node.args`——`.strip()` 的 args 恒为空，
            #   该条件恒False ⇒ 剥壳被跳过 ⇒ 11 站全 None（我第一版的错）。
            if (isinstance(key_node, ast.Call) and isinstance(key_node.func, ast.Attribute)
                    and key_node.func.attr == "strip"):
                key_node = key_node.func.value   # 剥壳：取 .strip 的被调对象
            if (isinstance(key_node, ast.Call) and isinstance(key_node.func, ast.Attribute)
                    and key_node.func.attr == "getenv" and key_node.args
                    and isinstance(key_node.args[0], ast.Constant)):
                key_env = str(key_node.args[0].value)
            default = None
            if isinstance(model_node, ast.BoolOp):  # `a or b`
                default = model_node.values[-1].value \
                    if isinstance(model_node.values[-1], ast.Constant) else None
            out.append({"site": site, "base": str(base) if base else None,
                        "default": default, "key_env": key_env})
    return sorted(out, key=lambda x: x["site"])


# R695：站名 → key env 名（由 main.py 的 extra_keys 动态提取，不手抄）。
# 用途：区分「未登记 AUTH_MODE」里两种处置完全不同的情形——
#有 key（探针配置缺条目）vs 无 key（用户还没配）。
SITE_KEY_ENV = {e["site"]: e.get("key_env") for e in extract_pool()}


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


def _try_auth(url, key, mode):
    """按指定认证方式取目录，成功返回 payload，失败抛 HTTPError。

    单独抽出是为了让 check_site 能在 401 时**换一种方式重试**：R619 实测发现
    各站认证方式不统一（tokenrouter 吃 ?key= 也吃 Bearer，而 stepfun 走
    Bearer），只押一种方式会把"方式错了"误报成"key 失效"——两者处置完全不同。
    """
    if mode == "query":
        return _get_json(f"{url}?key={key}")
    return _get_json(url, bearer=key)


def _probe_auth_mode(url, key):
    """R695：**实测**某站 `/models` 认哪种认证方式，返回可照抄的结论串。

    为什么必须实测（R619 的同型，但更隐蔽）：`AUTH_MODE` 靠人肉逐站填写，
    填错时探针会走错误方式→ 401 → 报"未核实"，而排障会去看 key 失效、
    看网络，**真正的问题（方式填错）留在原地**。填对之前没人会发现填错。

    ⚠️ **判据不能用"哪种方式成功"**——目录可能公开，两种方式都成功，
    此时无法区分（结论 `both_ok_public`，需人工/换判据）。
    ⚠️ 也不能因为"未登记"就替站点填一个方式进 AUTH_MODE：
    那是把**猜测**写进配置，比留空更危险（错误的配置会持续给出错误结论）。
    ⇒ 本函数只**回报事实**，不写 AUTH_MODE。

    返回串格式（供人照抄进 AUTH_MODE）：
      bearer_ok / query_ok      —— 唯一被接受的方式（另一种被拒）
      both_ok_public            —— 两种都成功 = 目录公开，**无法判定**
      both_rejected:<a>,<b>     —— 两种都被拒 = key 可能失效或方式超出这两种
      error:<异常类名>          —— 网络/解析异常，本轮测不出
    """
    verdicts = {}
    for mode in ("bearer", "query"):
        try:
            _try_auth(url, key, mode)
            verdicts[mode] = True
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                verdicts[mode] = False      # 明确被拒 = 该方式不被接受
            else:
                verdicts[mode] = None# 其它错误与认证无关，不算判据
        except Exception:
            verdicts[mode] = None
    b, q = verdicts.get("bearer"), verdicts.get("query")
    if b is True and q is False:
        return "bearer_ok"
    if q is True and b is False:
        return "query_ok"
    if b is True and q is True:
        return "both_ok_public"
    if b is False and q is False:
        return "both_rejected:bearer,query"
    return "error:inconclusive"


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
                "note": "无法从 main.py静态解析 base_url/默认名，需人工核对"}
    # R626：目录 URL 优先取站级override（OpenAI 兼容层的 /models 不存在），
    # 缺省才按 {base}/models 拼。**不能只看 base_url 里有 openai 就跳过——
    # 是否有 /models 只有试过才知道，而试错的代价是每轮一条假"未核实"。**
    url = MODELS_URL_OVERRIDE.get(site) or (base.rstrip("/") + MODELS_PATH)
    auth = AUTH_MODE.get(site)
    if not auth:
        # R695：**不猜认证方式**（R619：stepfun/tokenrouter都曾因猜错而把
        # "方式错了"报成"key 坏了"）。对"有 key 却未登记"的站，实测两种方式
        # 并把结论写进 note——让下一个人**照抄即可**，不必重测。
        key_env = SITE_KEY_ENV.get(site) or ""
        key = os.getenv(key_env, "").strip() if key_env else ""
        payload = _get_json(url)
        out = _judge(site, default, payload)
        if not key:
            # 连凭据都没有 ⇒ 实测不了，note 说清"为什么没实测"
            out["auth_probe"] = (f"no_key:{key_env}" if key_env
                                 else "no_key_env_name")
            return out
        out["auth_probe"] = _probe_auth_mode(url, key)
        return out

    key_env, mode = auth
    key = os.getenv(key_env, "").strip()
    if not key:
        # R619：区分"配置漏注入"与"本机无凭据"。CI 里若真的漏注入，
        # 这句会被运维看到并去补 secret；笼统说"跳过"会让人以为正常。
        return {"site": site, "default": default, "ok": None,
                "note": f"未设 {key_env}（若在 CI 里出现=workflow 漏注入 secret）"}
    try:
        payload = _try_auth(url, key, mode)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            raise
        # R619：认证被拒时换另一种方式再试一次，两种都不行才判"未核实"。
        # 不这样做就会把"我猜错了认证方式"报成"你的 key 坏了"——后者会把人
        # 引去重置一个其实没坏的 key，真正的问题（方式）却留在原地。
        alt = "bearer" if mode == "query" else "query"
        try:
            payload = _try_auth(url, key, alt)
        except urllib.error.HTTPError as e2:
            raise
        except Exception:
            raise e
        out = _judge(site, default, payload)
        out["note"] = (f"以 {mode} 方式被拒(HTTP {e.code})，换 {alt} 成功"
                       f"——AUTH_MODE 已自动纠正")
        return out
    except urllib.error.HTTPError:
        raise
    except Exception:
        raise
    out = _judge(site, default, payload)
    out["note"] = f"以 {mode} 方式成功"
    return out


def _judge(site, default, payload):
    """比对默认名是否在目录里，产出 ok 三态 + 目录规模信息。

    与认证完全解耦（R619）：check_site 负责"怎么拿到 payload"，这里只负责
    "拿到之后怎么判"。这样认证降级重试（换方式再试）不必重复判定逻辑。
    """
    ids = _model_ids(payload)
    free = sorted(i for i in ids
                  if i.endswith(":free") or i.endswith("-free")
                  or "-free" in i.rsplit("/", 1)[-1]
                  or i == "openrouter/free")
    out = {"site": site, "default": default, "ok": default in ids,
           "total": len(ids)}
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
    # R637：**未登记 AUTH_MODE 的站要单独暴露**——它们的"核实通过"依赖
    # `/models` 恰好公开，而这个前提**不在代码里、也不会出现在任何输出中**。
    #
    # 生产实锤：openrouter / xkiro / aihubmix / inferera / bluesminds 五站
    # 未登记 AUTH_MODE ⇒ 走「无认证直查」。其中 **xkiro / aihubmix / inferera
    # 在主流程是需要 key 的**（`gh secret list` 无这三个 secret，生产从未
    # 上场），它们"核实通过"纯粹因为目录公开可查。
    #
    # 危害：若哪天这些站的 /models 改为需认证，探针会集体报"未核实"，
    # 而**原因（没登记 AUTH_MODE）不在任何字段里** ⇒ 排障会去查 key 失效、
    # 查网络，**真正的问题（探针配置缺条目）留在原地**。R619 的同型：
    # "方式错了"被报成"key 坏了"。这里更隐蔽——**连note 都不会有**。
    #
    # ── R695（2026-10-06）：把「未登记」拆成两类，因为处置动作完全不同 ──
    # 生产实锤：`no_auth_count=5` 已**恒定 3 天 242 次不变**，而其中
    # **openrouter 是唯一「有 secret + 主流程真实在用」的站**
    # （`gh secret list` 有 OPENROUTER_API_KEY，且 `extra_keys` 里是主力条目）。
    # ⇒ 它和另外 4 站（xkiro/aihubmix/inferera/bluesminds，**连 key 都没有**）
    # 被混在同一个 `no_auth_sites` 里报出来。
    #
    # ⚠️ **恒定不变的告警等于噪声**：3 天没人动、没人查，因为看不出该做什么。
    # 而 openrouter 真出问题时，运维看到"5 站未登记"会先怀疑另外 4 个不相关的站，
    # 排障方向被稀释（R620 的"排序键恒等"同型：字段在，但答不了问题）。
    #
    # 拆分口径（**按「有没有拿到 key」分，不按「在不在池里」分**）：
    #   auth_gap_sites：池内 + **本机有 key** + 未登记 ⇒ 探针配置缺条目。
    #       排障方向＝补 AUTH_MODE；**这是代码问题，不是用户该做的事**。
    #   nokey_sites：池内 + **本机无 key** ⇒ 用户还没配。
    #       排障方向＝配 secret；登记 AUTH_MODE 对它**无意义**（拿不到凭据）。
    #
    # ⚠️ 为什么不直接给 openrouter 填上bearer：R619 的教训是
    # **"认证方式不能猜"**（stepfun/tokenrouter 都曾因猜错而误报 key 坏了）。
    # 本地无 OPENROUTER_API_KEY ⇒ **实测不了** ⇒ 猜一个值填进去正是
    # 重犯 R619。故改为**实测并回报**（见 probe_site 的 auth_probe），
    # 由真实响应决定，而非我拍。
    _auth_gap, _nokey = [], []
    for r in results:
        _site = str(r.get("site"))
        if _site in AUTH_MODE:
            continue
        # 从 main.py 池条目取该站的 key env 名（R695：判定要基于真实配置）
        _key_env = SITE_KEY_ENV.get(_site, "")
        ( _auth_gap if (os.getenv(_key_env, "").strip() if _key_env else "")
          else _nokey ).append(_site)
    if _auth_gap:
        rec["auth_gap_count"] = len(_auth_gap)
        rec["auth_gap_sites"] = ",".join(_auth_gap)
    if _nokey:
        rec["nokey_count"] = len(_nokey)
        rec["nokey_sites"] = ",".join(_nokey)
    # R695：认证方式实测结论落盘。⚠️ **不落等于没做**——实测只存在内存里
    # 的话，下一个人还得重测一遍（R612：程序在用 ≠ 人在看，同款理由）。
    # 格式 `site=结论` 逗号分隔，空结论（无 key / 非未登记站）不占位。
    _probes = ["%s=%s" % (r.get("site"), r["auth_probe"])
               for r in results if r.get("auth_probe")]
    if _probes:
        rec["auth_probe_results"] = ",".join(_probes)
    # 保留旧字段（读侧/报表仍在用，删了会破历史序列）
    _noauth = [str(r.get("site")) for r in results
               if str(r.get("site")) not in AUTH_MODE]
    if _noauth:
        rec["no_auth_count"] = len(_noauth)
        rec["no_auth_sites"] = ",".join(_noauth)
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
        _ok = r.get("ok")
        if _ok is None:
            # 未核实：未知≠ 通过，图标必须与"通过"区分
            print(f"  ⚠️ 未核实：{r.get('note') or '原因未记录'}")
            continue
        print(f"  目录模型数: {r.get('total')}  默认名存活: {'是' if _ok else '否 ← 僵尸名，需换'}")
        # R619：note 现在也可能承载"认证方式降级成功"这类**正常信息**，
        # 不能一律当警告打——那会把一次成功的核对渲染成告警，训练人忽略 ⚠️。
        if r.get("note"):
            print(f"  ℹ️ {r['note']}")
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
