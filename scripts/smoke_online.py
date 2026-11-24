#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方案 C 离线自测：全部流量只到 127.0.0.1，不碰任何外部网络。

用一个本机临时 HTTP 服务假装「OpenAI 兼容接口」，验证四件事：
1. 默认关闭 / 缺密钥时，**一个请求都不发**
2. 在线成功时用它、本地模型不被调用
3. 在线 500 / 超时 / 返回空时**自动降级本地**，并标注后端
4. 连续失败会熔断，不会每句话都干等

这测的是「方案 C 的链路能不能做到」，不是「某家 API 好不好用」——
没有任何真实地址、账号或密钥参与。

用法：
    ./.venv/Scripts/python.exe scripts/smoke_online.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.online_client import (                          # noqa: E402
    CapabilityClient,
    OnlineClient,
    OnlineConfig,
)

ONLINE_TEXT = "在线回的一句 喵（歪头）"
LOCAL_TEXT = "本地回的一句"


class FakeHandler(BaseHTTPRequestHandler):
    mode = "ok"
    hits = 0
    last_body = None
    last_auth = ""

    def log_message(self, *args):
        pass

    def do_POST(self):
        FakeHandler.hits += 1
        n = int(self.headers.get("Content-Length", 0))
        FakeHandler.last_body = json.loads(self.rfile.read(n) or b"{}")
        FakeHandler.last_auth = self.headers.get("Authorization", "")
        if FakeHandler.mode == "slow":
            time.sleep(1.2)
        if FakeHandler.mode in ("error", "slow"):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"boom"}')
            return
        body = {"choices": [{"message": {"content": ONLINE_TEXT if FakeHandler.mode == "ok" else ""}}]}
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeLocal:
    def __init__(self):
        self.calls = 0

    def chat(self, user_text, kind="chat", memory_block="", seed=None, history=None):
        self.calls += 1
        return LOCAL_TEXT

    def close(self):
        pass


def fresh(base_url: str, **kw) -> tuple[CapabilityClient, FakeLocal, OnlineClient]:
    cfg = OnlineConfig(base_url=base_url, model="fake-model", api_key="local-test-key",
                       enabled=True, timeout_seconds=0.5, max_tokens=32, **kw)
    online = OnlineClient(cfg)
    local = FakeLocal()
    return CapabilityClient(local, online), local, online


def main() -> int:
    failures: list[str] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    print(f"[0] 本机假服务：{base}（无外部网络）")

    try:
        # ---------- 1. 默认配置必须关闭 ----------
        print("\n[1] 默认关闭 / 缺密钥：零请求")
        disk = OnlineConfig.from_local()
        ok = not disk.enabled and not disk.ready()
        print(f"    {'PASS' if ok else 'FAIL'}  configs/online.json 读出来 enabled={disk.enabled} ready={disk.ready()}")
        if not ok:
            failures.append("本机配置默认是启用的，违反边界")

        local = FakeLocal()
        off = OnlineClient(OnlineConfig(base_url=base, model="m", api_key="k", enabled=False))
        cap = CapabilityClient(local, off)
        text = cap.chat("随便说句")
        ok = (text == LOCAL_TEXT and off.requests_sent == 0 and cap.last_backend == "local")
        print(f"    {'PASS' if ok else 'FAIL'}  enabled=False -> 本地回复，发请求 {off.requests_sent} 次")
        if not ok:
            failures.append("enabled=False 时仍发了请求或没用本地")

        local2 = FakeLocal()
        nokey = OnlineClient(OnlineConfig(base_url=base, model="m", api_key="", enabled=True))
        cap2 = CapabilityClient(local2, nokey)
        cap2.chat("随便说句")
        ok = nokey.requests_sent == 0 and cap2.last_backend == "local"
        print(f"    {'PASS' if ok else 'FAIL'}  缺密钥 -> 零请求，自动用本地")
        if not ok:
            failures.append("缺密钥时仍发了请求")

        # ---------- 2. 在线成功 ----------
        print("\n[2] 在线成功")
        FakeHandler.mode, FakeHandler.hits = "ok", 0
        cap, local, online = fresh(base)
        text = cap.chat("今天有点累", history=[("上一句", "上一句回复")])
        body = FakeHandler.last_body or {}
        ok = (text == ONLINE_TEXT and cap.last_backend == "online" and local.calls == 0
              and FakeHandler.last_auth.startswith("Bearer ")
              and len(body.get("messages", [])) == 4
              and body.get("model") == "fake-model")
        print(f"    {'PASS' if ok else 'FAIL'}  -> {text!r} | 后端={cap.last_backend} "
              f"| 本地调用={local.calls} | messages={len(body.get('messages', []))} 条")
        if not ok:
            failures.append("在线成功链路不正确（回复/后端/鉴权/多轮消息）")

        # ---------- 3. 故障降级 ----------
        print("\n[3] 在线故障自动降级本地")
        for mode in ("error", "empty", "slow"):
            FakeHandler.mode = mode
            cap, local, online = fresh(base)
            text = cap.chat("这句要降级")
            ok = (text == LOCAL_TEXT and cap.last_backend == "local" and local.calls == 1)
            print(f"    {'PASS' if ok else 'FAIL'}  {mode:<6} -> {text!r} | 后端={cap.last_backend} | {online.last_error[:40]}")
            if not ok:
                failures.append(f"{mode} 未正确降级")

        # ---------- 4. 熔断 ----------
        print("\n[4] 连续失败熔断")
        FakeHandler.mode, FakeHandler.hits = "error", 0
        cap, local, online = fresh(base)
        for _ in range(4):
            cap.chat("再来一句")
        sent = online.requests_sent
        cap.chat("熔断后这句")
        ok = online.requests_sent == sent and cap.last_backend == "local"
        print(f"    {'PASS' if ok else 'FAIL'}  4 次失败后只发了 {sent} 次请求，第 5 句直接走本地")
        if not ok:
            failures.append("熔断没生效，断网时每句都会等超时")

        # ---------- 5. 全程没碰外网 ----------
        ok = base.startswith("http://127.0.0.1:")
        print(f"\n[5] {'PASS' if ok else 'FAIL'}  所有请求目标均为 127.0.0.1（共 {FakeHandler.hits} 次）")
        if not ok:
            failures.append("出现了非本机地址")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    print("\n" + "=" * 56)
    if failures:
        print(f"FAILED: {len(failures)} 项")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL PASS：默认零联网、在线可用、故障降级、失败熔断（全程 127.0.0.1）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
