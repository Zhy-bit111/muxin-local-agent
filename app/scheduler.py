#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""主动提醒调度器：随机时刻 + 随机品类，全部在本机。

设计原则沿用项目主线：时间与状态归程序，模型只负责把话说好听。
调度器只产出事件（kind / instruction / context），不生成任何自然语言。

2026-11-14 18:5x 改成「随机」。旧版是每 60 分钟准点一句喝水、23:30 准点一句早睡，
像闹钟；现在间隔、品类、早睡时刻都在窗口内随机抽，只保留两条硬约束：
夜间免打扰、两条提醒之间的最小间隔，免得变成轰炸。

**这一层不依赖方案 C。** 不 import 任何在线模块，不看 online.json，断网、只用本地、
甚至模型没加载起来（话术由 persona.FALLBACK 兜住）时，事件照样产生。
"""

from __future__ import annotations

import json
import random
import threading
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from app.persona import REMIND_INSTRUCTIONS

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "configs" / "reminders.json"

DEFAULT_CONFIG = {
    "enabled": True,
    "random": True,
    "interval_min": 40,
    "interval_max": 85,
    "min_gap_min": 20,
    "kinds": ["water", "sit", "eyes", "eat"],
    "kind_weights": {"water": 5, "sit": 2, "eyes": 2, "eat": 1},
    "sleep_window": ["22:50", "23:40"],
    "quiet_hours": ["00:30", "08:00"],
    "frequency": "正常",
}

# 界面上的三档频率，改的是随机抽样的区间（分钟）
FREQUENCY_PRESETS = {
    "松一点": (70, 130),
    "正常": (40, 85),
    "勤一点": (20, 45),
}


def load_config(path=None) -> dict:
    """读本机配置；缺文件、损坏、字段非法一律回退到默认值，不让提醒静默失效。"""
    cfg = dict(DEFAULT_CONFIG)
    p = Path(path) if path else CONFIG_PATH
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if not k.startswith("_")})
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    lo = max(1, int(cfg.get("interval_min") or 40))
    hi = max(lo, int(cfg.get("interval_max") or 85))
    cfg["interval_min"], cfg["interval_max"] = lo, hi
    cfg["min_gap_min"] = max(0, int(cfg.get("min_gap_min") or 20))
    kinds = [k for k in (cfg.get("kinds") or []) if k in REMIND_INSTRUCTIONS]
    cfg["kinds"] = kinds or list(DEFAULT_CONFIG["kinds"])
    if not isinstance(cfg.get("kind_weights"), dict):
        cfg["kind_weights"] = dict(DEFAULT_CONFIG["kind_weights"])
    return cfg


def save_config(cfg: dict, path=None) -> None:
    """把界面上的开关写回本机配置，重启后仍然生效。"""
    p = Path(path) if path else CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    out = {k: v for k, v in cfg.items() if not k.startswith("_")}
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _hhmm(s: str, fallback: str) -> dtime:
    try:
        h, m = (int(x) for x in str(s).split(":")[:2])
        return dtime(hour=h % 24, minute=m % 60)
    except (ValueError, TypeError, IndexError):
        h, m = (int(x) for x in fallback.split(":"))
        return dtime(hour=h, minute=m)


class ReminderScheduler:
    def __init__(
        self,
        state_path: str,
        water_interval_min: int = 60,
        sleep_time: str = "23:30",
        quiet: bool = False,
        config: dict | None = None,
        clock=None,
        rng=None,
        poll_seconds: float = 20,
    ):
        """旧参数 water_interval_min / sleep_time 保留兼容，实际以 config 为准。

        clock / rng / poll_seconds 可注入，供离线冒烟测试快进 24 小时而不用真等。
        """
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg = {**load_config(), **(config or {})}
        self._now = clock or datetime.now
        self.rng = rng or random.Random()
        self.poll_seconds = poll_seconds
        self.quiet = quiet
        self.on_event = None
        self._stop = threading.Event()
        self._thread = None
        self.state = self._load()
        # 本次会话的开始时刻。「连续坐了 X 分钟」只按本次打开软件算，
        # 不能用磁盘上可能已过去几天的 last_water 直接求差（B 阶段修）。
        self.session_start = self._now()

    # ---------- 状态持久化 ----------
    def _load(self) -> dict:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "last_water": None,
            "last_sleep_date": None,
            "last_remind_at": None,
            "next_random_at": None,
            "sleep_date": None,
            "sleep_at": None,
            "count_date": None,
            "today_count": 0,
            "sessions": [],
        }

    def _save(self):
        try:
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            print(f"[scheduler] 状态写入失败：{e}")

    # ---------- 生命周期 ----------
    def prepare(self, on_event):
        """只做准备、不启线程。供离线测试逐帧快进；start() 内部也走这里。"""
        self.on_event = on_event
        now = self._now()
        self.session_start = now
        iso = now.isoformat(timespec="seconds")
        # 每次启动重新计时，绝不拿磁盘上的旧时间戳算时长
        self.state["last_water"] = iso
        self.state["session_start"] = iso
        if self.state.get("count_date") != now.date().isoformat():
            self.state["count_date"] = now.date().isoformat()
            self.state["today_count"] = 0
        self._arm_random(now, keep_plan=True)
        self._arm_sleep(now)
        self._save()

    def start(self, on_event):
        """on_event(kind: str, instruction: str, context: str)"""
        self.prepare(on_event)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def pause(self):
        self.quiet = True

    def resume(self):
        self.quiet = False
        now = self._now()
        self.session_start = now
        iso = now.isoformat(timespec="seconds")
        self.state["last_water"] = iso
        self.state["session_start"] = iso
        # 恢复时重新抽下一次，不补发暂停期间攒下的提醒
        self._arm_random(now)
        self._save()

    # ---------- 主循环 ----------
    def _loop(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.poll_seconds)   # stop() 能立刻唤醒，不必等满 20 秒

    def tick(self, now: datetime | None = None):
        """一次检查。抽出来是为了能在测试里快进，不依赖真实时钟。"""
        now = now or self._now()
        if self.quiet or not self.cfg.get("enabled", True):
            return
        self._check_sleep(now)
        self._check_random(now)

    # ---------- 随机排程 ----------
    def _interval(self) -> int:
        lo, hi = int(self.cfg["interval_min"]), int(self.cfg["interval_max"])
        if not self.cfg.get("random", True):
            return lo
        return self.rng.randint(lo, hi)

    def _arm_random(self, now: datetime, keep_plan: bool = False):
        """抽下一次随机提醒的绝对时刻。

        keep_plan=True 只接受「仍在未来、且不超出最大间隔」的旧计划，
        避免软件重开时把几天前的过期计划补发成一串轰炸。
        """
        if keep_plan:
            try:
                nxt = datetime.fromisoformat(self.state.get("next_random_at") or "")
                if now < nxt <= now + timedelta(minutes=int(self.cfg["interval_max"])):
                    return
            except (ValueError, TypeError):
                pass
        nxt = now + timedelta(minutes=self._interval())
        self.state["next_random_at"] = nxt.isoformat(timespec="seconds")

    def _arm_sleep(self, now: datetime):
        """每天在早睡窗口里随机抽一个时刻；只抽一次，当天不再变。"""
        if self.state.get("sleep_date") == now.date().isoformat():
            return
        win = self.cfg.get("sleep_window") or ["22:50", "23:40"]
        a = _hhmm(win[0], "22:50")
        b = _hhmm(win[-1], "23:40")
        lo, hi = a.hour * 60 + a.minute, b.hour * 60 + b.minute
        if hi < lo:
            hi += 24 * 60
        picked = self.rng.randint(lo, hi) % (24 * 60)
        target = now.replace(hour=picked // 60, minute=picked % 60,
                             second=0, microsecond=0)
        # 深夜/凌晨打开软件：别在人家刚开机时补一句「该睡了」
        if self._in_quiet(now):
            target = None
        elif now > now.replace(hour=b.hour, minute=b.minute, second=0, microsecond=0):
            target = now          # 确实很晚了，开机就轻轻提一句
        self.state["sleep_date"] = now.date().isoformat()
        self.state["sleep_at"] = target.isoformat(timespec="seconds") if target else None

    def _quiet_bounds(self) -> tuple[dtime, dtime]:
        q = self.cfg.get("quiet_hours") or ["00:30", "08:00"]
        return _hhmm(q[0], "00:30"), _hhmm(q[-1], "08:00")

    def _in_quiet(self, now: datetime) -> bool:
        a, b = self._quiet_bounds()
        t = dtime(now.hour, now.minute)
        if a <= b:
            return a <= t < b
        return t >= a or t < b        # 跨零点写法，例如 23:30–08:00

    def _resume_time(self, now: datetime) -> datetime:
        """免打扰结束后再重新开始随机排程。"""
        a, b = self._quiet_bounds()
        end = now.replace(hour=b.hour, minute=b.minute, second=0, microsecond=0)
        if a <= b and dtime(now.hour, now.minute) < a:
            return now
        if end <= now:
            end += timedelta(days=1)
        return end + timedelta(minutes=self.rng.randint(0, max(1, int(self.cfg["interval_min"]) // 2)))

    def _pick_kind(self) -> str:
        kinds = list(self.cfg["kinds"])
        weights = [max(1, int(self.cfg["kind_weights"].get(k, 1))) for k in kinds]
        pick = self.rng.choices(kinds, weights=weights, k=1)[0]
        # 同一类连着来两次不像随口一提，重抽一次即可
        if pick == self.state.get("last_kind") and len(kinds) > 1:
            pick = self.rng.choices(kinds, weights=weights, k=1)[0]
        return pick

    # ---------- 事件 ----------
    def _emit(self, kind: str, instruction: str, context: str):
        if self.on_event:
            try:
                self.on_event(kind, instruction, context)
            except Exception as e:  # 回调异常不能拖垮调度线程
                print(f"[scheduler] 回调异常: {e}")

    def _record(self, kind: str, now: datetime):
        iso = now.isoformat(timespec="seconds")
        self.state["last_remind_at"] = iso
        self.state["last_kind"] = kind
        if kind == "water":
            self.state["last_water"] = iso
        if self.state.get("count_date") != now.date().isoformat():
            self.state["count_date"] = now.date().isoformat()
            self.state["today_count"] = 0
        self.state["today_count"] = int(self.state.get("today_count") or 0) + 1
        self._save()

    def _check_random(self, now: datetime):
        try:
            nxt = datetime.fromisoformat(self.state.get("next_random_at") or "")
        except (ValueError, TypeError):
            self._arm_random(now)
            self._save()
            return
        if now < nxt:
            return
        if self._in_quiet(now):
            self.state["next_random_at"] = self._resume_time(now).isoformat(timespec="seconds")
            self._save()
            return
        gap = int(self.cfg["min_gap_min"])
        last = self.state.get("last_remind_at")
        if gap and last:
            try:
                earliest = datetime.fromisoformat(last) + timedelta(minutes=gap)
                if now < earliest:
                    self.state["next_random_at"] = earliest.isoformat(timespec="seconds")
                    self._save()
                    return
            except (ValueError, TypeError):
                pass

        kind = self._pick_kind()
        minutes = max(0, int((now - self.session_start).total_seconds() // 60))
        context = f"现在是{now.strftime('%H:%M')}，这次已经坐了{minutes}分钟"
        self._emit(kind, REMIND_INSTRUCTIONS.get(kind, REMIND_INSTRUCTIONS["water"]), context)
        self._record(kind, now)
        self._arm_random(now)
        self._save()

    def _check_sleep(self, now: datetime):
        today = now.date().isoformat()
        if self.state.get("last_sleep_date") == today:
            return
        target_s = self.state.get("sleep_at")
        if not target_s:
            return
        try:
            target = datetime.fromisoformat(target_s)
        except (ValueError, TypeError):
            return
        if now < target or self._in_quiet(now):
            return
        self.state["last_sleep_date"] = today
        self.state["sleep_at"] = None
        self._emit("sleep", REMIND_INSTRUCTIONS["sleep"], f"现在是{now.strftime('%H:%M')}")
        self._record("sleep", now)

    # ---------- 界面可调 ----------
    def fire_once(self, kind: str | None = None) -> str | None:
        """「现在来一条」：立刻发一条，并把随机计时重置，避免马上又来一条。"""
        if self.quiet:
            return None
        now = self._now()
        k = kind or self._pick_kind()
        self._emit(k, REMIND_INSTRUCTIONS.get(k, REMIND_INSTRUCTIONS["water"]),
                   f"现在是{now.strftime('%H:%M')}（你点的试一条）")
        self._record(k, now)
        self._arm_random(now)
        return k

    def set_random(self, on: bool):
        self.cfg["random"] = bool(on)
        self._arm_random(self._now())
        self._save()
        save_config(self.cfg)

    def set_frequency(self, name: str):
        if name not in FREQUENCY_PRESETS:
            return
        lo, hi = FREQUENCY_PRESETS[name]
        self.cfg["interval_min"], self.cfg["interval_max"] = lo, hi
        self.cfg["frequency"] = name
        self._arm_random(self._now())
        self._save()
        save_config(self.cfg)

    # ---------- 对外查询 ----------
    def status(self) -> str:
        now = self._now()
        try:
            nxt = datetime.fromisoformat(self.state.get("next_random_at") or "")
            nxt_txt = nxt.strftime("%H:%M")
        except (ValueError, TypeError):
            nxt_txt = "未知"
        mode = "随机" if self.cfg.get("random", True) else "固定"
        span = f"{self.cfg['interval_min']}~{self.cfg['interval_max']} 分钟"
        if not self.cfg.get("random", True):
            span = f"每 {self.cfg['interval_min']} 分钟"
        sleep_txt = "今晚已提醒" if self.state.get("last_sleep_date") == now.date().isoformat() else "今晚会随机提一句"
        return (
            f"提醒状态：{'暂停中' if self.quiet else '运行中'}\n"
            f"  下次提醒：约 {nxt_txt}（{mode} {span}）\n"
            f"  早睡提醒：{sleep_txt}\n"
            f"  今天已主动提醒：{int(self.state.get('today_count') or 0)} 条\n"
            f"  频率：{self.cfg.get('frequency', '正常')} · 夜间免打扰 {self.cfg['quiet_hours'][0]}–{self.cfg['quiet_hours'][1]}"
        )
