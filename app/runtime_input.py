#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GUI 与评测共用的「输入构造」——单一事实来源。

## 为什么要有这个模块

旧 `scripts/eval.py` 自己拼 `build_user_text(instruction, input)` 且**不传 memory_block**，
而 GUI 的真实链路还会注入记忆块、再走两道程序兜底。两边输入形态不同，
评出来的分数不能代表软件实际表现。本模块把构造步骤抽出来，评测器与 GUI 都调用它；
任何一侧要改输入形态，只改这里一处，避免「两份实现各自漂移」。

## 版本沿革

- **A-2**：如实复刻 GUI 当时的行为，**故意保留双重背景 bug**，用于量出带病基线。
- **B 阶段**（当前）：按 A-3 证据修三处，GUI 与评测同步切换：
  1. 去掉双重背景 —— `user_text` 恒为原句，背景只由 `client.chat` 拼**一次**；
     提醒场景串合并进同一个 `memory_block`
  2. 多轮上下文以真实 `user/assistant` 消息传入（`history`），不再压成
     「刚才聊到…」这种会被 1.5B 当成当前话题的噪声
  3. `Memory.build_block()` 改为按需注入，无相关事实就不给背景

`--input-mode plain` 仍是 A-2 的旧口径（`build_user_text`，无记忆块、无 history），
专供「修前 vs 修后」对照，不要改它。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from app import persona
from app.memory import BLOCK_BUDGET, Memory

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "data" / "eval" / "memory_snapshot.json"


@dataclass
class Turn:
    """一次对话的完整输入。client.chat 只负责把它渲染成 messages。"""

    user_text: str
    memory_block: str
    history: list[tuple[str, str]]
    kind: str
    trace: dict


# ---------------- 唯一构造入口 ----------------

def build_block(memory: Memory | None, query: str) -> str:
    if memory is None:
        return ""
    return memory.build_block(query)


def compose_turn(
    instruction: str,
    context: str = "",
    memory: Memory | None = None,
    history: list[tuple[str, str]] | None = None,
    kind: str = "chat",
    is_reminder: bool = False,
    seed: int | None = None,
) -> Turn:
    """GUI 与评测共用的唯一入口。

    - `user_text` 恒为原始句子（提醒题也不再自带 `（背景：…）`）
    - 提醒场景串 + 相关记忆合并成**一个** `memory_block`，由 client 拼一次背景
    - `history` 是最近若干轮 `(用户话, 慕心回复)`，交给 client 渲染成真实多轮消息
    """
    instr = (instruction or "").strip()
    ctx = (context or "").strip()
    block = build_block(memory, instr)

    pieces = []
    if is_reminder and ctx:
        pieces.append(ctx)
    if block:
        pieces.append(block)
    merged = "；".join(pieces)[:BLOCK_BUDGET]

    hist = [(u or "", b or "") for u, b in (history or []) if (u or "").strip()]
    return Turn(
        user_text=instr,
        memory_block=merged,
        history=hist,
        kind=kind,
        trace={
            "had_context": bool(ctx),
            "had_memory": bool(block),
            "history_turns": len(hist),
            "background_count": 1 if merged else 0,
            "seed": seed,
        },
    )


# 兼容旧调用点（A-2 时期的封装）。保留但不再产生双重背景。
def build_chat_input(user_text: str, memory: Memory | None) -> tuple[str, str]:
    t = compose_turn(user_text, memory=memory, kind="chat")
    return t.user_text, t.memory_block


def build_remind_input(instruction: str, context: str,
                       memory: Memory | None) -> tuple[str, str]:
    t = compose_turn(instruction, context=context, memory=memory,
                     kind="chat", is_reminder=True)
    return t.user_text, t.memory_block


def kind_for_row(row: dict) -> str:
    """复刻 GUI 的 kind 来源。

    GUI 的 kind 由调度器直接给出（water/sleep/...），或从 `_on_send` 固定传 `chat`；
    **从不传 `boundary`**。评测集里提醒题显式带 `kind` 字段，避免靠关键词反推把新问法猜错。
    """
    if row.get("kind"):
        return row["kind"]
    if row.get("cluster") == "remind" or row.get("category") == "提醒":
        return persona.detect_remind_kind(row.get("instruction", ""))
    return "chat"


