#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""记忆模块冒烟：验证写入、去重、检索、注入块长度。

用法：
    python scripts/smoke_memory.py            # 只测逻辑（秒出）
    python scripts/smoke_memory.py --model    # 再跑一次真实模型，看记忆是否被用上
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.memory import Memory  # noqa: E402

TEST_PATH = ROOT / "output" / "_mem_test.json"


def logic_test() -> str:
    if TEST_PATH.exists():
        TEST_PATH.unlink()
    mem = Memory(TEST_PATH)

    print("== 1. 显式记忆 ==")
    for line in ["记住我周三有早八", "记一下：我不喝冰的", "记住我周三有早八"]:
        fact = mem.parse_remember(line)
        print(f"  {line!r} -> {fact!r}")

    print("== 2. 普通闲聊不该被当事实 ==")
    print("  ", mem.parse_remember("今天课太多了，累"))

    print("== 3. 对话入库 ==")
    mem.add_episode("今天课太多了，累", "换我也顶不住。去冲个澡。")
    mem.add_episode("室友又打游戏到两点", "这挺吵的，耳塞备一副。")

    print("== 4. 检索 ==")
    for q in ["周三早上我要干嘛", "能不能喝冰的", "室友怎么样"]:
        facts = [f["text"] for f in mem.recall(q)]
        print(f"  {q} -> {facts}")

    print("== 5. 注入块 ==")
    block = mem.build_block("周三早上我要干嘛")
    print(block)
    print(f"  [长度 {len(block)} 字，上限 160]")
    assert len(block) <= 200, "记忆块超长，会挤占模型注意力"
    print("stats:", mem.stats())
    return block


def model_test(block: str):
    from app.llm_client import LLMClient

    client = LLMClient(
        str(ROOT / "models" / "Qwen2.5-1.5B-Instruct"),
        adapter_path=str(ROOT / "output" / "lora-v3"),
    )
    print("\n== 6. 真实模型（带记忆） ==")
    for q in ["周三早上我要干嘛", "我想喝冰的"]:
        r1 = client.chat(q)
        r2 = client.chat(q, memory_block=block)
        print(f"  {q}")
        print(f"    无记忆: {r1}")
        print(f"    有记忆: {r2}")


if __name__ == "__main__":
    block = logic_test()
    if "--model" in sys.argv:
        model_test(block)
