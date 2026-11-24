#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GGUF 后端：用 llama.cpp 的 llama-server 跑本地模型。

为什么要单独做一个后端：
- 发给别人时不能带 torch（光运行时就是 3 GB+），而 llama.cpp 跑 Q4 GGUF
  只要 1 GB 模型 + 30 MB 运行时，普通笔记本 CPU 上也能到 10-20 字/秒
- 接口与 LLMClient.chat 完全一致（chat(user_text, kind, memory_block)），
  GUI 不用改业务逻辑，只换实现

进程模型：GUI 启动时拉起一次 llama-server（localhost:端口），之后每轮对话走
HTTP，避免每句话都重新加载模型。退出时由 close() 收掉。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from app import persona

# 打包后（PyInstaller onedir）：exe 同级目录
# 开发时：项目根 tools/llama-cpu/
_APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


def _clean(text: str) -> str:
    """与 app/llm_client.py 的 _clean 保持一致，两侧改一处要同步。"""
    text = (text or "").strip()
    text = re.sub(r"^(小林|慕心|助手|AI)[:：]\s*", "", text)
    text = text.split("\n")[0].strip()
    if "  " in text:
        text = text.split("  ")[0].strip()
    return text[:120]


class GGUFClient:
    def __init__(
        self,
        model_path: str,
        exe_path: str | None = None,
        port: int = 18080,
        n_ctx: int = 4096,
        n_threads: int | None = None,
        n_gpu_layers: int = 0,
        mock: bool = False,
        startup_timeout: int = 180,
    ):
        self.mock = mock
        self.model_path = str(model_path)
        self.port = port
        self.proc = None
        self._log = None

        if mock:
            return

        exe = Path(exe_path) if exe_path else self._find_exe()
        if not exe.exists():
            raise FileNotFoundError(f"找不到 llama-server: {exe}")
        model = Path(self.model_path)
        if not model.exists():
            raise FileNotFoundError(f"找不到模型文件: {model}")

        threads = n_threads or max(2, (os.cpu_count() or 4) - 2)
        log_path = Path(tempfile.gettempdir()) / "muxin_llama_server.log"
        self._log = open(log_path, "w", encoding="utf-8")
        print(f"[gguf] 启动 llama-server: {exe}")
        print(f"[gguf] 模型: {model} 线程: {threads}")

        cmd = [
            str(exe),
            "-m", str(model),
            "-c", str(n_ctx),
            "-t", str(threads),
            "-ngl", str(n_gpu_layers),
            "--host", "127.0.0.1",
            "--port", str(port),
        ]
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            cmd, stdout=self._log, stderr=self._log, creationflags=flags,
            cwd=str(exe.parent),
        )
        self._wait_ready(startup_timeout)

    # ---------------- 内部 ----------------
    def _find_exe(self) -> Path:
        candidates = [
            _APP_DIR / "runtime" / "llama-server.exe",
            _APP_DIR / "tools" / "llama-cpu" / "llama-server.exe",
            _APP_DIR.parent / "tools" / "llama-cpu" / "llama-server.exe",
        ]
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]

    def _wait_ready(self, timeout: int):
        url = f"http://127.0.0.1:{self.port}/health"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"llama-server 启动后立即退出（码 {self.proc.returncode}），看日志")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200 and b"ok" in r.read().lower():
                        print("[gguf] 服务就绪")
                        return
            except Exception:
                pass
            time.sleep(0.6)
        raise TimeoutError(f"llama-server 在 {timeout}s 内没就绪")

    # ---------------- 对外 ----------------
    def chat(self, user_text: str, kind: str = "chat", memory_block: str = "",
             seed: int | None = None,
             history: list[tuple[str, str]] | None = None) -> str:
        if self.mock:
            return persona.fallback_reply(kind, user_text)

        # 与训练样本同构：背景拼在 user 消息，不塞 system。
        # B 阶段：整个链路只在这里拼一次（A-2 的提醒路径曾拼两次）。
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
            "messages": msgs,
            "max_tokens": 96,
            "temperature": 0.7,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "stream": False,
        }
        # llama.cpp 的 OpenAI 兼容接口支持 seed；固定后同题可复现
        if seed is not None:
            payload["seed"] = seed
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8"))
            return _clean(data["choices"][0]["message"]["content"])
        except Exception as e:
            print(f"[gguf] 请求失败: {e}")
            return ""

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self._log:
            try:
                self._log.close()
            except Exception:
                pass
