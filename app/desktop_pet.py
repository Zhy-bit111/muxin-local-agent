#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌面虚拟形象（智能桌宠）· 一层**可丢弃的皮**。

## 这条边界最重要

本文件**没有修改任何现有链路**：`muxin_gui.py` / `engine.py` / `runtime_input.py` /
`router.py` / `style.py` / `scheduler.py` / `persona.py` / `online_client.py` 一行未动。
把桌宠这几个文件删掉，项目立刻回到原样；不参与评测、不参与打包脚本、不是新主入口。

- 回复只走 `app.engine.respond()` —— 与 GUI、评测**唯一同源**；
- 输入只走 `app.runtime_input.compose_turn()`；
- 主动提醒只走 `app.scheduler.ReminderScheduler`（纯本地、默认开、与方案 C 无关）；
- **运行时零网络**：不读 `configs/online.json`、不 import `app.online_client`、不发请求。
  只有显式 `--online` 才允许接方案 C，且提醒类仍由硬闸强制本地。
  （AI 立绘是**构建期**一次性产物，已经落成本地 PNG，运行时不联网。）

## 交互约定

- 左键**拖**着走，拖过就不算点击；
- **单击** = 简单互动（抖一下 + 一句本地短句，不调模型、不算模型能力）；
- **双击** = 打开聊天窗口；单击会延迟 260ms 等双击，两者互斥；
- 右键 = 菜单；窗口无边框所以退出只在菜单里，避免误关；
- **滚轮 / 右键档位 / 大小设置滑块**都能调大小，比例记在 `configs/pet.json`；
- 置顶常驻，位置 / 置顶 / 形象选择 / 大小记在 `configs/pet.json`，重开还在。

## 形象双模式

`assets/pet_black_cat.png` 存在则优先用 AI 立绘（本地色键 + 启动时预生成 55 帧形变帧库），
否则自动回退到 `app/pet_art.py` 的程序化小黑猫；`--no-sprite` 可强制回退。
两种形象共用同一套动作时间表：**微动作**每 2.5~6 秒（眨眼 / 抖耳 / 甩尾 / 看你一眼），
**大动作**每 9~20 秒（伸懒腰 / 打哈欠 / 抖毛），全部纯本地、不调模型。
立绘的动作由预烘焙的形变帧表达（呼吸、眼皮压下来、尾巴耳朵局部位移），
运行时只切帧、不做图像运算，也不叠加任何简笔线条。

## 「模型当内核」这句话要小心

真正的内核是程序层（调度器 / 规则 / 记忆 / 风格），模型只是可替换的话术生成器。
`client=None`（mock、模型没加载、断网）时，这只猫照样呼吸、眨眼、自己做动作、
到点喊你喝水 —— 只是那句话由本地兜底说。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import queue
import random
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import pet_art                                    # noqa: E402
from app.engine import respond as respond_once             # noqa: E402
from app.memory import Memory                             # noqa: E402
from app.persona import REMIND_INSTRUCTIONS, fallback_reply    # noqa: E402
from app.runtime_input import compose_turn                # noqa: E402
from app.scheduler import ReminderScheduler               # noqa: E402

# ---------------------------------------------------------------- 常量
W, H = 260, 300
CHROMA = pet_art.CHROMA
BUBBLE_BG = pet_art.BUBBLE_BG
INK = "#2B2A34"
EDGE = "#D8CBBB"
LIGHT = "#454250"

FONT = "Microsoft YaHei"
CONFIG_PATH = ROOT / "configs" / "pet.json"
CLICK_DELAY_MS = 260          # 等双击的窗口；单击在此期间不执行
ANIM_MS = 80                 # 约 12.5fps；纯切帧，开销极低
MICRO_GAP = (2.5, 6.0)       # 微动作：让猫一直「活着」，但不说话
MACRO_GAP = (9.0, 20.0)      # 大动作：伸懒腰 / 打哈欠 / 抖毛
GLANCE_GAP = (3.2, 7.0)      # 鼠标经过时转头看你的最短间隔
ACTION_DURATION = {
    "stretch": 1.1, "yawn": 0.9, "ear_twitch": 0.5, "tail_flick": 0.6,
    "look_away": 0.7, "shake": 0.6, "blink": 0.42, "poke_react": 0.55,
    "glance_left": 0.55, "glance_right": 0.55, "pat": 0.8,
}

