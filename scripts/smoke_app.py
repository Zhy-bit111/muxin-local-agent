#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M3 验收：真实模型 + LoRA 联调冒烟。

检查两件事：
1) 闲聊 / 提醒的输出是否守格式（不长、不分点、不自称 AI、不说教）
2) 提醒输出是否保住了核心实义词（喝水/睡/坐/饭/眼睛）
   —— 这是评测里唯一的丢分点，必须单独验证是否真的丢了
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm_client import LLMClient                    # noqa: E402
from app.persona import (                                # noqa: E402
    REMIND_INSTRUCTIONS,
    REMIND_CORE_WORDS,
    build_user_text,
)

CHATS = [
    "你好，今天过得怎么样",
    "我有点累，不太想学习",
    "室友太吵了，我静不下心",
    "你平时喜欢干什么",
    "我今天把训练跑完了",
    "晚安",
]

# 提醒核心词统一用 app.persona.REMIND_CORE_WORDS。
# 之前这里另存了一份关键词，和程序侧兜底用的表不一致，导致"功能其实没问题"被报成丢词。

BAD_CLAIMS = ["人工智能", "AI助手", "AI 助手", "语言模型", "智能助手", "大模型", "作为一个AI"]


def check_format(text):
    issues = []
    if any(b in text for b in BAD_CLAIMS):
        issues.append("自称AI")
    if len(text) > 80:
        issues.append("超长%d" % len(text))
    if any(t in text for t in ["1.", "2.", "3.", "- ", "•"]):
        issues.append("分点")
    if "建议你" in text:
        issues.append("说教")
    return issues


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    import torch

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--adapter", default="output/lora-v2")
    ap.add_argument("--no-adapter", action="store_true",
                    help="不挂 adapter，用于验证合并后的完整模型")
    args = ap.parse_args()

    model_dir = Path(args.model)
    if not model_dir.exists():
        model_dir = ROOT / args.model
    adapter = None if args.no_adapter else str(ROOT / args.adapter)

    client = LLMClient(str(model_dir), adapter_path=adapter)
    print("[模式] %s%s" % (model_dir, "" if adapter else "（不挂 adapter，验证合并模型）"))

    total = 0
    ok = 0

    print("\n==== 闲聊 ====")
    for q in CHATS:
        r = client.chat(q, kind="chat")
        issues = check_format(r)
        total += 1
        ok += 0 if issues else 1
        print("用户：%s" % q)
        print("小林：%s" % r)
        print("  -> %s\n" % ("OK" if not issues else "问题：" + "/".join(issues)))

    print("==== 提醒 ====")
    for kind, instr in REMIND_INSTRUCTIONS.items():
        u = build_user_text(instr, "")
        r = client.chat(u, kind=kind)
        issues = check_format(r)
        if not any(w in r for w in REMIND_CORE_WORDS[kind]):
            issues.append("丢核心词")
        total += 1
        ok += 0 if issues else 1
        print("[%s] %s" % (kind, instr))
        print("小林：%s" % r)
        print("  -> %s\n" % ("OK" if not issues else "问题：" + "/".join(issues)))

    print("[小结] 格式+核心词 通过 %d/%d" % (ok, total))
    if torch.cuda.is_available():
        print("[显存] 这轮推理峰值 %.2f GB" % (torch.cuda.max_memory_allocated() / 1024 ** 3))


if __name__ == "__main__":
    main()
