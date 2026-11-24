#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""风格层：把 `configs/style_profile.json` 落到最终回复上，只加味道、不洗硬伤。

## 为什么单独一层，而且放在 engine 出口

A 阶段立过一条规矩：**GUI 与评测共用 `engine.respond()` 这一个响应入口**。
风格如果只在 GUI 里做，评测量到的就还是旧口吻，又会出现「软件跑的」和「尺子量的」
不是同一条链路（A-2 的 95% 就是这么废掉的）。所以 `polish()` 由 `engine.respond()`
统一调用一次，程序化回复与模型回复都会经过它。

## 一条不能破的边界：只增不减

本模块**不做**这些事，哪怕做了分数会更好看：
- 不截断长句（`too_long` 硬伤必须如实暴露）；
- 不删「建议你 / 人工智能」这类词（`preachy` / `self_claim` 硬伤必须如实暴露）；
- 不合并分点、不吞掉换行、不替换掉模型的失败输出。

理由和「程序化回复不算模型能力」是同一条：把问题藏起来不等于解决问题。
风格层能做的只有三类**不影响任何自动判据**的变换：
1. 禁用称呼按**等长**词替换（老周 → 姐姐）；
2. 低频追加 `喵（神态）` 或句首称呼（只增字，且先确认不会把长度顶过 80 字上限）。

**不做**儿化音/禁词的输出清洗：改词形会让 `no_refusal` 之类的硬伤凭空消失（实测过），
儿化音只在 `SYSTEM_PROMPT` 与话术池的生成端约束。
因为所有变换都不缩短、不替换判据相关词，`too_long` / `self_claim` / `preachy`
等硬伤一个都藏不住。
因此：**开启风格前后，硬伤数量应当完全相同**。这一点由 `scripts/smoke_style.py` 验收。

## 频率怎么做到「偶尔」且可复现

用 `md5(salt|回复文本)` 取模决定要不要加装饰，而不是 `random`：
- 同一句话在 GUI 与评测里结果一致，评测可复现；
- 不同句子分散命中，比例约 1/`cat_every_n`（默认每 9 句一次）；
- 提醒类（`water`/`sleep`/`sit`/`eat`/`eyes`）不加装饰，功能句要清楚。

改口吻只需改 JSON 里的词表和数字，不用动代码、不用重训；`enabled=false` 或
`--no-style` 可整体回退。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "configs" / "style_profile.json"

# 配置缺失/损坏时的内置兜底：宁可朴素，也不能因为一个 JSON 让软件说不出话
FALLBACK_RUNTIME = {
    "target_chars": 40,
    "address_terms": ["姐姐", "主人"],
    "address_every_n": 7,
    "cat_reactions": ["喵（歪头）", "喵（打了个哈欠）", "喵（耳朵一抖）"],
    "cat_every_n": 9,
    "forbidden_address": ["老周"],
    "address_replacement": "姐姐",
    "skip_decoration_kinds": ["water", "sleep", "sit", "eat", "eyes"],
}

# judge() 的 MAX_LEN=80；装饰前留足余量，绝不把一句合格回复顶成 too_long
DECORATE_BUDGET = 74


def load_profile() -> dict:
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"enabled": False, "runtime": FALLBACK_RUNTIME}


def runtime_config() -> dict:
    cfg = dict(FALLBACK_RUNTIME)
    cfg.update(load_profile().get("runtime") or {})
    return cfg


def is_enabled() -> bool:
    return bool(load_profile().get("enabled"))


def _bucket(text: str, salt: str) -> int:
    return int(hashlib.md5(f"{salt}|{text}".encode("utf-8")).hexdigest()[:8], 16)


def _pick(pool: list[str], text: str, salt: str) -> str:
    return pool[_bucket(text, salt) % len(pool)] if pool else ""


def _replace_terms(text: str, cfg: dict) -> str:
    """只做禁用称呼的等长替换，保证 `len(结果) >= len(原文)`。

    为什么不做儿化音替换：18:28 实测，把「这活儿我干不了」改成「这干活我干不了」后，
    原本命中 `no_refusal` 的硬伤凭空消失了 —— 替换虽等长，但改了词形，
    一样是在粉饰评测。所以儿化音只在 `SYSTEM_PROMPT` 与话术池的**生成端**约束，
    输出清洗端不碰模型已经说出口的词。
    """
    replacement = cfg.get("address_replacement") or "姐姐"
    for w in cfg.get("forbidden_address", []):
        if w:
            text = text.replace(w, replacement[:len(w)] if len(replacement) >= len(w) else replacement.ljust(len(w)))
    return text


def polish(text: str, *, kind: str = "chat", enabled: bool | None = None) -> str:
    """对最终回复做一次风格收口。enabled=None 时读配置里的 enabled。"""
    if not text:
        return text
    if enabled is False:
        return text
    try:
        profile = load_profile()
        if enabled is None and not profile.get("enabled"):
            return text
        cfg = dict(FALLBACK_RUNTIME)
        cfg.update(profile.get("runtime") or {})

        out = _replace_terms(text, cfg)

        if kind in (cfg.get("skip_decoration_kinds") or []):
            return out

        # 装饰互斥：一句里最多出现一种，避免「姐姐，……喵（歪头）」变得黏糊
        cat_added = False
        every_cat = max(1, int(cfg.get("cat_every_n", 9)))
        if ("喵（" not in out and len(out) < DECORATE_BUDGET
                and _bucket(out, "cat") % every_cat == 0):
            reaction = _pick(cfg.get("cat_reactions", []), out, "cat_pick")
            tail = " " + reaction
            if reaction and len(out) + len(tail) <= DECORATE_BUDGET:
                out += tail
                cat_added = True

        if not cat_added:
            terms = cfg.get("address_terms", [])
            every_addr = max(1, int(cfg.get("address_every_n", 7)))
            if (terms and not any(w in out for w in terms)
                    and len(out) < DECORATE_BUDGET
                    and _bucket(out, "address") % every_addr == 0):
                term = _pick(terms, out, "address_pick")
                prefix = term + "，"
                if len(out) + len(prefix) <= DECORATE_BUDGET:
                    out = prefix + out
        return out
    except Exception:
        # 风格是锦上添花，任何配置/编码意外都不能让回复消失
        return text
