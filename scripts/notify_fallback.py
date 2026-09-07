#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
失败兜底通报器（last-resort notifier）。

触发场景：定时任务红了，但主脚本一次都没跑起来（checkout/pip 挂了）或中途崩了，
主流程自带的 Notifier 指望不上——要么依赖没装好（import main 直接炸），要么
进程已死。故本脚本只用标准库，不 import 仓库任何模块，纯 urllib 直调各渠道。

由 .github/workflows/auto_post.yml 末尾 `if: failure()` 步骤调用。
成功运行时不会执行；失败时尽力而为，单通道异常不影响其他通道。
"""
import json
import os
import sys
import urllib.request
from urllib.parse import quote


def _post(url, payload=None, timeout=10):
    """返回 (status, body_snippet)；网络异常直接抛给调用方归类"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(2000).decode("utf-8", "replace")


def _check(status, body, want=None):
    """HTTP 非 2xx 或业务码不对都算失败（转异常，统一走重试/日志口径）"""
    if status // 100 != 2:
        raise RuntimeError(f"HTTP {status}: {body[:120]}")
    if want is not None and want not in body:
        raise RuntimeError(f"业务码异常: {body[:120]}")


def send_fallback(title, message, env=None):
    """经所有已配置渠道各发一次；返回 {channel: bool}，绝不抛异常。"""
    env = env if env is not None else os.environ
    get = lambda k: (env.get(k, "") or "").strip()
    results = {}

    def _try(name, fn):
        try:
            fn()
            results[name] = True
        except Exception as e:
            results[name] = False
            print(f"兜底通报失败 [{name}]: {str(e)[:150]}")

    if (k := get("SERVERCHAN_KEY")):
        _try("Server酱", lambda _k=k: _check(*_post(
            f"https://sctapi.ftqq.com/{_k}.send",
            {"title": title, "desp": message}), '"code":0'))
    if (k := get("PUSHPLUS_TOKEN")):
        _try("PushPlus", lambda _k=k: _check(*_post(
            "http://www.pushplus.plus/send",
            {"token": _k, "title": title, "content": message,
             "template": "markdown"}), '"code":200'))
    if (k := get("BARK_KEY")):
        _try("Bark", lambda _k=k: _check(*_post(
            f"https://api.day.app/{_k}/{quote(title, safe='')}/{quote(message, safe='')}"),
            '"code":200'))
    if (bt := get("TELEGRAM_BOT_TOKEN")) and (cid := get("TELEGRAM_CHAT_ID")):
        _try("Telegram", lambda _bt=bt, _cid=cid: _check(*_post(
            f"https://api.telegram.org/bot{_bt}/sendMessage",
            {"chat_id": _cid, "text": f"{title}\n\n{message}"}), '"ok":true'))
    if (k := get("WEBHOOK_URL")):
        # 通用 Webhook 各家成功语义不一，只验 HTTP 2xx
        _try("Webhook", lambda _k=k: _check(*_post(
            _k, {"msgtype": "text",
                 "text": {"content": f"{title}\n\n{message}"}})))
    return results


def main(argv=None) -> int:
    env = os.environ
    server = (env.get("GITHUB_SERVER_URL", "") or "").strip() or "https://github.com"
    repo = (env.get("GITHUB_REPOSITORY", "") or "").strip() or "?"
    run_id = (env.get("GITHUB_RUN_ID", "") or "").strip() or "?"
    title = "【兜底】发帖主流程异常退出"
    message = (
        f"安装步骤: {env.get('INSTALL_RESULT', '?')} / "
        f"发帖步骤: {env.get('POSTER_RESULT', '?')} / "
        f"触发: {env.get('EVENT_NAME', '?')}\n"
        f"主脚本未产生任何状态变更；若连续出现请检查 Secrets/依赖/Runner。\n"
        f"运行日志: {server}/{repo}/actions/runs/{run_id}")
    results = send_fallback(title, message, env)
    done = sorted(k for k, v in results.items() if v)
    print(f"兜底通报完成: {', '.join(done) if done else '无可用渠道或全部失败'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
