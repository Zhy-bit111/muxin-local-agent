#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""清洗种子数据、分层切分 train/valid、与评测集查重、输出统计。

用法：
    python scripts/build_dataset.py
输入：
    data/raw/seed.jsonl
    data/eval/dev.jsonl（可选，不存在则跳过查重）
输出：
    data/processed/train.jsonl
    data/processed/valid.jsonl
    data/processed/stats.txt
"""

import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

random.seed(42)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "seed.jsonl"
EVAL = ROOT / "data" / "eval" / "dev.jsonl"
PROC = ROOT / "data" / "processed"
PROC.mkdir(parents=True, exist_ok=True)

VALID_RATIO = 0.1          # 验证集比例
MAX_OUTPUT_LEN = 120       # 超过此长度的输出判为不合格
CONTAM_THRESHOLD = 0.6     # 与评测样本 5-gram 重叠比例超过 60% 视为污染

PUNCT = re.compile(r"[\s，。！？、,.!?；;：:“”\"'（）()【】\[\]—-]+")


def norm(text: str) -> str:
    return PUNCT.sub("", text.strip()).lower()


def ngrams(text: str, n: int = 5) -> set:
    s = norm(text)
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def load_jsonl(path: Path, require_output: bool = True):
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path.name} 第 {lineno} 行 JSON 解析失败: {e}")
            if not obj.get("instruction"):
                raise ValueError(f"{path.name} 第 {lineno} 行缺少字段 instruction")
            # 评测集的 output 故意留空，由被测模型填写，故不强制非空
            if require_output and not obj.get("output"):
                raise ValueError(f"{path.name} 第 {lineno} 行缺少字段 output")
            obj.setdefault("input", "")
            obj.setdefault("output", "")
            obj.setdefault("category", "未标注")
            rows.append(obj)
    return rows


def main():
    rows = load_jsonl(RAW)
    print(f"[1/6] 读入原始样本: {len(rows)}")

    # ---- 清洗：长度过滤 + 整行去重 ----
    cleaned, seen = [], set()
    dropped_len = dropped_dup = 0
    for r in rows:
        out = r["output"].strip()
        if len(out) > MAX_OUTPUT_LEN:
            dropped_len += 1
            continue
        key = (r["instruction"].strip(), r["input"].strip(), out)
        if key in seen:
            dropped_dup += 1
            continue
        seen.add(key)
        r["output"] = out
        cleaned.append(r)
    print(f"[2/6] 长度过滤剔除 {dropped_len} 条，重复剔除 {dropped_dup} 条，剩余 {len(cleaned)}")

    # ---- 与评测集查重 ----
    # 说明：评测集的 output 留空，因此无法比对"答案重叠"。
    # 这里只防真正的污染：训练样本的「题面」与评测题面归一化后完全相同，
    # 那样评测就等于考原题。不能用 5-gram 交叉比对 instruction，
    # 因为提醒类题面天然同构（"[提醒]该喝水了"），会误杀整个类别。
    eval_rows = load_jsonl(EVAL, require_output=False)
    contaminated = 0
    if eval_rows:
        eval_keys = {norm(r["instruction"] + r["input"]) for r in eval_rows}
        eval_out_keys = {norm(r["output"]) for r in eval_rows if r["output"]}
        kept = []
        for r in cleaned:
            if norm(r["instruction"] + r["input"]) in eval_keys:
                contaminated += 1
                continue
            if eval_out_keys and norm(r["output"]) in eval_out_keys:
                contaminated += 1
                continue
            kept.append(r)
        cleaned = kept
        print(f"[3/6] 评测集 {len(eval_rows)} 条，题面重复剔除 {contaminated} 条")
    else:
        print(f"[3/6] 未找到 {EVAL}，跳过污染检测")

    # ---- 分层切分 ----
    buckets = defaultdict(list)
    for r in cleaned:
        buckets[r["category"]].append(r)

    train, valid = [], []
    for cat, items in buckets.items():
        random.shuffle(items)
        n_valid = max(1, int(len(items) * VALID_RATIO)) if len(items) >= 10 else 0
        valid.extend(items[:n_valid])
        train.extend(items[n_valid:])

    random.shuffle(train)
    random.shuffle(valid)
    print(f"[4/6] 切分完成: train {len(train)} / valid {len(valid)}")

    # ---- 落盘 ----
    for name, data in (("train.jsonl", train), ("valid.jsonl", valid)):
        with (PROC / name).open("w", encoding="utf-8") as f:
            for r in data:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[5/6] 已写出 {PROC}\\train.jsonl 与 valid.jsonl")

    # ---- 统计 ----
    lengths = [len(r["output"]) for r in train]
    cat_cnt = Counter(r["category"] for r in train)
    full_dup = 1 - len({norm(r["output"]) for r in train}) / max(1, len(train))
    lines = [
        "desk-companion 数据集统计",
        f"train: {len(train)} 条",
        f"valid: {len(valid)} 条",
        "",
        "分类分布（train）:",
    ]
    for k, v in sorted(cat_cnt.items(), key=lambda x: -x[1]):
        lines.append(f"  {k}: {v}")
    lines += [
        "",
        "输出长度（字符）:",
        f"  最短 {min(lengths) if lengths else 0}",
        f"  平均 {sum(lengths) / len(lengths):.1f}" if lengths else "  平均 0",
        f"  最长 {max(lengths) if lengths else 0}",
        f"  超 80 字: {sum(1 for x in lengths if x > 80)} 条",
        "",
        f"output 归一化后重复率: {full_dup:.2%}",
        f"评测集污染剔除: {contaminated} 条",
    ]
    stats = "\n".join(lines)
    (PROC / "stats.txt").write_text(stats, encoding="utf-8")
    print(f"[6/6] 统计已写出\n{stats}")


if __name__ == "__main__":
    main()