SCALE_MIN, SCALE_MAX, SCALE_DEFAULT = 0.6, 2.0, 1.0
SCALE_PRESETS = (
    ("迷你 70%", 0.7), ("小 85%", 0.85), ("正常 100%", 1.0),
    ("大 120%", 1.2), ("很大 150%", 1.5), ("超大 180%", 1.8),
)

# 纯表现层的动作反馈。**这些是程序说的，不是模型能力**，也不进任何评测明细。
# 写成语义类别的短句池，不引用评测题面、不读 expect。
POKE_LINES = [
    "喵（耳朵抖了一下）",
    "干嘛 别戳太用力",
    "在呢 别没事找事",
    "喵（尾巴扫过你手背）",
    "嗯？我可没空理你",
    "行吧 勉强抬下眼皮",
]
PAT_LINES = [
    "呼噜呼噜（脑袋往你手心蹭）",
    "喵（眼睛眯成一条缝）",
    "这个可以 再摸一下",
    "行吧 勉强让你摸",
]


def _pick(pool: list[str], salt: str) -> str:
    """确定性挑选，避免连点两次复读同一句，也方便离线自测。"""
    idx = int(hashlib.md5(salt.encode("utf-8")).hexdigest()[:8], 16) % len(pool)
    return pool[idx]


def _load_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


