#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跑评测集，输出自动指标与失败样本。

用法：
    python scripts/eval.py                       # 基座 + LoRA adapter
    python scripts/eval.py --no-adapter          # 纯基座（对比基线）
    python scripts/eval.py --mock                # 不加载模型，验证评测脚本本身
产物：
    output/eval_responses.jsonl
    output/eval_report.txt
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm_client import LLMClient   # noqa: E402
from app.persona import build_user_text, detect_remind_kind  # noqa: E402

EVAL_FILE = ROOT / "data" / "eval" / "dev.jsonl"

MAX_LEN = 80
LIST_PATTERN = re.compile(r"(\n\s*[\d一二三四五六七八九]+[.、)）])|(^\s*[-•])", re.M)
SELF_CLAIM = re.compile(r"(人工智能|AI\s*助手|作为.{0,4}助手|语言模型)")

# 提醒类：按指令关键词判定是否说到点子上
# 注意这里的词是"语义等价"，不是"原题字面"：
# 说"屏幕一关就闭眼"就是提醒睡觉，说"别等饿到手抖"就是提醒吃饭。
# 只用字面词判定会把真实有效的输出判成 off_topic，评测器要背这个锅。
REMIND_KEYWORDS = [
    ("喝水", ["水", "喝", "灌", "接一杯", "润"]),
    ("睡觉", ["睡", "关灯", "躺", "手机", "闭眼", "早点", "歇"]),
    ("坐太久", ["站", "走", "活动", "伸", "起身", "动", "腰", "脖子", "椅子"]),
    ("该起来动一下", ["站", "走", "活动", "伸", "起身", "起来", "动", "腰", "脖子"]),
    ("吃饭", ["吃", "饭", "食堂", "垫", "饿"]),
    ("眼睛", ["眼", "远处", "闭", "屏幕", "抬头", "亮"]),
]

# 边界类：先查有没有"应承"，再查有没有"拒绝/能力说明"
# 单字"不/没"太容易命中，会把"答非所问"也算成拒绝，所以加了应承黑名单做前置拦截。
COMPLY_WORDS = ["好的", "行啊", "可以", "没问题", "当然",
                "能啊", "这活儿我干", "我最在行", "我这就", "你说吧"]
REFUSE_WORDS = ["不", "没", "别", "帮不了", "做不了", "干不了", "算不了",
                "猜不了", "查不了", "订不了", "上去", "自己", "得你", "没有"]


def judge(row: dict, reply: str) -> tuple[bool, list[str]]:
    issues = []
    if not reply:
        return False, ["empty"]
    if len(reply) > MAX_LEN:
        issues.append(f"too_long({len(reply)})")
    if LIST_PATTERN.search(reply):
        issues.append("list_format")
    if SELF_CLAIM.search(reply):
        issues.append("self_claim")
    if "建议你" in reply or "建议您" in reply:
        issues.append("preachy")

    cat = row["category"]
    instr = row["instruction"]
    if cat == "提醒":
        kws = []
        for key, words in REMIND_KEYWORDS:
            if key in instr:
                kws = words
                break
        if kws and not any(w in reply for w in kws):
            issues.append("off_topic")
    elif cat == "边界":
        if any(w in reply for w in COMPLY_WORDS):
            # 先应承了，后面再说"不过不行"也已经是应承的语气
            issues.append("no_refusal")
        elif not any(w in reply for w in REFUSE_WORDS):
            issues.append("no_refusal")

    return (len(issues) == 0), issues


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--adapter", default="output/lora-qwen25-1.5b")
    ap.add_argument("--no-adapter", action="store_true")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--tag", default="", help="输出文件后缀（如 base / lora），避免基线与 LoRA 互相覆盖")
    args = ap.parse_args()

    tag = f"_{args.tag}" if args.tag else ""
    out_jsonl = ROOT / "output" / f"eval_responses{tag}.jsonl"
    out_report = ROOT / "output" / f"eval_report{tag}.txt"

    model_path = Path(args.model)
    if not model_path.exists():
        model_path = ROOT / args.model
    if not model_path.exists():
        model_path = ROOT / "models" / "Qwen" / Path(args.model).name
    adapter = None if args.no_adapter else str(ROOT / args.adapter)
    client = LLMClient(str(model_path), adapter_path=adapter, device=args.device, mock=args.mock)

    rows = []
    with EVAL_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    print(f"[eval] {len(rows)} 条评测样本")

    results = []
    stats = defaultdict(lambda: Counter())
    fails = defaultdict(list)

    for i, row in enumerate(rows, 1):
        user_text = build_user_text(row["instruction"], row.get("input", ""))
        if row["category"] == "提醒":
            kind = detect_remind_kind(row["instruction"])
        elif row["category"] == "边界":
            kind = "boundary"
        else:
            kind = "chat"
        reply = client.chat(user_text, kind=kind)
        ok, issues = judge(row, reply)
        results.append({**row, "reply": reply, "pass": ok, "issues": issues})
        stats[row["category"]]["total"] += 1
        stats[row["category"]]["pass"] += int(ok)
        if not ok:
            fails[row["category"]].append((row["instruction"], reply, issues))
        if i % 10 == 0:
            print(f"  {i}/{len(rows)}")

    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with out_jsonl.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    lines = [
        f"评测时间: {datetime.now():%Y-%m-%d %H:%M}",
        f"模式: {'mock' if args.mock else ('基座' if args.no_adapter else '基座+LoRA')}",
        f"样本数: {len(rows)}",
        "",
    ]
    total_pass = total = 0
    for cat, c in stats.items():
        rate = c["pass"] / c["total"] * 100 if c["total"] else 0
        lines.append(f"{cat}: {c['pass']}/{c['total']} 通过率 {rate:.1f}%")
        total_pass += c["pass"]
        total += c["total"]
    overall = total_pass / total * 100 if total else 0
    lines += ["", f"总体通过率: {total_pass}/{total} = {overall:.1f}%", ""]

    for cat, items in fails.items():
        if not items:
            continue
        lines.append(f"--- {cat} 失败样本 ---")
        for instr, reply, issues in items[:5]:
            lines.append(f"[{instr}] -> {reply}  | {','.join(issues)}")
        lines.append("")

    report = "\n".join(lines)
    out_report.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"明细: {out_jsonl}\n报告: {out_report}")


if __name__ == "__main__":
    main()
