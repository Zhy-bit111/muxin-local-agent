#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""慕心 · 桌面陪伴助手（图形界面）

设计原则沿用整个项目的主线：
- 时间与状态归程序：ReminderScheduler 负责"什么时候提醒"
- 语言表达归模型：LLMClient 负责"提醒的话怎么说"
- 内容可靠性归程序：ensure_remind_output 兜住"说了但没说到点上"的情况

用法：
    python app/muxin_gui.py
    python app/muxin_gui.py --model output/merged-qwen25-1.5b --no-adapter
    python app/muxin_gui.py --adapter output/lora-v2
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm_client import LLMClient                    # noqa: E402
from app.persona import (                               # noqa: E402
    FALLBACK,
    REMIND_INSTRUCTIONS,
    build_user_text,
    ensure_remind_output,
    fallback_reply,
)
from app.scheduler import ReminderScheduler             # noqa: E402

# ---------------- 配色（深色暖调，避免刺眼） ----------------
BG = "#121317"
HEADER_BG = "#1A1C22"
CHAT_BG = "#121317"
USER_BG = "#28303F"
BOT_BG = "#3A3127"
ACCENT = "#E9B458"
TEXT_MAIN = "#E9EAEE"
TEXT_DIM = "#868C99"
INPUT_BG = "#1E2028"
BORDER = "#2A2D36"

FONT_UI = "Microsoft YaHei UI"
FONT_UI_FALLBACK = "Microsoft YaHei"

AVATAR_TEXT = "慕"


