#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方案 C · 在线能力层（OpenAI 兼容 /chat/completions），默认完全关闭。

## 这个模块「不做」什么，比「做」什么更重要

- **不自带任何密钥、地址、模型名**。密钥只从环境变量 `MUXIN_API_KEY` 或界面输入
  （仅内存，不落盘）取得；`configs/online.json` 只存非敏感项，且默认 `enabled=false`。
- **不主动联网**。`enabled=false` 或没密钥时，`chat()` 直接返回空串，一个包都不发。
  界面上的「测试连接」也只在用户亲手点击时才发起一次请求。
- **不接管确定性意图**。身份 / 时间 / 边界 / 记忆由 `app/router.py` 在本机处理，
  `engine.respond()` 命中规则时根本不会调 client。提醒事件没有命中规则，所以
  `CapabilityClient.chat()` 里对提醒类 kind 再加一道硬闸：**无论在线开没开都走本地**。
  真正会发出去的，只有未被规则接管的闲聊与多轮接话。
- **主动提醒与方案 C 无关**。随机提醒由 `app/scheduler.py` 在本机定时触发，
  不读本文件、不发请求；断网或只用本地时照常发。
- **不假装断网也正常**。超时、报错、返回空一律降级到本地 1.5B，并在状态栏写明。

## 熔断

在线超时设得短（默认 8 秒），连续失败 2 次就临时停用 60 秒，
避免断网时每说一句话都干等 8 秒。恢复后自动再试。

## 隐私

开启在线时，每次闲聊会发送：系统人设、当前这句、最近两轮对话、相关记忆块。
这些**会离开本机**。所以默认关闭，且界面在开启前必须把这句话给用户看。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app import persona

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "configs" / "online.json"
ENV_KEY = "MUXIN_API_KEY"

FAIL_STREAK_LIMIT = 2
COOLDOWN_SECONDS = 60

# 提醒是本机功能：即便在线已启用，也不把「该喝水了 / 该睡了」发去外网。
# 这样「不启用方案 C 能不能随机提醒」的答案是无条件能，且启用后行为不变。
LOCAL_ONLY_KINDS = frozenset(persona.REMIND_INSTRUCTIONS)


@dataclass
class OnlineConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    enabled: bool = False
    timeout_seconds: float = 8.0
    max_tokens: int = 96

    @classmethod
    def from_local(cls, **overrides) -> "OnlineConfig":
        """读本机配置 + 环境变量；配置缺失/损坏一律返回「关闭」。"""
        data = {}
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        cfg = cls(
            base_url=str(data.get("base_url") or ""),
            model=str(data.get("model") or ""),
            api_key=os.environ.get(ENV_KEY, ""),
            enabled=bool(data.get("enabled", False)),
            timeout_seconds=float(data.get("timeout_seconds") or 8),
            max_tokens=int(data.get("max_tokens") or 96),
        )
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def ready(self) -> bool:
        return bool(self.enabled and self.base_url and self.model and self.api_key)

    def describe(self) -> str:
        if not self.enabled:
            return "未启用"
        if not self.base_url or not self.model:
            return "缺地址或模型名"
        if not self.api_key:
            return "缺密钥"
        return "已配置"


def _clean(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^(小林|慕心|助手|AI)[:：]\s*", "", text)
    text = text.split("\n")[0].strip()
    if "  " in text:
        text = text.split("  ")[0].strip()
    return text[:120]


class OnlineClient:
    """只负责「向在线模型要一句话」；失败返回空串，由 CapabilityClient 决定降级。"""

    def __init__(self, config: OnlineConfig):
        self.config = config
        self.fail_streak = 0
        self.disabled_until = 0.0
        self.requests_sent = 0          # 供离线自测断言「没配置时一个包都不发」
        self.last_error = ""

    def _endpoint(self) -> str:
        base = (self.config.base_url or "").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def chat(self, user_text: str, kind: str = "chat", memory_block: str = "",
             seed: int | None = None,
             history: list[tuple[str, str]] | None = None) -> str:
        if not self.config.ready() or time.time() < self.disabled_until:
            return ""

        user_content = user_text
        if memory_block:
            user_content = f"{user_text}\n（背景：{memory_block}）"
        msgs = [{"role": "system", "content": persona.SYSTEM_PROMPT}]
        for u, b in (history or []):
            if u:
                msgs.append({"role": "user", "content": u})
            if b:
                msgs.append({"role": "assistant", "content": b})
        msgs.append({"role": "user", "content": user_content})

        payload = {
            "model": self.config.model,
            "messages": msgs,
            "max_tokens": self.config.max_tokens,
            "temperature": 0.7,
            "top_p": 0.9,
            "stream": False,
        }
        if seed is not None:
            payload["seed"] = seed
        req = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )
        self.requests_sent += 1
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as r:
                data = json.loads(r.read().decode("utf-8"))
            reply = _clean(data["choices"][0]["message"]["content"])
            if not reply:
                raise ValueError("在线返回空内容")
            self.fail_streak = 0
            self.disabled_until = 0.0
            self.last_error = ""
            return reply
        except Exception as e:                       # 超时/HTTP 错/解析错一律降级
            self.fail_streak += 1
            self.last_error = f"{type(e).__name__}: {e}"
            if self.fail_streak >= FAIL_STREAK_LIMIT:
                self.disabled_until = time.time() + COOLDOWN_SECONDS
            print(f"[online] 不可用，降级本地：{self.last_error}")
            return ""

    def close(self):
        pass


class CapabilityClient:
    """把「本地」与「在线」包成一个 `chat()`，让 engine / GUI / eval 完全无感。

    关键：**默认只有本地**。传进来的 online 未 ready 时，一次网络请求都不会有。
    """

    def __init__(self, local_client, online_client: OnlineClient | None = None,
                 on_status=None):
        self.local = local_client
        self.online = online_client
        self.on_status = on_status
        self.last_backend = "local"

    def _emit(self, text: str):
        if self.on_status:
            try:
                self.on_status(text)
            except Exception:
                pass

    def set_online(self, online_client: OnlineClient | None):
        self.online = online_client
        self._emit("在线已启用" if online_client and online_client.config.ready() else "只用本地")

    def chat(self, user_text: str, kind: str = "chat", memory_block: str = "",
             seed: int | None = None,
             history: list[tuple[str, str]] | None = None) -> str:
        if kind in LOCAL_ONLY_KINDS:
            # 硬闸：提醒永远本地。放在最前面，在线 ready 与否都不影响。
            self.last_backend = "local"
            return self.local.chat(
                user_text, kind=kind, memory_block=memory_block,
                seed=seed, history=history,
            )
        if self.online is not None and self.online.config.ready():
            reply = self.online.chat(
                user_text, kind=kind, memory_block=memory_block,
                seed=seed, history=history,
            )
            if reply:
                self.last_backend = "online"
                self._emit("在线")
                return reply
            self._emit("在线连不上 · 已用本地")
        self.last_backend = "local"
        return self.local.chat(
            user_text, kind=kind, memory_block=memory_block,
            seed=seed, history=history,
        )

    def close(self):
        closer = getattr(self.local, "close", None)
        if closer:
            closer()
