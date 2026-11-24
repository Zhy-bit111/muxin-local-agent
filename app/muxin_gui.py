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

# 打包成 exe 后 __file__ 在临时解包目录里，图标/记忆文件必须跟着 exe 走
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 注意：llm_client 依赖 torch/transformers，分发版（打包）里没有这些包，
# 所以它只能在走 transformers 后端时延迟导入；这里只导入纯标准库的模块。
from app.memory import Memory                           # noqa: E402
from app.persona import (                               # noqa: E402
    REMIND_INSTRUCTIONS,
    fallback_reply,
)
from app.runtime_input import compose_turn              # noqa: E402
from app.engine import respond as respond_once          # noqa: E402
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
                 auto_say: str | None = None, gguf_path: str | None = None,
                 gpu_layers: int = 0, router_enabled: bool = True,
                 style_enabled: bool = True):
        super().__init__()
        self.model_path = model_path
        self.adapter_path = adapter_path
        self.mock = mock
        self.auto_say = auto_say
        self.gguf_path = gguf_path
        # C 阶段方案 A：确定性意图交给程序层。--no-router 可整体回退到纯模型。
        self.router_enabled = router_enabled
        # 风格档：--no-style 可整体回退到落地前的口吻，用于同尺子对照。
        self.style_enabled = style_enabled
        # 方案 C：本地是唯一默认后端；在线要用户在界面里亲手填、亲手开。
        self._local_client = None
        self.capability = None
        self._source_hint = ""
        self._source_win = None
        self._src_vars = None
        self._src_status = None
        # 主动提醒设置面板（纯本地，与能力来源面板互不相干）
        self._remind_win = None
        self._remind_status = None
        self._remind_rand_var = None
        self._remind_freq_var = None
        # GGUF 后端的 GPU 卸载层数。默认 0（纯 CPU）以保证与 A-3 基线同构；
        # 有独显时可显式 --gpu-layers 20 加速，数值分布会略有变化。
        self.gpu_layers = gpu_layers

        self.client: LLMClient | None = None
        self.memory = Memory(ROOT / "data" / "memory.json")
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
        if not ico.exists() and getattr(sys, "_MEIPASS", None):
            # PyInstaller 把 --add-data 放进解包目录（_internal），不在 exe 同级
            ico = Path(sys._MEIPASS) / "assets" / "muxin.ico"
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

        self.remind_cfg_btn = tk.Button(
            header, text="提醒设置", command=self._open_remind_dialog,
            bg=HEADER_BG, fg=TEXT_DIM, activebackground=HEADER_BG,
            activeforeground=ACCENT, bd=0, relief="flat",
            font=(font_ui, 9), cursor="hand2", padx=px(8),
        )
        self.remind_cfg_btn.pack(side="right", padx=(0, px(4)))

        self.source_btn = tk.Button(
            header, text="能力来源", command=self._open_source_dialog,
            bg=HEADER_BG, fg=TEXT_DIM, activebackground=HEADER_BG,
            activeforeground=ACCENT, bd=0, relief="flat",
            font=(font_ui, 9), cursor="hand2", padx=px(8),
        )
        self.source_btn.pack(side="right", padx=(0, px(6)))

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
        # 随机时刻 / 随机品类 / 早睡窗口都读 configs/reminders.json。
        # 这一层只在本机跑，与在线能力（方案 C）没有任何依赖。
        self.scheduler = ReminderScheduler(str(state_path))
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
        self._refresh_remind_status()
        self.status.config(text=self._status_text())

    # ---------------- 主动提醒设置（纯本地） ----------------
    def _refresh_remind_status(self):
        lbl = getattr(self, "_remind_status", None)
        if lbl is not None and self.scheduler and lbl.winfo_exists():
            try:
                lbl.config(text=self.scheduler.status())
            except tk.TclError:
                pass

    def _open_remind_dialog(self):
        """打开就能点：随机开关、三档频率、现在来一条、下一次约几点。"""
        if self._remind_win is not None and self._remind_win.winfo_exists():
            self._remind_win.lift()
            return
        if not self.scheduler:
            return
        from app.scheduler import FREQUENCY_PRESETS

        font_ui = FONT_UI if FONT_UI in tkfont.families() else FONT_UI_FALLBACK

        def px(v):
            return int(round(v * getattr(self, "_dpi_scale", 1.0)))

        win = tk.Toplevel(self)
        self._remind_win = win
        win.title("提醒设置")
        win.configure(bg=BG)
        win.geometry(f"{px(440)}x{px(430)}")
        win.transient(self)

        tk.Label(win, text="主动提醒", bg=BG, fg=TEXT_MAIN,
                 font=(font_ui, 13, "bold"), anchor="w").pack(
            anchor="w", padx=px(18), pady=(px(16), px(4)))
        tk.Label(win,
                 text="开着的时候，会随机挑时候轻轻提一句喝水、起来动动、看看远处、"
                      "该吃饭了，晚上再随机找个时间催早睡。这些全在本机定时触发，"
                      "跟在线能力没关系：不启用在线、断网、甚至模型没加载好，也照常发。",
                 bg=BG, fg=ACCENT, font=(font_ui, 9), justify="left",
                 anchor="w", wraplength=px(400)).pack(
            anchor="w", padx=px(18), pady=(0, px(12)))

        rand_v = tk.BooleanVar(value=bool(self.scheduler.cfg.get("random", True)))
        freq_v = tk.StringVar(value=str(self.scheduler.cfg.get("frequency", "正常")))
        self._remind_rand_var = rand_v
        self._remind_freq_var = freq_v

        tk.Checkbutton(win, text="随机时刻（关掉就是固定间隔）", variable=rand_v,
                       command=self._remind_apply, bg=BG, fg=TEXT_MAIN,
                       selectcolor=INPUT_BG, activebackground=BG,
                       activeforeground=ACCENT, bd=0, font=(font_ui, 9),
                       cursor="hand2").pack(anchor="w", padx=px(18))

        tk.Label(win, text="多久来一条", bg=BG, fg=TEXT_DIM, font=(font_ui, 9),
                 anchor="w").pack(anchor="w", padx=px(18), pady=(px(10), px(2)))
        row = tk.Frame(win, bg=BG)
        row.pack(anchor="w", padx=px(18))
        for name in FREQUENCY_PRESETS:
            tk.Radiobutton(row, text=name, value=name, variable=freq_v,
                           command=self._remind_apply, bg=BG, fg=TEXT_MAIN,
                           selectcolor=INPUT_BG, activebackground=BG,
                           activeforeground=ACCENT, bd=0, font=(font_ui, 9),
                           cursor="hand2").pack(side="left", padx=(0, px(14)))

        tk.Button(win, text="现在来一条试试", command=self._remind_fire_once,
                  bg=HEADER_BG, fg=ACCENT, activebackground=BORDER,
                  activeforeground=TEXT_MAIN, bd=0, relief="flat",
                  font=(font_ui, 9), cursor="hand2", padx=px(12),
                  pady=px(6)).pack(anchor="w", padx=px(18), pady=(px(16), px(4)))

        self._remind_status = tk.Label(
            win, text=self.scheduler.status(), bg=BG, fg=TEXT_DIM,
            font=(font_ui, 9), anchor="w", justify="left", wraplength=px(400),
        )
        self._remind_status.pack(anchor="w", padx=px(18), pady=(px(8), 0))
        tk.Label(win,
                 text="免打扰时段和间隔写在 configs/reminders.json，改完重启生效。"
                      "顶部「提醒 开 / 关」可以一键全停。",
                 bg=BG, fg=TEXT_DIM, font=(font_ui, 8), anchor="w",
                 justify="left", wraplength=px(400)).pack(
            anchor="w", padx=px(18), pady=(px(10), 0))

        def refresh():
            if not win.winfo_exists():
                return
            self._refresh_remind_status()
            win.after(1500, refresh)

        refresh()

        def close_win():
            self._remind_status = None
            self._remind_rand_var = None
            self._remind_freq_var = None
            self._remind_win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_win)

    def _remind_apply(self):
        if not self.scheduler:
            return
        self.scheduler.set_frequency(self._remind_freq_var.get())
        self.scheduler.set_random(bool(self._remind_rand_var.get()))
        self._refresh_remind_status()
        self.status.config(text=self._status_text())

    def _remind_fire_once(self):
        if not self.scheduler:
            return
        if self.busy:
            self._append_hint("上一条还没说完，等下再试")
            return
        if not self.remind_on:
            self._append_hint("提醒现在关着，先点顶部打开")
            return
        self.scheduler.fire_once()
        self._refresh_remind_status()

    # ---------------- 能力来源（方案 C，默认只用本地） ----------------
    def _on_source_status(self, text: str):
        """由生成线程回调，Tk 只能主线程碰 —— 一律走队列。"""
        self.q.put(("source_status", text))

    def _update_source_label(self):
        if not hasattr(self, "sub_label"):
            return
        online = self.capability.online if self.capability else None
        src = "在线" if (online and online.config.ready()) else "本地"
        hint = f" · {self._source_hint[:12]}" if self._source_hint else ""
        self.sub_label.config(
            text=f"陪伴中 · {src}{hint} · 提醒{'开' if self.remind_on else '关'}"
        )

    def _set_src_status(self, text: str):
        lbl = getattr(self, "_src_status", None)
        if lbl is not None and lbl.winfo_exists():
            lbl.config(text=text)

    def _open_source_dialog(self):
        """打开就能点：三个输入框 + 三个按钮。密钥只在内存，关窗即清。"""
        if self._source_win is not None and self._source_win.winfo_exists():
            self._source_win.lift()
            return
        from app.online_client import OnlineConfig

        cfg = OnlineConfig.from_local()
        font_ui = FONT_UI if FONT_UI in tkfont.families() else FONT_UI_FALLBACK

        def px(v):
            return int(round(v * getattr(self, "_dpi_scale", 1.0)))

        win = tk.Toplevel(self)
        self._source_win = win
        win.title("能力来源")
        win.configure(bg=BG)
        win.geometry(f"{px(440)}x{px(470)}")
        win.transient(self)

        tk.Label(win, text="能力来源", bg=BG, fg=TEXT_MAIN,
                 font=(font_ui, 13, "bold"), anchor="w").pack(
            anchor="w", padx=px(18), pady=(px(16), px(4)))
        tk.Label(
            win,
            text="默认只用本地，不联网。开启在线后，未被程序接管的闲聊会连同"
                 "人设、最近两轮对话和相关记忆一起发给你填的服务商；"
                 "身份、时间、边界、记忆、提醒仍在本机处理。",
            bg=BG, fg=ACCENT, font=(font_ui, 9), justify="left",
            anchor="w", wraplength=px(400),
        ).pack(anchor="w", padx=px(18), pady=(0, px(12)))

        url_v = tk.StringVar(value=cfg.base_url)
        model_v = tk.StringVar(value=cfg.model)
        key_v = tk.StringVar(value=cfg.api_key)
        self._src_vars = (url_v, model_v, key_v)

        def field(label, var, secret=False):
            tk.Label(win, text=label, bg=BG, fg=TEXT_DIM, font=(font_ui, 9),
                     anchor="w").pack(anchor="w", padx=px(18), pady=(px(6), 0))
            tk.Entry(win, textvariable=var, show="*" if secret else None,
                     bg=INPUT_BG, fg=TEXT_MAIN, insertbackground=ACCENT,
                     bd=0, relief="flat", font=(font_ui, 9)).pack(
                fill="x", padx=px(18), pady=px(3), ipady=px(4))

        field("服务地址（OpenAI 兼容，以 /v1 结尾）", url_v)
        field("模型名", model_v)
        field("API 密钥（只存内存，也可设环境变量 MUXIN_API_KEY）", key_v, secret=True)

        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", padx=px(18), pady=(px(14), px(6)))
        for text, cmd in (("只用本地", self._apply_local_only),
                          ("启用在线", self._apply_online),
                          ("测试连接", self._test_online)):
            tk.Button(btns, text=text, command=cmd, bg=HEADER_BG, fg=ACCENT,
                      activebackground=BORDER, activeforeground=TEXT_MAIN,
                      bd=0, relief="flat", font=(font_ui, 9), cursor="hand2",
                      padx=px(10), pady=px(5)).pack(side="left", padx=(0, px(8)))

        self._src_status = tk.Label(
            win, text="当前：只用本地", bg=BG, fg=TEXT_DIM, font=(font_ui, 9),
            anchor="w", justify="left", wraplength=px(400),
        )
        self._src_status.pack(anchor="w", padx=px(18), pady=(px(8), 0))
        tk.Label(
            win,
            text="「测试连接」只在你点击时发一句；平时不点就不会有请求。"
                 "断网、超时、报错会自动降级到本地，并在这里显示。",
            bg=BG, fg=TEXT_DIM, font=(font_ui, 8), anchor="w",
            justify="left", wraplength=px(400),
        ).pack(anchor="w", padx=px(18), pady=(px(10), 0))

        def close_win():
            key_v.set("")
            self._src_vars = None
            self._src_status = None
            self._source_win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_win)

    def _apply_local_only(self):
        if self.capability:
            self.capability.set_online(None)
        self._source_hint = ""
        self._set_src_status("已切回本地：不会有对话内容出本机")
        self._update_source_label()

    def _apply_online(self):
        from app.online_client import OnlineClient, OnlineConfig

        if not self.capability:
            self._set_src_status("模型还没加载好，等一下再开")
            return
        if not self._src_vars:
            return
        url, model, key = (v.get().strip() for v in self._src_vars)
        if not url or not model or not key:
            self._set_src_status("地址、模型名、密钥三样都要填")
            return
        cfg = OnlineConfig(base_url=url, model=model, api_key=key,
                           enabled=True, timeout_seconds=8)
        self.capability.set_online(OnlineClient(cfg))
        self._source_hint = ""
        self._set_src_status("已启用。下一句闲聊会发出本机；可先点「测试连接」。")
        self._update_source_label()

    def _test_online(self):
        online = self.capability.online if self.capability else None
        if not online or not online.config.ready():
            self._set_src_status("先填好三项并点「启用在线」再测试")
            return
        self._set_src_status("正在测试…（只发这一句）")

        def work():
            reply = online.chat("在吗", kind="chat")
            msg = ("在线通了：" + reply[:28]) if reply else "没通，会自动降级到本地"
            self.q.put(("source_test", msg))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 模型 ----------------
    def _load_model(self):
        try:
            if self.gguf_path:
                # 分发形态：llama.cpp + Q4 GGUF，不依赖 torch
                from app.llm_gguf import GGUFClient

                local = GGUFClient(
                    self.gguf_path, mock=self.mock, n_gpu_layers=self.gpu_layers
                )
            else:
                # 开发形态才用 transformers；打包版走不到这里
                from app.llm_client import LLMClient

                local = LLMClient(
                    self.model_path, adapter_path=self.adapter_path, mock=self.mock
                )
            # 方案 C：本地是唯一默认后端。CapabilityClient 实现同一个 chat() 接口，
            # engine / 调度器 / 评测都不用知道自己背后是本地还是在线。
            from app.online_client import CapabilityClient

            self._local_client = local
            self.capability = CapabilityClient(
                local, online_client=None, on_status=self._on_source_status
            )
            self.client = self.capability
            self.q.put(("ready", None))
        except Exception as e:  # 加载失败也要把界面留住
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def _recent_history(self, n: int = 2) -> list[tuple[str, str]]:
        """最近 n 轮真实对话，用作多轮 messages（B 阶段）。

        提醒事件不是用户说的话，不能当上下文喂给模型 —— 否则又会把
        「该喝水了」当成当前话题（A-3 里噪声串味就是这么来的）。
        """
        out = []
        for e in self.memory.data.get("episodes", [])[-n:]:
            u = (e.get("user") or "").strip()
            b = (e.get("bot") or "").strip()
            if not u or u.startswith("[提醒]"):
                continue
            out.append((u[:80], b[:80]))
        return out

    def _gen_worker(self, instruction: str, kind: str, context: str = ""):
        """输入构造走 compose_turn，回复走 engine.respond —— 与评测唯一同源。

        A-2 的 bug：提醒路径先 build_user_text(instr, ctx) 拼一次背景，
        这里又注入 memory_block 拼第二次；且 episodes 从没作为多轮消息进过 prompt。
        C 阶段方案 A 又加了一道分叉（程序化 vs 模型），所以回复也必须收成一处，
        否则「软件跑的」和「尺子量的」会再次变成两条链路。
        """
        is_remind = kind in REMIND_INSTRUCTIONS and kind not in ("chat", "boundary")
        try:
            turn = compose_turn(
                instruction,
                context=context,
                memory=self.memory,
                history=self._recent_history(),
                kind=kind,
                is_reminder=is_remind,
            )
            ans = respond_once(
                self.client,
                turn,
                memory=self.memory,
                seed=None,                 # GUI 不固定种子，避免连问两次复读同一句
                enable_router=self.router_enabled,
                enable_style=self.style_enabled,
            )
            reply = ans.text
            if ans.source == "programmatic":
                print(f"[router] {ans.rule}: {instruction}")
        except Exception as e:
            print(f"[gen] 生成失败: {e}")
            reply = fallback_reply(kind, instruction)
        try:
            self.memory.add_episode(instruction, reply, kind)
        except Exception as e:
            # 写记忆失败不能让 busy 永远卡住：队列一定要 put
            print(f"[gen] 记忆写入失败: {e}")
        self.q.put(("reply", (kind, reply, instruction)))

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
        # 放在 busy 检查之后：被拒答的消息不该写进长期记忆
        remembered = self.memory.parse_remember(text)
        if remembered:
            self._append_hint(f"记住了：{remembered}")
        self.busy = True
        self.status.config(text="慕心正在想…")
        threading.Thread(target=self._gen_worker, args=(text, "chat"), daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ready":
                    self.ready = True
                    self._update_source_label()
                    self.status.config(text=self._status_text())
                    self._append_bot("姐姐 我醒啦 要不要先喝口水呀")
                    if self.auto_say:
                        self.after(400, self._auto_send)
                elif kind == "source_status":
                    self._source_hint = payload
                    self._update_source_label()
                elif kind == "source_test":
                    self._append_hint(payload)
                    self._update_source_label()
                elif kind == "error":
                    self.status.config(text=f"模型加载失败：{payload}")
                    self._append_hint("模型没加载起来，先看看 output 里的报错")
                elif kind == "reply":
                    _, reply, _ = payload
                    self.busy = False
                    self._append_bot(reply)
                    self.status.config(text=self._status_text())
                    if self.auto_say:
                        chat_out = ROOT / "data" / "_gui_chat.txt"
                        chat_out.parent.mkdir(parents=True, exist_ok=True)
                        chat_out.write_text(
                            f"用户：{self.auto_say}\n慕心：{reply}\n", encoding="utf-8"
                        )
                        self.after(20000, self.destroy)   # 留时间给外部截图
                elif kind == "remind":
                    rkind, instr, ctx = payload
                    if self.busy:
                        # 旧逻辑无条件覆盖 busy，会和正在跑的 _gen_worker 并发写 memory，
                        # 且先到的 reply 会提前解锁。调度器已推进 last_water，不会重发。
                        print("[gui] 正在回复，跳过本次提醒（避免并发）")
                        continue
                    self.busy = True
                    self.status.config(text="慕心正在想…")
                    threading.Thread(
                        target=self._gen_worker, args=(instr, rkind, ctx), daemon=True
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
            nxt = next((x for x in s if "下次" in x), "").strip()
            return f"{nxt}    ·    提醒 {'开' if self.remind_on else '关'}"
        except Exception:
            return "就绪"

    def _auto_send(self):
        self.entry.insert(0, self.auto_say or "")
        self._on_send()

    def on_close(self):
        if self.scheduler:
            self.scheduler.stop()
        # 走 GGUF 后端时 llama-server 是我们拉起的子进程，必须收掉，
        # 否则关掉窗口后它还占着显存/内存
        if self.client is not None and hasattr(self.client, "close"):
            try:
                self.client.close()
            except Exception:
                pass
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
    ap.add_argument("--adapter", default=str(ROOT / "output" / "lora-v5"))
    ap.add_argument("--gguf", default=None, help="用 llama.cpp GGUF 模型（分发形态，优先于 --model/--adapter）")
    ap.add_argument("--gpu-layers", type=int, default=0,
                    help="GGUF 后端卸载到 GPU 的层数（默认 0 纯 CPU；有独显可试 20，需 CUDA 版 llama-server）")
    ap.add_argument("--no-adapter", action="store_true")
    ap.add_argument("--no-router", action="store_true",
                    help="关掉程序化能力层，回退到 B 阶段的纯模型链路")
    ap.add_argument("--no-style", action="store_true",
                    help="关掉风格层，回退到 9 张截图落地前的口吻（同尺子对照用）")
    ap.add_argument("--mock", action="store_true", help="不加载模型，只验证界面")
    ap.add_argument("--selftest", action="store_true", help="导出控件布局后自动退出")
    ap.add_argument("--say", default=None, help="启动后自动发送一句，用于自动化联调")
    args = ap.parse_args(argv)

    # 打包版没有源码里的 output/ 目录，参数也没法带：
    # exe 同级放 model/muxin.gguf 时直接认，双击 慕心.exe 就能跑
    if not args.gguf:
        bundled = ROOT / "model" / "muxin.gguf"
        if bundled.exists():
            args.gguf = str(bundled)
    return args


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
    app = MuxinApp(args.model, adapter, mock=args.mock, auto_say=args.say,
                   gguf_path=args.gguf, gpu_layers=args.gpu_layers,
                   router_enabled=not args.no_router,
                   style_enabled=not args.no_style)
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    if args.selftest:
        app.after(1200, app.selftest_dump)
    app.mainloop()


if __name__ == "__main__":
    main()
