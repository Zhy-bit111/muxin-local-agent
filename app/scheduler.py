#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定时提醒调度器。

设计原则：时间与状态归程序，模型只负责把话说好听。
调度器只产出事件（kind / instruction / context），不生成任何自然语言。
"""

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path


class ReminderScheduler:
    def __init__(
        self,
        state_path: str,
        water_interval_min: int = 60,
        sleep_time: str = "23:30",
        quiet: bool = False,
    ):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.water_interval = timedelta(minutes=water_interval_min)
        self.sleep_time = sleep_time
        self.quiet = quiet
        self.on_event = None
        self._stop = threading.Event()
        self._thread = None
        self.state = self._load()

    # ---------- 状态持久化 ----------
    def _load(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"last_water": None, "last_sleep_date": None, "sessions": []}

    def _save(self):
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 生命周期 ----------
    def start(self, on_event):
        """on_event(kind: str, instruction: str, context: str)"""
        self.on_event = on_event
        if self.state.get("last_water") is None:
            self.state["last_water"] = datetime.now().isoformat(timespec="seconds")
            self._save()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def pause(self):
        self.quiet = True

    def resume(self):
        self.quiet = False
        self.state["last_water"] = datetime.now().isoformat(timespec="seconds")
        self._save()

    # ---------- 主循环 ----------
    def _loop(self):
        while not self._stop.is_set():
            now = datetime.now()
            if not self.quiet:
                self._check_water(now)
                self._check_sleep(now)
            time.sleep(20)

    def _emit(self, kind: str, instruction: str, context: str):
        if self.on_event:
            try:
                self.on_event(kind, instruction, context)
            except Exception as e:  # 回调异常不能拖垮调度线程
                print(f"[scheduler] 回调异常: {e}")

    def _check_water(self, now: datetime):
        last = self.state.get("last_water")
        if not last:
            return
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            self.state["last_water"] = now.isoformat(timespec="seconds")
            self._save()
            return
        if now - last_dt >= self.water_interval:
            minutes = int((now - last_dt).total_seconds() // 60)
            self.state["last_water"] = now.isoformat(timespec="seconds")
            self._save()
            self._emit(
                "water",
                "[提醒]该喝水了",
                f"现在是{now.strftime('%H:%M')}，已经连续坐了{minutes}分钟",
            )

    def _check_sleep(self, now: datetime):
        today = now.date().isoformat()
        if self.state.get("last_sleep_date") == today:
            return
        hh, mm = (int(x) for x in self.sleep_time.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= target:
            self.state["last_sleep_date"] = today
            self._save()
            self._emit("sleep", "[提醒]该睡觉了", f"现在是{now.strftime('%H:%M')}")

    # ---------- 对外查询 ----------
    def status(self) -> str:
        now = datetime.now()
        last = self.state.get("last_water")
        if last:
            next_water = datetime.fromisoformat(last) + self.water_interval
            water_txt = next_water.strftime("%H:%M")
        else:
            water_txt = "未知"
        sleep_txt = "已暂停" if self.quiet else f"今天 {self.sleep_time}"
        return (
            f"提醒状态：{'暂停中' if self.quiet else '运行中'}\n"
            f"  下次喝水提醒：{water_txt}\n"
            f"  睡觉提醒：{sleep_txt}\n"
            f"  今天已提醒睡觉：{'是' if self.state.get('last_sleep_date') == now.date().isoformat() else '否'}"
        )