class PetApp(tk.Tk):
    """置顶、无边框、透明背景、常驻桌面的小黑猫。"""

    def __init__(self, *, mock: bool = True, gguf_path: str | None = None,
                 allow_online: bool = False, autostart: bool = True,
                 state_path: str | None = None, memory_path: str | None = None,
                 config_path: str | None = None, use_sprite: bool = True,
                 action_seed: int | None = None):
        super().__init__()
        self.title("慕心 · 桌宠")
        self.mock = mock
        self.gguf_path = gguf_path
        self.allow_online = allow_online     # 默认 False：连 online.json 都不读
        self.config_path = Path(config_path or CONFIG_PATH)
        self.cfg = _load_config(self.config_path)
        self.use_sprite = bool(self.cfg.get("sprite", use_sprite)) and use_sprite
        self.ready = False
        self.client = None
        self.busy = False
        self._t = 0.0
        self._poke_n = 0
        self._pat_n = 0
        self._blink_until = 0.0
        self._rng = random.Random(action_seed)
        self._action = "idle"
        self._action_started = 0.0
        self._action_until = 0.0
        # 两条独立时间线：微动作高频、大动作低频；再加一条「看你一眼」
        self._next_action_at = self._rng.uniform(*MACRO_GAP)
        self._next_micro_at = self._rng.uniform(*MICRO_GAP)
        self._next_glance_at = self._t + self._rng.uniform(*GLANCE_GAP)
        self._sprite_frames = None
        self._frame_cache: dict[int, dict] = {}
        self._history: list[tuple[str, str]] = []
        self.q: queue.Queue = queue.Queue()
        self._anim_job = self._poll_job = self._bubble_job = self._click_job = None
        self._scale_job = None
        self._size_win = None
        self._size_lbl = None
        self._moved = False
        self._click_xy = (0, 0)
        self._chat_win = None
        self._last_bubble = ("", 0.0)
        self._mouse_dx = 0.0
        self._mouse_active_until = 0.0
        self._topmost = bool(self.cfg.get("topmost", True))
        try:
            s = float(self.cfg.get("scale", SCALE_DEFAULT))
        except (TypeError, ValueError):
            s = SCALE_DEFAULT
        self._s = max(SCALE_MIN, min(SCALE_MAX, s))
        self._pending_scale = self._s

        ui_font = FONT if FONT in tkfont.families() else "SimHei"
        self._font = (ui_font, 10)
        self._font_small = (ui_font, 9)

        self.overrideredirect(True)
        self.attributes("-topmost", self._topmost)
        try:
            # Windows 有效；其它平台抛 TclError 时退化成洋红底，不影响功能
            self.attributes("-transparentcolor", CHROMA)
        except tk.TclError:
            pass
        self.resizable(False, False)

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        ww, hh = self._win_size()
        x = self.cfg.get("x")
        y = self.cfg.get("y")
        if not isinstance(x, int) or not isinstance(y, int) \
                or x < -ww + 20 or x > sw - 20 or y < -20 or y > sh - 20:
            x, y = max(0, sw - ww - 60), max(0, sh - hh - 90)
        self.geometry(f"{ww}x{hh}+{x}+{y}")

        self.cv = tk.Canvas(self, width=ww, height=hh, bg=CHROMA,
                            highlightthickness=0, bd=0, cursor="hand2")
        self.cv.pack()
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Double-Button-1>", self._double)
        self.cv.bind("<Button-3>", self._menu)
        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<MouseWheel>", self._wheel)
        self.protocol("WM_DELETE_WINDOW", self._quit)

        # 与主窗口共用记忆（同一只猫）；state 用独立文件，避免两个程序互写
        self.memory = Memory(memory_path or (ROOT / "data" / "memory.json"))
        self.scheduler = ReminderScheduler(
            state_path or str(ROOT / "data" / "state_pet.json"))
        if autostart:
            self.scheduler.start(self._on_remind)
        else:
            # 离线自测：只装回调不启线程，仍可用 fire_once() 快进验证
            self.scheduler.prepare(self._on_remind)

        if self.use_sprite:
            self._reload_frames()
        self._draw_cat()
        self._say("我醒啦 双击能说话 右键有菜单" if mock else "正在醒来…", 5)
        self._load_client()
        self._animate()
        self._poll_queue()

    # ------------------------------------------------------------ 形象 / 大小
    def _win_size(self):
        return int(W * self._s), int(H * self._s)

    def _reload_frames(self):
        """按当前大小取帧库；同一档位只生成一次，最多缓存 4 档。"""
        size = int(pet_art.DEFAULT_SPRITE_SIZE * self._s)
        if size not in self._frame_cache:
            frames = pet_art.sprite_frames(pet_art.SPRITE_PATH, size=size)
            if frames is None:
                self._sprite_frames = None
                return
            if len(self._frame_cache) >= 4:
                self._frame_cache.pop(next(iter(self._frame_cache)))
            self._frame_cache[size] = frames
        self._sprite_frames = self._frame_cache[size]

    def _apply_scale(self, value: float, *, quiet: bool = False):
        s = max(SCALE_MIN, min(SCALE_MAX, float(value)))
        self._pending_scale = s
        if abs(s - self._s) < 0.001:
            return
        self._s = s
        ww, hh = self._win_size()
        self.cv.config(width=ww, height=hh)
        self.geometry(f"{ww}x{hh}+{self.winfo_x()}+{self.winfo_y()}")
        if self.use_sprite:
            self._reload_frames()
        self._draw_cat()
        text, secs = self._last_bubble     # 气泡按新比例重画，不会残留旧尺寸
        if text:
            self._say(text, secs)
        if not quiet:
            self._save_config()

    def _queue_scale(self, value: float):
        """滑块 / 滚轮去抖：停 160ms 才重建帧库，拖动时不会卡。"""
        self._pending_scale = max(SCALE_MIN, min(SCALE_MAX, float(value)))
        if self._size_lbl is not None:
            try:
                self._size_lbl.config(text=f"{int(self._pending_scale * 100)}%")
            except tk.TclError:
                pass
        if self._scale_job:
            try:
                self.after_cancel(self._scale_job)
            except tk.TclError:
                pass
        self._scale_job = self.after(160, self._commit_scale)

    def _commit_scale(self):
        self._scale_job = None
        self._apply_scale(self._pending_scale)

    def _wheel(self, e):
        step = 0.05 if e.delta > 0 else -0.05
        self._queue_scale(self._pending_scale + step)

    def _motion(self, e):
        half = W * self._s / 2
        self._mouse_dx = max(-1.0, min(1.0, (e.x - half) / half))
        self._mouse_active_until = self._t + 3.0   # 鼠标离开 3 秒后不再转头

    def _draw_cat(self):
        dur = ACTION_DURATION.get(self._action, 0.8)
        phase = 0.0 if self._action == "idle" else max(
            0.0, min(1.0, (self._t - self._action_started) / dur))
        if self._sprite_frames:
            pet_art.draw_sprite(self.cv, self._sprite_frames,
                                t=self._t, action=self._action, phase=phase,
                                cx=int(130 * self._s), cy=int(200 * self._s),
                                scale=self._s)
        else:
            pet_art.draw_vector_cat(self.cv, t=self._t, action=self._action,
                                    phase=phase, blink_until=self._blink_until)
            if abs(self._s - 1.0) > 0.001:
                # 程序化猫用基准坐标绘制，整体缩放一次即可（无文字图元）
                self.cv.scale("cat", 0, 0, self._s, self._s)

    def _start_action(self, name: str):
        self._action = name
        self._action_started = self._t
        self._action_until = self._t + ACTION_DURATION.get(name, 0.8)

    def _schedule_idle_action(self):
        self._next_action_at = self._t + self._rng.uniform(*MACRO_GAP)

    def _step_actions(self):
        """两级动作状态机。抽成方法是为了离线自测能直接快进。"""
        if self._action != "idle" and self._t > self._action_until:
            self._action = "idle"
            self._schedule_idle_action()
            # 动作期间攒下的微动作不立刻补发，留出一点喘息
            self._next_micro_at = max(self._next_micro_at, self._t + 0.6)
        if self._action != "idle":
            return
        if self._t >= self._next_action_at:
            self._start_action(self._rng.choice(pet_art.MACRO_ACTIONS))
            self._schedule_idle_action()
        elif self._t >= self._next_micro_at:
            self._start_action(self._rng.choice(pet_art.MICRO_ACTIONS))
            self._next_micro_at = self._t + self._rng.uniform(*MICRO_GAP)
        elif (self._t >= self._next_glance_at
              and self._t < self._mouse_active_until
              and abs(self._mouse_dx) > 0.15):
            self._start_action("glance_left" if self._mouse_dx < 0
                               else "glance_right")
            self._next_glance_at = self._t + self._rng.uniform(*GLANCE_GAP)

    def _animate(self):
        self._t += ANIM_MS / 1000.0
        self._step_actions()
        self._draw_cat()
        self._anim_job = self.after(ANIM_MS, self._animate)

    # ------------------------------------------------------------ 气泡
    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
               x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
               x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.cv.create_polygon(pts, smooth=True, **kw)

    def _say(self, text: str, seconds: float = 6.0):
        """气泡。这里的截断**只作用于展示**，不写回任何明细、不参与评测。"""
        self.cv.delete("bubble")
        s = self._s
        self._last_bubble = (text or "", seconds)
        txt = (text or "").strip().replace("\n", " ")
        if len(txt) > 58:
            txt = txt[:57] + "…"
        lines = max(1, math.ceil(len(txt) / 13))
        y1 = min(120 * s, (6 + 22 * lines + 18) * s)
        self._round_rect(10 * s, 6 * s, 250 * s, y1, 13 * s,
                         fill=BUBBLE_BG, outline=EDGE,
                         width=max(1, int(2 * s)), tag="bubble")
        self.cv.create_polygon(112 * s, y1 - 2 * s, 128 * s, y1 + 12 * s,
                               142 * s, y1 - 2 * s,
                               fill=BUBBLE_BG, outline="", tag="bubble")
        self.cv.create_text(22 * s, 15 * s, text=txt, width=206 * s, anchor="nw",
                            font=(self._font[0], max(7, int(round(10 * s)))),
                            fill=INK, justify="left", tag="bubble")
        if self._bubble_job:
            try:
                self.after_cancel(self._bubble_job)
            except tk.TclError:
                pass
        self._bubble_job = self.after(int(seconds * 1000),
                                     lambda: self.cv.delete("bubble"))

    # ------------------------------------------------------------ 交互
    def _cancel_click(self):
        if self._click_job:
            try:
                self.after_cancel(self._click_job)
            except tk.TclError:
                pass
        self._click_job = None

    def _press(self, e):
        self._ox, self._oy = e.x_root, e.y_root
        self._wx, self._wy = self.winfo_x(), self.winfo_y()
        self._click_xy = (e.x, e.y)
        self._moved = False
        self._cancel_click()

    def _drag(self, e):
        dx, dy = e.x_root - self._ox, e.y_root - self._oy
        if abs(dx) + abs(dy) > 5:
            self._moved = True
            self._cancel_click()
        self.geometry(f"+{self._wx + dx}+{self._wy + dy}")

    def _release(self, _e=None):
        """单击不立刻执行，等 260ms 看有没有双击。"""
        self._cancel_click()
        if self._moved:
            return
        x, y = self._click_xy
        s = self._s
        if 10 * s <= x <= 250 * s and 20 * s <= y <= 295 * s:
            self._click_job = self.after(CLICK_DELAY_MS, self._poke)

    def _double(self, _e=None):
        """双击：取消待执行的单击，只打开聊天。"""
        self._cancel_click()
        self._moved = True          # 挡住随后那次 Release 再排一次单击
        self._open_chat()

    def _poke(self):
        self._click_job = None
        self._poke_n += 1
        self._start_action("poke_react")
        self._say(_pick(POKE_LINES, f"poke/{self._poke_n}"), 3.5)

    def _pat(self):
        self._pat_n += 1
        self._blink_until = self._t + 1.2
        self._start_action("yawn")
        self._say(_pick(PAT_LINES, f"pat/{self._pat_n}"), 4)

    def _toggle_sprite(self):
        if not pet_art.SPRITE_PATH.exists():
            self._say("没有立绘文件 先用简笔猫", 3.5)
            return
        self.use_sprite = not self.use_sprite
        if self.use_sprite:
            self._reload_frames()
        else:
            self._sprite_frames = None
        self._draw_cat()
        self._say("换成画像啦" if self.use_sprite else "换成简笔猫啦", 3)

    def _pick_scale(self, value: float):
        self._pending_scale = value
        self._apply_scale(value)

    def _open_size_dialog(self):
        """「打开就能点」的大小面板：拖滑块实时改，松手 160ms 后重建帧库。"""
        if self._size_win is not None and self._size_win.winfo_exists():
            self._size_win.lift()
            return
        win = tk.Toplevel(self)
        win.title("桌宠大小")
        win.configure(bg=BUBBLE_BG)
        win.attributes("-topmost", True)
        ww = self._win_size()[0]
        sw = self.winfo_screenwidth()
        x = max(0, min(self.winfo_x() + ww + 8, sw - 300))
        win.geometry(f"292x146+{x}+{max(0, self.winfo_y())}")

        tk.Label(win, text="大小", font=self._font, bg=BUBBLE_BG,
                 fg=INK).pack(anchor="w", padx=14, pady=(12, 0))
        self._size_lbl = tk.Label(win, text=f"{int(self._s * 100)}%",
                                  font=(self._font[0], 13), bg=BUBBLE_BG,
                                  fg=LIGHT)
        self._size_lbl.pack(anchor="w", padx=14)
        sc = tk.Scale(win, from_=SCALE_MIN, to=SCALE_MAX, resolution=0.05,
                      orient="horizontal", length=262, showvalue=False,
                      bg=BUBBLE_BG, fg=INK, troughcolor=EDGE,
                      highlightthickness=0, command=lambda v: self._queue_scale(v))
        sc.set(self._s)
        sc.pack(padx=14, pady=(2, 0))
        self._size_scale = sc

        bar = tk.Frame(win, bg=BUBBLE_BG)
        bar.pack(fill="x", padx=14, pady=(4, 10))
        tk.Button(bar, text="恢复默认", font=self._font_small, bg=LIGHT,
                  fg="#FFFFFF", bd=0, relief="flat", cursor="hand2",
                  command=lambda: (sc.set(SCALE_DEFAULT),
                                   self._queue_scale(SCALE_DEFAULT))).pack(side="left")
        tk.Button(bar, text="完成", font=self._font_small, bg=LIGHT,
                  fg="#FFFFFF", bd=0, relief="flat", cursor="hand2",
                  command=win.destroy).pack(side="right")

        def close():
            self._size_lbl = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)
        self._size_win = win

    def _menu(self, e):
        m = tk.Menu(self, tearoff=0, font=self._font_small)
        m.add_command(label="打开聊天（双击也行）", command=self._open_chat)
        m.add_command(label="摸一下头", command=self._pat)
        m.add_command(label="换形象：立绘 / 简笔", command=self._toggle_sprite)
        m.add_separator()
        sub = tk.Menu(m, tearoff=0, font=self._font_small)
        for label, value in SCALE_PRESETS:
            sub.add_command(label=label,
                            command=lambda v=value: self._pick_scale(v))
        sub.add_separator()
        sub.add_command(label="精细调节…（滑块）", command=self._open_size_dialog)
        self._size_menu = sub                 # 保活引用，否则子菜单会被 GC
        m.add_cascade(label=f"大小（当前 {int(self._s * 100)}%）", menu=sub)
        m.add_command(label="提示：滚轮也能调大小", state="disabled")
        m.add_separator()
        m.add_command(label="现在来一条提醒", command=self._fire_one)
        m.add_command(label="提醒 开 / 关", command=self._toggle_remind)
        m.add_command(label="取消 / 恢复置顶", command=self._toggle_topmost)
        m.add_separator()
        m.add_command(label="状态", command=lambda: self._say(self.scheduler.status(), 7))
        m.add_command(label="退出", command=self._quit)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _fire_one(self):
        k = self.scheduler.fire_once()
        if k is None:
            self._say("提醒先停着呢 右键可以打开", 4)

    def _toggle_remind(self):
        if self.scheduler.quiet:
            self.scheduler.resume()
            self._say("提醒开啦 我会不定时喊你", 4)
        else:
            self.scheduler.pause()
            self._say("好 先安静一会儿", 4)

    def _toggle_topmost(self):
        self._topmost = not self._topmost
        self.attributes("-topmost", self._topmost)
        self._say("置顶开" if self._topmost else "置顶关", 2.5)

    def _open_chat(self):
        if self._chat_win is not None and self._chat_win.winfo_exists():
            self._chat_win.lift()
            return
        win = tk.Toplevel(self)
        win.title("跟慕心说句话")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = max(0, min(self.winfo_x() - 50, sw - 360))
        y = self.winfo_y() + self._win_size()[1] + 8
        if y + 110 > sh:
            y = max(0, self.winfo_y() - 110)
        win.geometry(f"360x96+{x}+{y}")
        win.attributes("-topmost", True)
        win.configure(bg=BUBBLE_BG)
        entry = tk.Entry(win, font=self._font, bd=0, relief="flat",
                         bg="#FFFFFF", fg=INK, insertbackground=INK)
        entry.pack(fill="x", padx=12, pady=(12, 6), ipady=6)

        def send(_e=None):
            text = entry.get().strip()
            if not text:
                return
            if self.busy:
                self._say("上一条还没说完 等我一下", 3)
                return
            entry.delete(0, "end")
            self.busy = True
            try:
                self.memory.parse_remember(text)
            except Exception:
                pass
            self._say("…", 3)
            threading.Thread(target=self._gen_worker,
                             args=(text, "chat", ""), daemon=True).start()

        tk.Button(win, text="发送", command=send, font=self._font_small,
                  bg=LIGHT, fg="#FFFFFF", bd=0, relief="flat",
                  activebackground=INK, cursor="hand2").pack(anchor="e", padx=12)
        entry.bind("<Return>", send)
        entry.focus_set()
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._chat_win = win

    # ------------------------------------------------------------ 链路
    def _recent_history(self):
        return list(self._history)

    def _gen_worker(self, instruction: str, kind: str, context: str = ""):
        """与主 GUI 的 `_gen_worker` 同构：唯一输入构造 + 唯一回复入口。"""
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
                seed=None,
                enable_router=True,
                enable_style=None,       # 读 configs/style_profile.json
            )
            reply = ans.text
            if ans.source == "programmatic":
                print(f"[pet/router] {ans.rule}: {instruction}")
        except Exception as exc:
            print(f"[pet/gen] 生成失败: {exc}")
            reply = fallback_reply(kind, instruction)
        if not (reply or "").strip():
            # 空回复时给一句本地话，纯展示层保险，不写回任何评测明细
            reply = fallback_reply(kind, instruction)
        try:
            self.memory.add_episode(instruction, reply, kind)
        except Exception as exc:
            print(f"[pet/gen] 记忆写入失败: {exc}")
        self._history.append((instruction[:80], reply[:80]))
        if len(self._history) > 5:
            self._history = self._history[-5:]
        self.q.put(("reply", (kind, reply, instruction)))

    def _on_remind(self, kind: str, instruction: str, context: str):
        # 调度线程里不能碰 Tk，全部丢进队列
        self.q.put(("remind", (kind, instruction, context)))

    def _load_client(self):
        if self.mock:
            self.ready = True
            return

        def work():
            try:
                from app.llm_gguf import GGUFClient

                c = GGUFClient(str(self.gguf_path), n_gpu_layers=0)
                if self.allow_online:
                    # 只有显式 --online 才 import；配置默认 enabled=false，
                    # 没地址 / 模型名 / 密钥时 ready() 为假，不会构造在线客户端
                    from app.online_client import (
                        CapabilityClient, OnlineClient, OnlineConfig,
                    )
                    cfg = OnlineConfig.from_local()
                    if cfg.ready():
                        c = CapabilityClient(c, OnlineClient(cfg))
                self.q.put(("client", c))
            except Exception as exc:
                self.q.put(("client_error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "reply":
                    _, text, _ = payload
                    self.busy = False
                    self._say(text)
                elif kind == "remind":
                    k, instr, ctx = payload
                    threading.Thread(target=self._gen_worker,
                                     args=(instr, k, ctx), daemon=True).start()
                elif kind == "client":
                    self.client, self.ready = payload, True
                    self._say("我醒啦 双击能说话 右键有菜单", 5)
                elif kind == "client_error":
                    self.ready = True
                    print(f"[pet] 模型加载失败，改用本地兜底: {payload}")
                    self._say("模型没醒过来 先用本地话陪你", 5)
        except queue.Empty:
            pass
        self._poll_job = self.after(100, self._poll_queue)

    def _save_config(self):
        data = dict(self.cfg)
        try:
            data["x"], data["y"] = self.winfo_x(), self.winfo_y()
        except tk.TclError:
            pass
        data["topmost"] = self._topmost
        data["sprite"] = self.use_sprite
        data["scale"] = round(self._s, 3)
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _quit(self):
        for job in (self._anim_job, self._poll_job, self._bubble_job,
                    self._click_job, self._scale_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._size_lbl = None
        if self._size_win is not None:
            try:
                self._size_win.destroy()
            except tk.TclError:
                pass
        self._save_config()
        try:
            self.scheduler.stop()
        except Exception:
            pass
        try:
            if self.client is not None and hasattr(self.client, "close"):
                self.client.close()
        except Exception:
            pass
        self.destroy()

    # ------------------------------------------------------------ 自检
    def run_selftest(self) -> int:
        """不进 mainloop，验证渲染、交互、动作与链路。"""
        self.update()
        # 停掉轮询，避免 after 回调在自测期间把队列里的事件提前消费掉
        if self._poll_job:
            self.after_cancel(self._poll_job)
            self._poll_job = None
        checks: list[tuple[str, bool, str]] = []

        def ck(name, ok, note=""):
            checks.append((name, bool(ok), note))

        n_cat = len(self.cv.find_withtag("cat"))
        ck("形象已绘制", n_cat >= (1 if self._sprite_frames else 40),
           f"items={n_cat} mode={'立绘' if self._sprite_frames else '简笔'}")
        ck("气泡已绘制", len(self.cv.find_withtag("bubble")) >= 3,
           f"items={len(self.cv.find_withtag('bubble'))}")
        try:
            transparent_ok = (str(self.attributes("-transparentcolor")).upper()
                              == CHROMA.upper())
        except tk.TclError:
            transparent_ok = False
        ck("无边框置顶透明",
           bool(self.overrideredirect()) and bool(self.attributes("-topmost"))
           and transparent_ok, "")

        # 单击 / 双击互斥
        poke_before = self._poke_n
        self._click_xy, self._moved = (130, 200), False
        self._release()
        ck("单击进入 260ms 待定", self._click_job is not None)
        self._double()
        ck("双击取消待定单击", self._click_job is None
           and self._poke_n == poke_before, f"poke_n={self._poke_n}")
        ck("双击打开聊天窗口",
           self._chat_win is not None and self._chat_win.winfo_exists())
        if self._chat_win is not None and self._chat_win.winfo_exists():
            self._chat_win.destroy()
        self._click_xy, self._moved = (130, 200), True
        self._release()
        ck("拖拽不误触单击", self._click_job is None)

        # 自主小动作状态机
        self._start_action("stretch")
        self._t = self._action_until + 0.01
        self._step_actions()
        ck("动作结束自动回 idle", self._action == "idle")
        self._t = self._next_action_at + 0.01
        self._step_actions()
        ck("到点随机触发新动作", self._action in pet_art.IDLE_ACTIONS,
           f"action={self._action}")

        # 两级动作：高频微动作 + 鼠标经过时转头
        self._action = "idle"
        self._next_action_at = self._t + 999
        self._next_micro_at = self._t + 0.01
        self._t += 0.02
        self._step_actions()
        ck("高频微动作会触发", self._action in pet_art.MICRO_ACTIONS,
           f"action={self._action}")
        self._action = "idle"
        self._next_micro_at = self._t + 999
        self._next_glance_at = self._t
        self._mouse_active_until = self._t + 10
        self._mouse_dx = -0.8
        self._step_actions()
        ck("鼠标在左边会转头看你", self._action == "glance_left",
           f"action={self._action}")
        self._mouse_active_until = 0.0

        # 形变帧库（不是旧版 5 帧旋转）
        if self._sprite_frames:
            ck("立绘帧库含呼吸 / 眨眼池",
               len(self._sprite_frames.get("idle", [])) >= 8
               and len(self._sprite_frames.get("blink", [])) >= 3,
               f"pools={len(self._sprite_frames)}")

        # 大小调节：窗口、画布、帧库、气泡一起变
        self._apply_scale(1.4, quiet=True)
        cw, ch = int(self.cv.cget("width")), int(self.cv.cget("height"))
        ck("放大改变窗口与画布", (cw, ch) == (int(W * 1.4), int(H * 1.4)),
           f"{cw}x{ch}")
        if self._sprite_frames:
            ck("立绘帧按新大小重建",
               self._sprite_frames["idle"][0].width()
               == int(pet_art.DEFAULT_SPRITE_SIZE * 1.4),
               f"size={self._sprite_frames['idle'][0].width()}")
        ck("气泡按新比例重画", len(self.cv.find_withtag("bubble")) >= 3)
        self._apply_scale(1.0, quiet=True)
        ck("可回到原大小", abs(self._s - 1.0) < 0.001)

        # 程序化形象随时可回退
        saved = self._sprite_frames
        self._sprite_frames = None
        n_vec = pet_art.draw_vector_cat(self.cv, t=1.0, action="idle", phase=0.0)
        ck("程序化黑猫可回退", n_vec >= 40, f"items={n_vec}")
        self._sprite_frames = saved

        # 唯一链路 / 纯本地
        self._gen_worker("今天有点累", "chat", "")
        _, payload = self.q.get(timeout=10)
        got = (payload[1] or "").strip()
        ck("mock 闲聊出话", bool(got), f"reply={got[:24]!r}")
        self.scheduler.fire_once()
        events = []
        while True:
            try:
                events.append(self.q.get_nowait())
            except queue.Empty:
                break
        ck("主动提醒事件", any(k == "remind" for k, _ in events),
           f"events={[k for k, _ in events]}")
        ck("零网络模块未加载", "app.online_client" not in sys.modules, "")

        # 常驻设置落盘（selftest 用的是临时 config，不碰真实文件）
        self._save_config()
        try:
            saved_cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            ck("位置 / 置顶 / 形象 / 大小落盘",
               all(k in saved_cfg for k in ("x", "y", "topmost",
                                            "sprite", "scale")))
        except (OSError, json.JSONDecodeError):
            ck("位置 / 置顶 / 形象记忆落盘", False)

        for name, ok, note in checks:
            print(f"    {'PASS' if ok else 'FAIL'}  {name} {note}")
        self._quit()
        return 0 if all(ok for _, ok, _ in checks) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="桌面虚拟形象（桌宠）原型")
    ap.add_argument("--mock", action="store_true",
                    help="不加载模型，纯本地兜底演示形态与主动提醒（默认）")
    ap.add_argument("--gguf", default=str(ROOT / "output" / "muxin-q4km.gguf"),
                    help="使用真实本地模型；与 --mock 互斥")
    ap.add_argument("--online", action="store_true",
                    help="显式允许读 configs/online.json；默认完全不碰、零请求")
    ap.add_argument("--no-sprite", action="store_true",
                    help="强制用程序化简笔黑猫，不加载 AI 立绘")
    ap.add_argument("--no-scheduler", action="store_true",
                    help="不启动提醒线程（离线自测用）")
    ap.add_argument("--selftest", action="store_true",
                    help="不进 mainloop，跑一次渲染与链路自检")
    args = ap.parse_args()

    if args.selftest:
        # 自测绝不写真实的 memory.json / state_pet.json / configs/pet.json
        stem = Path(tempfile.gettempdir()) / "muxin_pet_selftest"
        state, mem = str(stem) + "_state.json", str(stem) + "_memory.json"
        cfg = str(stem) + "_config.json"
        app = PetApp(mock=True, allow_online=False, autostart=False,
                     state_path=state, memory_path=mem, config_path=cfg,
                     use_sprite=True, action_seed=7)
        rc = app.run_selftest()
        for p in (state, mem, cfg):
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        return rc

    app = PetApp(mock=args.mock, gguf_path=args.gguf,
                 allow_online=args.online, autostart=not args.no_scheduler,
                 use_sprite=not args.no_sprite)
    try:
        app.mainloop()
    except Exception as exc:      # pythonw 无控制台，落一份日志便于排查
        try:
            (ROOT / "logs" / "pet_error.log").write_text(
                f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        except OSError:
            pass
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