class MuxinApp(tk.Tk):
    def __init__(self, model_path: str, adapter_path: str | None, mock: bool = False,
                 auto_say: str | None = None):
        super().__init__()
        self.model_path = model_path
        self.adapter_path = adapter_path
        self.mock = mock
        self.auto_say = auto_say

        self.client: LLMClient | None = None
        self.q: queue.Queue = queue.Queue()
        self.ready = False
        self.busy = False
        self.remind_on = True
        self.scheduler: ReminderScheduler | None = None

        self._setup_window()
        self._build_ui()
        self._start_scheduler()

        threading.Thread(target=self._load_model, daemon=True).start()
        self.after(120, self._poll_queue)

    # ---------------- 窗口 ----------------
    def _setup_window(self):
        self.title("慕心")
        self.configure(bg=BG)
        ico = ROOT / "assets" / "muxin.ico"
        if ico.exists():
            try:
                self.iconbitmap(default=str(ico))
            except Exception:
                pass
        # 声明 DPI 感知后 Tk 用物理像素：尺寸要按缩放比例换算，否则窗口偏小
        # 注意不能用 self._w / self._h —— Tk 内部用 _w 存窗口路径名，覆盖会崩
        self._dpi_scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        self._win_w = int(430 * self._dpi_scale)
        self._win_h = min(int(660 * self._dpi_scale), self.winfo_screenheight() - 80)
        self.geometry(f"{self._win_w}x{self._win_h}")
        self.minsize(int(380 * self._dpi_scale), int(480 * self._dpi_scale))
        self.after(80, self._center_window)

    def _center_window(self):
        """窗口 map 之后定位。自动居中在这台机器上会被窗口管理器忽略
        （实测窗口跑到屏幕右侧被裁），改成固定落在左上角，用户可自行拖动。"""
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x, y = 18, 48
        if x + self._win_w > sw:
            x = max(0, sw - self._win_w - 8)
        if y + self._win_h > sh:
            y = max(0, sh - self._win_h - 8)
        self.geometry(f"{self._win_w}x{self._win_h}+{x}+{y}")
        try:
            self.iconbitmap(default="")
        except Exception:
            pass

    def _build_ui(self):
        font_ui = FONT_UI if FONT_UI in tkfont.families() else FONT_UI_FALLBACK
        # 像素值统一按 DPI 缩放换算：字体是 pt 会自动放大，硬编码的像素不会
        def px(v):
            return int(round(v * getattr(self, "_dpi_scale", 1.0)))

        # ---- 顶部 ----
        header = tk.Frame(self, bg=HEADER_BG, height=px(76))
        header.pack(side="top", fill="x")
        header.pack_propagate(False)

        av = px(42)
        self.avatar = tk.Canvas(
            header, width=av, height=av, bg=HEADER_BG, highlightthickness=0
        )
        self.avatar.pack(side="left", padx=(px(18), px(10)), pady=px(17))
        self.avatar.create_oval(1, 1, av - 1, av - 1, fill=ACCENT, outline="")
        self.avatar.create_text(
            av // 2, av // 2, text=AVATAR_TEXT, fill="#1A1C22",
            font=(font_ui, 15, "bold"),
        )

        name_box = tk.Frame(header, bg=HEADER_BG)
        name_box.pack(side="left", pady=(px(15), 0))
        tk.Label(
            name_box, text="慕心", bg=HEADER_BG, fg=TEXT_MAIN,
            font=(font_ui, 15, "bold"), anchor="w",
        ).pack(anchor="w")
        self.sub_label = tk.Label(
            name_box, text="正在醒来…", bg=HEADER_BG, fg=TEXT_DIM,
            font=(font_ui, 9), anchor="w",
        )
        self.sub_label.pack(anchor="w", pady=(1, 0))

        self.remind_btn = tk.Button(
            header, text="提醒 开", command=self._toggle_remind,
            bg=HEADER_BG, fg=ACCENT, activebackground=HEADER_BG,
            activeforeground=TEXT_MAIN, bd=0, relief="flat",
            font=(font_ui, 9), cursor="hand2", padx=px(10),
        )
        self.remind_btn.pack(side="right", padx=(0, px(14)))

        # ---- 分隔线 ----
        tk.Frame(self, bg=BORDER, height=1).pack(side="top", fill="x")

        # ---- 对话区 ----
        chat_box = tk.Frame(self, bg=CHAT_BG)
        chat_box.pack(side="top", fill="both", expand=True)

        self.scroll = tk.Scrollbar(chat_box, bg=CHAT_BG, troughcolor=CHAT_BG,
                                   bd=0, width=px(8), activebackground=BORDER)
        self.scroll.pack(side="right", fill="y")

        self.chat = tk.Text(
            chat_box, bg=CHAT_BG, fg=TEXT_MAIN, bd=0, relief="flat",
            wrap="char", padx=px(14), pady=px(10), cursor="arrow",
            font=(font_ui, 10), insertbackground=ACCENT,
            yscrollcommand=self.scroll.set,
            spacing1=px(2), spacing2=px(4), spacing3=px(2),
        )
        self.chat.pack(side="left", fill="both", expand=True)
        self.scroll.config(command=self.chat.yview)
        self.chat.configure(state="disabled")

        # wrap 必须是 char：中文没有空格，用 word 会把整句当成一个词，超出宽度直接裁掉
        self.chat.tag_configure(
            "bot", background=BOT_BG, foreground=TEXT_MAIN,
            justify="left", lmargin1=px(6), lmargin2=px(6), rmargin=px(76),
            spacing1=px(8), spacing3=px(8),
        )
        self.chat.tag_configure(
            "user", background=USER_BG, foreground=TEXT_MAIN,
            justify="right", lmargin1=px(76), lmargin2=px(76), rmargin=px(6),
            spacing1=px(8), spacing3=px(8),
        )
        self.chat.tag_configure(
            "hint", foreground=TEXT_DIM, justify="center",
            font=(font_ui, 9), spacing1=px(12), spacing3=px(12),
        )

        # ---- 状态行 ----
        tk.Frame(self, bg=BORDER, height=1).pack(side="top", fill="x")
        self.status = tk.Label(
            self, text="模型加载中…", bg=BG, fg=TEXT_DIM,
            font=(font_ui, 9), anchor="w", padx=px(16), pady=px(4),
        )
        self.status.pack(side="top", fill="x")

        # ---- 输入区 ----
        bottom = tk.Frame(self, bg=INPUT_BG, height=px(58))
        bottom.pack(side="bottom", fill="x")
        bottom.pack_propagate(False)

        self.entry = tk.Entry(
            bottom, bg=INPUT_BG, fg=TEXT_MAIN, insertbackground=ACCENT,
            bd=0, relief="flat", font=(font_ui, 10),
        )
        self.entry.pack(side="left", fill="both", expand=True,
                        padx=(px(16), px(8)), pady=px(12))
        self.entry.bind("<Return>", lambda e: self._on_send())

        self.send_btn = tk.Button(
            bottom, text="发送", command=self._on_send,
            bg=ACCENT, fg="#16171B", activebackground="#F0C271",
            activeforeground="#16171B", bd=0, relief="flat",
            font=(font_ui, 10, "bold"), cursor="hand2",
            padx=px(18), pady=px(6),
        )
        self.send_btn.pack(side="right", padx=(0, px(14)))

        self.entry.focus_set()

        # pack 按调用顺序切片：先 pack 的 expand 控件会把后 pack 的底部控件
        # 挤出可视区（实测：输入框整块消失）。这里把对话区重新 pack 到最后。
        chat_box.pack_forget()
        chat_box.pack(side="top", fill="both", expand=True)

    # ---------------- 调度器 ----------------
    def _start_scheduler(self):
        state_path = ROOT / "data" / "state.json"
        self.scheduler = ReminderScheduler(
            str(state_path), water_interval_min=60, sleep_time="23:30"
        )
        self.scheduler.start(self._on_remind)

    def _on_remind(self, kind: str, instruction: str, context: str):
        # 调度线程里不能碰 Tk，全部丢进队列
        self.q.put(("remind", (kind, instruction, context)))

    def _toggle_remind(self):
        if not self.scheduler:
            return
        self.remind_on = not self.remind_on
        if self.remind_on:
            self.scheduler.resume()
            self.remind_btn.config(text="提醒 开", fg=ACCENT)
            self._append_hint("提醒已打开")
        else:
            self.scheduler.pause()
            self.remind_btn.config(text="提醒 关", fg=TEXT_DIM)
            self._append_hint("提醒已暂停")

    # ---------------- 模型 ----------------
    def _load_model(self):
        try:
            self.client = LLMClient(
                self.model_path, adapter_path=self.adapter_path, mock=self.mock
            )
            self.q.put(("ready", None))
        except Exception as e:  # 加载失败也要把界面留住
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def _gen_worker(self, user_text: str, kind: str):
        reply = ""
        try:
            reply = self.client.chat(user_text, kind=kind) if self.client else ""
        except Exception as e:
            print(f"[gen] 生成失败: {e}")
        if kind in REMIND_INSTRUCTIONS and kind not in ("chat", "boundary"):
            reply = ensure_remind_output(kind, reply)
        elif not reply:
            reply = fallback_reply(kind, user_text)
        self.q.put(("reply", (kind, reply, user_text)))

    # ---------------- 交互 ----------------
    def _on_send(self):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self._append_user(text)
        if not self.ready or not self.client:
            self._append_hint("我还在启动，稍等一下")
            return
        if self.busy:
            self._append_hint("上一条还没说完，等我一下")
            return
        self.busy = True
        self.status.config(text="慕心正在想…")
        threading.Thread(target=self._gen_worker, args=(text, "chat"), daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ready":
                    self.ready = True
                    self.sub_label.config(text="陪伴中 · 提醒已开")
                    self.status.config(text=self._status_text())
                    self._append_bot("我醒了，老周。要不要先喝口水？")
                    if self.auto_say:
                        self.after(400, self._auto_send)
                elif kind == "error":
                    self.status.config(text=f"模型加载失败：{payload}")
                    self._append_hint("模型没加载起来，先看看 output 里的报错")
                elif kind == "reply":
                    _, reply, _ = payload
                    self.busy = False
                    self._append_bot(reply)
                    self.status.config(text=self._status_text())
                    if self.auto_say:
                        (ROOT / "output" / "_gui_chat.txt").write_text(
                            f"用户：{self.auto_say}\n慕心：{reply}\n", encoding="utf-8"
                        )
                        self.after(20000, self.destroy)   # 留时间给外部截图
                elif kind == "remind":
                    rkind, instr, ctx = payload
                    self.busy = True
                    self.status.config(text="慕心正在想…")
                    user_text = build_user_text(instr, ctx)
                    threading.Thread(
                        target=self._gen_worker, args=(user_text, rkind), daemon=True
                    ).start()
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)

    # ---------------- 渲染 ----------------
    def _append_user(self, text: str):
        self.chat.configure(state="normal")
        self.chat.insert("end", "\n")          # 每条消息独立成段，否则会接在上一条同一行
        self.chat.insert("end", text, "user")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _append_bot(self, text: str):
        self.chat.configure(state="normal")
        self.chat.insert("end", "\n")
        self.chat.insert("end", text, "bot")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _append_hint(self, text: str):
        self.chat.configure(state="normal")
        self.chat.insert("end", "\n")
        self.chat.insert("end", text, "hint")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _status_text(self) -> str:
        if not self.scheduler:
            return "就绪"
        try:
            s = self.scheduler.status().splitlines()
            water = next((x for x in s if "下次喝水" in x), "").strip()
            return f"{water}    ·    提醒 {'开' if self.remind_on else '关'}"
        except Exception:
            return "就绪"

    def _auto_send(self):
        self.entry.insert(0, self.auto_say or "")
        self._on_send()

    def on_close(self):
        if self.scheduler:
            self.scheduler.stop()
        self.destroy()

    def selftest_dump(self):
        """把每个子控件的实际位置尺寸写出来，用于无头验证布局。"""
        out = ROOT / "output" / "_layout.txt"
        lines = [
            f"window: {self.winfo_width()}x{self.winfo_height()}",
            f"screen: {self.winfo_screenwidth()}x{self.winfo_screenheight()}",
        ]
        for child in self.winfo_children():
            lines.append(
                f"{child.winfo_class():12s} y={child.winfo_y():4d} "
                f"h={child.winfo_height():4d} w={child.winfo_width():4d} "
                f"mapped={child.winfo_ismapped()}"
            )
        out.write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        self.destroy()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="慕心 · 桌面陪伴助手")
    ap.add_argument("--model", default=str(ROOT / "models" / "Qwen2.5-1.5B-Instruct"))
    ap.add_argument("--adapter", default=str(ROOT / "output" / "lora-v3"))
    ap.add_argument("--no-adapter", action="store_true")
    ap.add_argument("--mock", action="store_true", help="不加载模型，只验证界面")
    ap.add_argument("--selftest", action="store_true", help="导出控件布局后自动退出")
    ap.add_argument("--say", default=None, help="启动后自动发送一句，用于自动化联调")
    return ap.parse_args(argv)


def _enable_dpi_awareness():
    """Windows 高 DPI 声明。

    不声明时系统会把整个窗口按显示缩放（本机 125%）二次拉伸，
    表现是窗口右侧和底部被裁掉一截、输入框看不见 —— 之前所有截图异常都源于此。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # system DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()        # 老系统兜底
        except Exception:
            pass


def main():
    _enable_dpi_awareness()
    args = parse_args()
    adapter = None if args.no_adapter else args.adapter
    app = MuxinApp(args.model, adapter, mock=args.mock, auto_say=args.say)
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    if args.selftest:
        app.after(1200, app.selftest_dump)
    app.mainloop()


if __name__ == "__main__":
    main()
