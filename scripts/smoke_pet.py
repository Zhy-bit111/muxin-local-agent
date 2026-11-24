#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌宠原型 · 离线验收。

只验四件事，别的都不算数：

1. **零网络**：默认不导入在线模块、不发 HTTP 请求；把 urlopen / HTTPConnection
   换成一调用就抛异常，链路仍要能跑通。
2. **不另起炉灶**：回复只走 `app.engine.respond`，输入只走 `compose_turn`，
   主动提醒只走 `ReminderScheduler`；形象层只画画，不碰模型、不读 `expect`。
3. **纯本地也能出话**：`client=None`（mock / 模型没加载 / 断网）时，
   闲聊与提醒都能说出一句本地兜底，且仍经过风格层。
4. **交互约定**：单击延迟待定、双击取消单击并只开聊天、拖拽不误触、
   自主小动作能自动回 idle、位置与形象选择能落盘、立绘能回退成简笔猫。

全程用临时 memory / state / config，绝不写真实的
`data/memory.json`、`data/state_pet.json`、`configs/pet.json`。
"""

from __future__ import annotations

import ast
import json
import queue
import sys
import tempfile
import urllib.request
import warnings
from pathlib import Path
from unittest import mock

warnings.filterwarnings("ignore", category=DeprecationWarning)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import engine, persona, pet_art, style                 # noqa: E402
from app.desktop_pet import PetApp, POKE_LINES, _pick            # noqa: E402
from app.runtime_input import compose_turn                      # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, note: str = ""):
    print(f"    {'PASS' if ok else 'FAIL'}  {name} {note}")
    if not ok:
        failures.append(name)


def main() -> int:
    pet_path = ROOT / "app" / "desktop_pet.py"
    src = pet_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    art_src = (ROOT / "app" / "pet_art.py").read_text(encoding="utf-8")
    art_tree = ast.parse(art_src)
    banned = ("import requests", "import urllib", "import socket", "http.client")

    # ---------- 1. 静态：不另起炉灶 ----------
    print("\n[1] 静态检查（不硬编码题面 / 不复制回复逻辑）")
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                imports[a.asname or a.name] = node.module
    check("回复入口 = app.engine.respond",
          imports.get("respond_once") == "app.engine")
    check("输入构造 = app.runtime_input.compose_turn",
          imports.get("compose_turn") == "app.runtime_input")
    check("主动提醒 = app.scheduler.ReminderScheduler",
          imports.get("ReminderScheduler") == "app.scheduler")
    # 查「实际读取的字段名」，而不是全文搜单词：
    # 注释里写「不读 expect」是说明，不算硬编码题面。
    touched: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            touched.add(node.attr)
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            touched.add(str(node.slice.value))
    check("没有读评测字段 / 题面",
          not ({"expect", "dev_v2", "real_usage", "eval_responses",
                "legacy_pass"} & touched))
    check("桌宠本体不引入网络库", not any(w in src for w in banned))
    top_modules = [n.module for n in tree.body
                   if isinstance(n, ast.ImportFrom) and n.module]
    check("顶层不 import 在线模块",
          not any("online" in m for m in top_modules))
    art_mods = [n.module for n in ast.walk(art_tree)
                if isinstance(n, ast.ImportFrom) and n.module]
    check("形象层不碰模型 / 链路",
          not any(m.startswith(("app.engine", "app.online", "app.router",
                                "app.persona")) for m in art_mods))
    check("形象层不引入网络库", not any(w in art_src for w in banned))
    check("自主小动作 >= 6 种", len(pet_art.IDLE_ACTIONS) >= 6,
          f"n={len(pet_art.IDLE_ACTIONS)}")
    check("两级动作时间表分开（微 / 大）",
          len(pet_art.MICRO_ACTIONS) >= 4 and len(pet_art.MACRO_ACTIONS) >= 4,
          f"micro={len(pet_art.MICRO_ACTIONS)} macro={len(pet_art.MACRO_ACTIONS)}")
    check("帧表配方覆盖全部微 / 大动作",
          all(a in pet_art._pool_specs() for a in pet_art.IDLE_ACTIONS),
          f"pools={len(pet_art._pool_specs())}")

    # ---------- 2. 运行时：网络全封死 ----------
    print("\n[2] 运行时检查（urlopen / HTTPConnection 一调用就抛异常）")
    stem = Path(tempfile.gettempdir()) / "muxin_smoke_pet"
    state, mem = str(stem) + "_state.json", str(stem) + "_memory.json"
    cfg = str(stem) + "_config.json"

    def no_network(*_a, **_kw):
        raise AssertionError("桌宠原型发起了网络请求！")

    app = None
    with mock.patch.object(urllib.request, "urlopen", side_effect=no_network), \
         mock.patch("http.client.HTTPConnection.connect", side_effect=no_network), \
         mock.patch("http.client.HTTPSConnection.connect", side_effect=no_network):
        try:
            app = PetApp(mock=True, allow_online=False, autostart=False,
                         state_path=state, memory_path=mem, config_path=cfg,
                         use_sprite=True, action_seed=7)
            app.update()

            check("默认不加载在线模块", "app.online_client" not in sys.modules)
            check("默认 client=None（纯本地）", app.client is None)
            check("入口对象没有被替换",
                  sys.modules["app.desktop_pet"].respond_once is engine.respond)
            check("风格档仍启用", style.is_enabled())
            fr = app._sprite_frames
            total = 0 if fr is None else sum(len(v) for v in fr.values())
            check("形变帧库已加载（不是旧版 5 帧旋转）",
                  fr is not None and len(fr) >= 10 and total >= 40,
                  f"pools={0 if fr is None else len(fr)} frames={total}")
            check("每个动作都有专属帧池",
                  fr is not None
                  and all(fr.get(a) for a in pet_art.IDLE_ACTIONS)
                  and len(fr.get("idle", [])) >= 8,
                  f"idle={0 if fr is None else len(fr.get('idle', []))}")

            # ---- 交互约定：单击 / 双击 / 拖拽 ----
            poke_before = app._poke_n
            app._click_xy, app._moved = (130, 200), False
            app._release()
            check("单击进入延迟待定", app._click_job is not None)
            app._double()
            check("双击取消待定单击",
                  app._click_job is None and app._poke_n == poke_before,
                  f"poke_n={app._poke_n}")
            check("双击打开聊天窗口",
                  app._chat_win is not None and app._chat_win.winfo_exists())
            if app._chat_win is not None and app._chat_win.winfo_exists():
                app._chat_win.destroy()
            app._click_xy, app._moved = (130, 200), True
            app._release()
            check("拖拽不误触单击", app._click_job is None)

            # ---- 自主小动作状态机 ----
            app._start_action("stretch")
            app._t = app._action_until + 0.01
            app._step_actions()
            check("动作结束自动回 idle", app._action == "idle")
            app._t = app._next_action_at + 0.01
            app._step_actions()
            check("到点随机触发新动作",
                  app._action in pet_art.IDLE_ACTIONS, f"action={app._action}")

            # ---- 形象随时可回退 ----
            saved = app._sprite_frames
            app._sprite_frames = None
            app._draw_cat()
            check("程序化黑猫可回退",
                  len(app.cv.find_withtag("cat")) >= 40,
                  f"items={len(app.cv.find_withtag('cat'))}")
            app._sprite_frames = saved
            app._draw_cat()

            # ---- 大小调节 ----
            from app.desktop_pet import W, H, SCALE_DEFAULT
            app._apply_scale(1.4, quiet=True)
            cw, ch = int(app.cv.cget("width")), int(app.cv.cget("height"))
            check("放大同步改窗口 / 画布", (cw, ch) == (int(W * 1.4), int(H * 1.4)),
                  f"{cw}x{ch}")
            check("立绘帧按新大小重建",
                  app._sprite_frames["idle"][0].width()
                  == int(pet_art.DEFAULT_SPRITE_SIZE * 1.4),
                  f"size={app._sprite_frames['idle'][0].width()}")
            check("帧库按档位缓存，不重复生成", len(app._frame_cache) >= 2,
                  f"cached={len(app._frame_cache)}")
            app._click_xy, app._moved = (int(130 * 1.4), int(200 * 1.4)), False
            app._release()
            check("放大后点击命中区域同步", app._click_job is not None)
            app._cancel_click()
            app._open_size_dialog()
            check("大小设置面板能打开",
                  app._size_win is not None and app._size_win.winfo_exists())
            app._size_win.destroy()
            app._size_lbl = None
            app._wheel(type("E", (), {"delta": 120})())
            check("滚轮调大小进去抖队列",
                  app._scale_job is not None and app._pending_scale > 1.4,
                  f"pending={app._pending_scale:.2f}")
            if app._scale_job:
                app.after_cancel(app._scale_job)
                app._scale_job = None
            app._apply_scale(SCALE_DEFAULT, quiet=True)
            check("可回到默认大小", abs(app._s - SCALE_DEFAULT) < 0.001)

            # ---- 两级动作 / 看你一眼 ----
            app._action = "idle"
            app._next_action_at = app._t + 999
            app._next_micro_at = app._t + 0.01
            app._t += 0.02
            app._step_actions()
            check("高频微动作会触发", app._action in pet_art.MICRO_ACTIONS,
                  f"action={app._action}")
            app._action = "idle"
            app._next_micro_at = app._t + 999
            app._next_glance_at = app._t
            app._mouse_active_until = app._t + 10
            app._mouse_dx = 0.8
            app._step_actions()
            check("鼠标在右边会转头看你", app._action == "glance_right",
                  f"action={app._action}")
            app._mouse_active_until = 0.0

            # ---- 常驻设置落盘 ----
            app._save_config()
            try:
                saved_cfg = json.loads(Path(cfg).read_text(encoding="utf-8"))
                check("位置 / 置顶 / 形象 / 大小落盘",
                      all(k in saved_cfg for k in ("x", "y", "topmost",
                                                   "sprite", "scale")),
                      f"scale={saved_cfg.get('scale')}")
            except (OSError, json.JSONDecodeError):
                check("位置 / 置顶 / 形象 / 大小落盘", False)

            # ---- 纯本地链路 ----
            app._gen_worker("今天有点累", "chat", "")
            kind, payload = app.q.get(timeout=10)
            reply = (payload[1] or "").strip()
            check("mock 闲聊能出本地话", kind == "reply" and bool(reply),
                  f"reply={reply[:26]!r}")
            k = app.scheduler.fire_once()
            check("能手动触发一条提醒", k in persona.REMIND_INSTRUCTIONS, f"kind={k}")
            events = []
            while True:
                try:
                    events.append(app.q.get_nowait())
                except queue.Empty:
                    break
            check("提醒事件进了主线程队列",
                  any(name == "remind" for name, _ in events))
            remind = next(((p[0], p[1], p[2]) for name, p in events
                           if name == "remind"), None)
            if remind:
                rk, instr, ctx = remind
                app._gen_worker(instr, rk, ctx)
                name, p2 = app.q.get(timeout=10)
                check("提醒类在 client=None 时仍出话",
                      name == "reply" and bool((p2[1] or "").strip()),
                      f"reply={p2[1][:26]!r}")

            check("动作反馈是程序短句池",
                  len(POKE_LINES) >= 4 and "喵" in "".join(POKE_LINES))
            check("短句挑选确定性",
                  _pick(POKE_LINES, "same") == _pick(POKE_LINES, "same"))
            app._poke()
            check("点击互动能画气泡",
                  len(app.cv.find_withtag("bubble")) >= 3)

            turn = compose_turn("你是谁", memory=app.memory, kind="chat")
            ans = engine.respond(None, turn, memory=app.memory)
            check("回复仍标记来源", ans.source in ("programmatic", "model"),
                  f"source={ans.source}")
            check("回复仍标记是否过风格层", isinstance(ans.styled, bool),
                  f"styled={ans.styled}")
        except Exception as exc:
            check(f"运行时未抛异常（{type(exc).__name__}: {exc}）", False)
        finally:
            if app is not None:
                app._quit()
            for p in (state, mem, cfg):
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass

    # ---------- 3. 立绘渲染质量 ----------
    print("\n[3] 立绘渲染质量（绒毛渐变 / 色键无紫块）")
    try:
        from PIL import Image

        size = pet_art.DEFAULT_SPRITE_SIZE
        src = Image.open(pet_art.SPRITE_PATH).convert("RGBA")
        bbox = src.getchannel("A").getbbox()
        if bbox:
            src = src.crop(bbox)
        src.thumbnail((size - 6, size - 6), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(src, ((size - src.width) // 2, (size - src.height) // 2))
        alpha_data = list(canvas.getchannel("A").getdata())
        base = pet_art._dither_composite(canvas)
        base_data = list(base.getdata())
        edge = [(a, c) for a, c in zip(alpha_data, base_data)
                if 16 <= a < 120]
        kept = sum(1 for _, c in edge if c != pet_art.CHROMA_RGB)
        ratio = kept / max(1, len(edge))
        check("绒毛边按 alpha 抖动成渐变点阵（非一刀切硬边）",
              0.10 < ratio < 0.90, f"keep={ratio:.2f} edge_px={len(edge)}")

        masks = pet_art._grad_masks(size)
        worst, frames = 0, 0
        for specs in pet_art._pool_specs().values():
            for kw in specs:
                f = pet_art._deform(base, masks, **kw)
                # 纯 CHROMA 是本来就该被精确抠掉的背景，不算残留
                n = sum(1 for c in f.getdata()
                        if c != pet_art.CHROMA_RGB and c[0] > 150
                        and c[2] > 150 and c[1] < 150 and c[2] - c[1] > 40)
                worst, frames = max(worst, n), frames + 1
        check("全部形变帧无亮紫色块 / 洋红硬边", worst < 150,
              f"worst={worst}px / frame, frames={frames}")
        check("眨眼帧确实盖住眼睛",
              len(pet_art._pool_specs()["blink"]) >= 3
              and any(kw.get("close", 0) >= 0.9
                      for kw in pet_art._pool_specs()["blink"]))
    except Exception as exc:
        check(f"渲染质量检查未抛异常（{type(exc).__name__}: {exc}）", False)

    # ---------- 4. 大小配置往返（关掉重开还在） ----------
    print("\n[4] 大小配置往返")
    probe_cfg = Path(tempfile.gettempdir()) / "muxin_pet_scale_probe.json"
    probe = None
    try:
        from app.desktop_pet import W as BASE_W
        probe_cfg.write_text(json.dumps({"scale": 1.5}, ensure_ascii=False),
                             encoding="utf-8")
        stem2 = str(Path(tempfile.gettempdir()) / "muxin_pet_scale_probe")
        probe = PetApp(mock=True, allow_online=False, autostart=False,
                       state_path=stem2 + "_state.json",
                       memory_path=stem2 + "_memory.json",
                       config_path=str(probe_cfg))
        probe.update()
        check("重启读回上次大小",
              abs(probe._s - 1.5) < 0.001
              and int(probe.cv.cget("width")) == int(BASE_W * 1.5),
              f"scale={probe._s} width={probe.cv.cget('width')}")
        check("按记住的大小重建帧库",
              probe._sprite_frames is not None
              and probe._sprite_frames["idle"][0].width()
              == int(pet_art.DEFAULT_SPRITE_SIZE * 1.5))
        probe._quit()
        probe = None
        again = json.loads(probe_cfg.read_text(encoding="utf-8"))
        check("退出时大小仍写回配置", abs(again.get("scale", 0) - 1.5) < 0.001,
              f"scale={again.get('scale')}")
    except Exception as exc:
        check(f"大小往返未抛异常（{type(exc).__name__}: {exc}）", False)
    finally:
        if probe is not None:
            probe._quit()
        for p in (probe_cfg,
                  Path(str(probe_cfg).replace(".json", "_state.json")),
                  Path(str(probe_cfg).replace(".json", "_memory.json"))):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    print("\n" + ("ALL_PASS" if not failures else "FAILED: " + ", ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