def postprocess(reply: str, user_text: str, kind: str) -> tuple[str, bool]:
    """复刻 `_gen_worker` 生成后的两道程序兜底，返回 (最终回复, 是否触发兜底)。"""
    raw = reply or ""
    if kind in persona.REMIND_INSTRUCTIONS and kind not in ("chat", "boundary"):
        fixed = persona.ensure_remind_output(kind, raw)
        return fixed, fixed != raw
    if not raw:
        return persona.fallback_reply(kind, user_text), True
    return raw, False


# ---------------- 评测专用：固定、可复现的记忆状态 ----------------

# 评测行的 `input` 字段有三种语义，必须分开处理：
#   （背景：…）  → 长期事实，作为 fact 注入 memory
#   上一轮：…    → 上一轮对话，作为**真实 history** 传入（B 阶段改）
#   其他          → 提醒场景串，合并进 memory_block 的第一段
_FACT_PREFIX = "（背景："
_PREV_PREFIX = "上一轮："


def split_row_context(text: str) -> tuple[str, list[str], list[str]]:
    """返回 (提醒场景串, 事实列表, 上一轮用户话列表)。"""
    text = (text or "").strip()
    if not text:
        return "", [], []
    if text.startswith(_FACT_PREFIX):
        inner = text[len(_FACT_PREFIX):].rstrip("）").strip()
        return "", ([inner] if inner else []), []
    if text.startswith(_PREV_PREFIX):
        inner = text[len(_PREV_PREFIX):].strip()
        return "", [], ([inner] if inner else [])
    return text, [], []


def load_snapshot(path: str | Path | None = None) -> dict:
    p = Path(path) if path else SNAPSHOT
    if not p.exists():
        return {"profile": {}, "facts": [], "episodes": []}
    return json.loads(p.read_text(encoding="utf-8"))


def memory_for_row(snapshot: dict, row: dict) -> Memory:
    """为该题构造一份**只读、不落盘**的记忆状态。

    刻意不把上一题的回复累积进来：真实 GUI 的 memory 跨轮累积，
    若评测也累积，结果会依赖题目顺序、无法复现，还会出现「被上一题带对」的假象。
    """
    data = copy.deepcopy(snapshot)
    _, facts, prevs = split_row_context(row.get("input", ""))
    for f in facts:
        data.setdefault("facts", []).append({"text": f, "ts": "eval", "source": "eval"})
    for u in prevs:
        data.setdefault("episodes", []).append({"user": u, "bot": "", "kind": "chat"})
    return Memory.from_data(data)


def prepare_row(row: dict, snapshot: dict | None, runtime: bool) -> dict:
    """把一条评测行展开成一次真实的模型调用参数。

    - runtime=False：**A-2 旧口径**，`build_user_text(instruction, input)`，
      无 memory_block / 无 history，专供修前修后对照
    - runtime=True ：走 `compose_turn`，与修后 GUI 完全同构
    """
    instr = (row.get("instruction") or "").strip()
    raw_input = (row.get("input") or "").strip()
    kind = kind_for_row(row)
    is_remind = kind in persona.REMIND_INSTRUCTIONS

    if not runtime:
        return {
            "user_text": persona.build_user_text(instr, raw_input),
            "memory_block": "",
            "history": [],
            "kind": kind,
            "memory": None,
            "trace": "plain",
        }

    ctx, _, prevs = split_row_context(raw_input)
    mem = memory_for_row(snapshot if snapshot is not None else load_snapshot(), row)
    history = [(p, "") for p in prevs]
    turn = compose_turn(
        instr, context=ctx, memory=mem, history=history,
        kind=kind, is_reminder=is_remind,
    )
    return {
        "user_text": turn.user_text,
        "memory_block": turn.memory_block,
        "history": turn.history,
        "kind": turn.kind,
        "memory": mem,
        "trace": turn.trace,
    }
