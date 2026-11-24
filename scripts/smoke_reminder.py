#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""随机主动提醒 · 离线自测。

快进 24 小时验证：随机性、免打扰、最小间隔、早睡只发一次、暂停/恢复、
频率档位、手动试一条，以及**不启用方案 C 时整条链路照常出话**。
全程不联网、不加载模型、不写用户配置（save_config 被临时替换为空操作）。
"""

from __future__ import annotations

import random
import sys
import tempfile
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.scheduler as S                                     # noqa: E402
from app.engine import respond                                # noqa: E402
from app.online_client import (                               # noqa: E402
    CapabilityClient, OnlineClient, OnlineConfig,
)
from app.persona import REMIND_CORE_WORDS, REMIND_INSTRUCTIONS  # noqa: E402
from app.runtime_input import compose_turn                     # noqa: E402

S.save_config = lambda cfg, path=None: None      # 测试不得改动 configs/reminders.json

failures: list[str] = []


def check(ok: bool, label: str, detail: str = ""):
    print(f"    {'PASS' if ok else 'FAIL'}  {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f"：{detail}" if detail else ""))


class Clock:
    def __init__(self, start: datetime):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, minutes: int):
        self.t += timedelta(minutes=minutes)


class FakeLocal:
    """模拟本地 1.5B：返回空串，逼程序兜底说话。"""

    def __init__(self):
        self.calls: list[str] = []

    def chat(self, user_text, kind="chat", memory_block="", seed=None, history=None):
        self.calls.append(kind)
        return ""

    def close(self):
        pass


class FakeOnline(OnlineClient):
    """记录调用但绝不发网络请求。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls: list[str] = []

    def chat(self, user_text, kind="chat", **kw):
        self.calls.append(kind)
        return "在线回了一句"


def in_quiet(t: datetime, cfg: dict) -> bool:
    a_h, a_m = (int(x) for x in cfg["quiet_hours"][0].split(":"))
    b_h, b_m = (int(x) for x in cfg["quiet_hours"][1].split(":"))
    a, b = dtime(a_h, a_m), dtime(b_h, b_m)
    cur = dtime(t.hour, t.minute)
    return (a <= cur < b) if a <= b else (cur >= a or cur < b)


