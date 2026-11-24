#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v4 验收：记忆是否真的被用上 + 猫味浓度。

只做两件事，都是"看输出"就行，不自动打分（打分容易自欺）：
  1. 同一问题上「无背景 / 有背景」并排，看回复里有没有出现背景里的信息
  2. 随机 8 条闲聊，数有多少条带猫的表达、有没有句句喵
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm_client import LLMClient  # noqa: E402

CASES = [
    ("周三早上我要干嘛", "他周三有早八，八点开始"),
    ("我想喝冰的", "他胃不好，不喝冰的"),
    ("我报告写完没", "他实验报告还差结果分析没写"),
    ("夜宵能吃吗", "他最近在控制体重，晚上不吃夜宵"),
    ("我几点睡比较好", "他一般十二点左右睡"),
]

CHATS = [
    "今天好累",
    "我考砸了",
    "周末想出去玩",
    "我是不是该休息了",
    "晚上要不要熬夜",
    "我最近有点迷茫",
    "我买了个新键盘",
    "我又长胖了",
]


def main():
    adapter = sys.argv[1] if len(sys.argv) > 1 else "output/lora-v4"
    c = LLMClient(str(ROOT / "models" / "Qwen2.5-1.5B-Instruct"), adapter_path=str(ROOT / adapter))

    print("=== 1. 记忆 A/B（看回复里是否出现背景信息）===")
    for q, bg in CASES:
        a = c.chat(q)
        b = c.chat(q, memory_block=bg)
        print(f"[Q] {q}")
        print(f"   背景: {bg}")
        print(f"   无: {a}")
        print(f"   有: {b}")
        print()

    print("=== 2. 猫味抽检（闲聊）===")
    cat_hits = 0
    for q in CHATS:
        r = c.chat(q)
        hit = ("猫" in r or "尾巴" in r or "爪子" in r or "胡须" in r
               or "晒太阳" in r or "呼噜" in r or "纸箱" in r or "哈欠" in r
               or "趴" in r or "舔" in r or "喵" in r)
        cat_hits += 1 if hit else 0
        print(f"[Q] {q} -> {r}{'   <=猫' if hit else ''}")
    print(f"\n猫味命中: {cat_hits}/{len(CHATS)}")


if __name__ == "__main__":
    main()
