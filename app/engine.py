#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方案 A · 回复引擎：GUI 与评测**唯一**共用的回复入口。

## 为什么必须有这一层

A-2 的教训是「评测和软件跑两套输入」让 95% 变成废分；B 阶段修好后，
`runtime_input.compose_turn()` 把输入构造收成一处。方案 A 又引入一条新分叉
（程序化回复 vs 模型回复），如果 GUI 自己判断、评测器自己判断，分分钟再次漂移。

所以：**任何一侧要拿到一句回复，只能调 `respond()`**。
它先问 `router.decide()`，命中就返回程序化回复并标记来源；
未命中才调 `client.chat()` 并走 `postprocess()` 兜底。
调用方一律通过 `Reply.source` 知道这句话是谁说的。

`--no-router` / `enable_router=False` 可以整体回退到 B 阶段的纯模型链路，
用于验证「程序层到底带来多少变化」以及随时撤销。
"""

from __future__ import annotations

from dataclasses import dataclass

from app import persona, router, style
from app.runtime_input import Turn, postprocess


@dataclass(frozen=True)
class Reply:
    text: str
    source: str                 # "programmatic" | "model"
    rule: str                   # 命中的规则名，模型回复为空
    cluster: str                # 规则判定的意图簇，模型回复为空
    raw_reply: str              # 模型裸输出；程序化回复为空串
    fallback_used: bool         # 是否经过 ensure_remind_output / fallback_reply
    styled: bool = False        # 是否经过风格层（可 --no-style 回退）


def facts_for(memory, query: str) -> list[str]:
    """取一条最相关的事实交给路由层判断；无记忆或无命中返回空列表。

    刻意只取 1 条：方案 A 要的是确定性复述，塞多条会让句子又长又跑题。
    """
    if memory is None:
        return []
    try:
        picked = memory.recall(query, limit=1)
    except Exception:
        return []
    return [(f.get("text") or "").strip() for f in picked if (f.get("text") or "").strip()]


def respond(
    client,
    turn: Turn,
    *,
    memory=None,
    seed: int | None = None,
    enable_router: bool = True,
    enable_style: bool | None = None,
) -> Reply:
    """生成一句回复。GUI、`scripts/eval.py`、冒烟测试都走这里。

    `enable_style=None` 读 `configs/style_profile.json`；GUI/eval 的 `--no-style`
    传 False 即可整体回退到风格落地前的口吻，用于同尺子对照。
    """
    is_remind = turn.kind in persona.REMIND_INSTRUCTIONS and turn.kind not in ("chat", "boundary")
    style_on = style.is_enabled() if enable_style is None else enable_style

    def finish(text: str) -> str:
        # 唯一出口：程序化与模型回复都过同一层风格，避免出现「GUI 像、评测不像」
        return style.polish(text, kind=turn.kind, enabled=True) if style_on else text

    if enable_router and not is_remind:
        decision = router.decide(
            turn.user_text,
            facts=facts_for(memory, turn.user_text),
            kind=turn.kind,
            seed=seed,
            history=turn.history,
        )
        if decision is not None:
            return Reply(
                text=finish(decision.reply),
                source="programmatic",
                rule=decision.rule,
                cluster=decision.cluster,
                raw_reply="",
                fallback_used=False,
                styled=style_on,
            )

    raw = ""
    if client is not None:
        raw = client.chat(
            turn.user_text,
            kind=turn.kind,
            memory_block=turn.memory_block,
            seed=seed,
            history=turn.history,
        )
    text, fallback_used = postprocess(raw, turn.user_text, turn.kind)
    return Reply(
        text=finish(text),
        source="model",
        rule="",
        cluster="",
        raw_reply=raw or "",
        fallback_used=fallback_used,
        styled=style_on,
    )