print("[1] 快进 24 小时：随机时刻 / 随机品类 / 早睡 / 免打扰")
cfg = S.load_config()
clock = Clock(datetime(2026, 11, 14, 18, 0, 0))
events: list[tuple[datetime, str, str, str]] = []
with tempfile.TemporaryDirectory() as td:
    sched = S.ReminderScheduler(
        str(Path(td) / "state.json"), config=cfg,
        clock=clock.now, rng=random.Random(7), poll_seconds=3600,
    )
    sched.prepare(lambda k, i, c: events.append((clock.now(), k, i, c)))
    for _ in range(24 * 60 // 5):
        clock.advance(5)
        sched.tick()

randoms = [e for e in events if e[1] != "sleep"]
sleeps = [e for e in events if e[1] == "sleep"]
check(8 <= len(randoms) <= 40, "一天内主动提醒条数合理", f"{len(randoms)} 条")
check(len(sleeps) == 1, "早睡提醒当天恰好一次", f"{len(sleeps)} 次")
check(all(not in_quiet(t, cfg) for t, *_ in randoms),
      "夜间免打扰时段零提醒",
      f"quiet={cfg['quiet_hours']}")
gaps = [(b[0] - a[0]).total_seconds() / 60 for a, b in zip(randoms, randoms[1:])]
check(all(g >= cfg["min_gap_min"] - 5 for g in gaps),
      "两条提醒间隔不小于最小间隔",
      f"min={min(gaps):.0f} 分钟，要求 {cfg['min_gap_min']}")
kinds = [e[1] for e in randoms]
check(len(set(kinds)) >= 2, "品类确实随机", f"分布={ {k: kinds.count(k) for k in set(kinds)} }")
check(kinds.count("water") == max(kinds.count(k) for k in set(kinds)),
      "喝水最常出现但不刷屏", f"water={kinds.count('water')}/{len(kinds)}")
check(all(k in REMIND_INSTRUCTIONS for k in kinds), "品类都能生成合法提醒指令")
if sleeps:
    t = sleeps[0][0]
    check(dtime(22, 45) <= dtime(t.hour, t.minute) <= dtime(23, 45),
          "早睡时刻落在配置窗口内", t.strftime("%H:%M"))

print("\n[2] 暂停 / 恢复 / 手动试一条 / 频率档位")
with tempfile.TemporaryDirectory() as td:
    clock = Clock(datetime(2026, 11, 14, 18, 0, 0))
    got: list[str] = []
    sched = S.ReminderScheduler(
        str(Path(td) / "state.json"), config=cfg,
        clock=clock.now, rng=random.Random(11), poll_seconds=3600,
    )
    sched.prepare(lambda k, i, c: got.append(k))
    sched.pause()
    for _ in range(60):       # 5 小时
        clock.advance(5)
        sched.tick()
    check(not got, "暂停期间一条都不发", f"{len(got)} 条")
    sched.resume()
    for _ in range(60):
        clock.advance(5)
        sched.tick()
    check(len(got) >= 1, "恢复后重新开始随机提醒", f"{len(got)} 条")
    n0 = len(got)
    kind = sched.fire_once()
    check(len(got) == n0 + 1 and kind in REMIND_INSTRUCTIONS,
          "「现在来一条」立刻发一条", f"kind={kind}")
    sched.set_random(False)
    check(sched.cfg["random"] is False and sched.cfg["interval_min"] == 40,
          "可切回固定间隔（回退开关可用）")
    sched.set_frequency("勤一点")
    check(sched.cfg["interval_min"] == 20 and sched.cfg["interval_max"] == 45,
          "频率档位生效", f"{sched.cfg['interval_min']}~{sched.cfg['interval_max']} 分钟")
    sched.set_random(True)
    sched.set_frequency("正常")

print("\n[3] 不启用方案 C：整条链路照常，且提醒永不出本机")
local = FakeLocal()
cap = CapabilityClient(local, None)
check(not cap.online, "在线后端为空（方案 C 未启用）")
turn = compose_turn(REMIND_INSTRUCTIONS["water"], context="现在是20:15",
                    kind="water", is_reminder=True)
ans = respond(cap, turn, enable_router=False, enable_style=True)
check(bool(ans.text) and any(w in ans.text for w in REMIND_CORE_WORDS["water"]),
      "本地模型不可用也能说出喝水提醒", f"「{ans.text}」")
check("water" in local.calls and ans.source == "model",
      "确实走了本地后端", f"source={ans.source}")

fake_online = FakeOnline(OnlineConfig(
    base_url="http://127.0.0.1:9/v1", model="fake", api_key="fake", enabled=True
))
local2 = FakeLocal()
cap2 = CapabilityClient(local2, fake_online)
cap2.chat(REMIND_INSTRUCTIONS["sleep"], kind="sleep")
cap2.chat(REMIND_INSTRUCTIONS["eyes"], kind="eyes")
cap2.chat("今天好累啊", kind="chat")
check(fake_online.calls == ["chat"],
      "在线已开时提醒仍强制本地、闲聊照常可外发", f"在线收到 {fake_online.calls}")
check(local2.calls.count("water") == 0 and local2.calls[:2] == ["sleep", "eyes"],
      "硬闸没有误伤闲聊", f"本地收到 {local2.calls}")

print("\n[4] 重开软件不补发历史提醒")
with tempfile.TemporaryDirectory() as td:
    p = str(Path(td) / "state.json")
    clock = Clock(datetime(2026, 11, 14, 10, 0, 0))
    a = S.ReminderScheduler(p, config=cfg, clock=clock.now,
                            rng=random.Random(3), poll_seconds=3600)
    a.prepare(lambda *e: None)
    import json
    stale = json.loads(Path(p).read_text(encoding="utf-8"))
    stale["next_random_at"] = (clock.now() - timedelta(days=3)).isoformat(timespec="seconds")
    Path(p).write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    clock.t = datetime(2026, 11, 17, 10, 0, 0)
    got2: list[str] = []
    b = S.ReminderScheduler(p, config=cfg, clock=clock.now,
                            rng=random.Random(3), poll_seconds=3600)
    b.prepare(lambda k, i, c: got2.append(k))
    b.tick()
    check(not got2, "三天前的过期计划不会被补发成轰炸", f"{len(got2)} 条")

print("\n" + ("=" * 46))
if failures:
    print(f"FAILED: {len(failures)} 项")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL PASS：随机提醒离线自测全部通过（零网络请求）")
