#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A-1 评测集污染审计（只读，不修改任何数据文件）

把 data/eval/dev.jsonl 的每一条题面，与 seed / train 的全部题面比对：
  - exact     归一化后完全相同
  - contained 一方是另一方的子串（较短方 >= 4 字）
  - similar   字符 2-gram Jaccard >= SIM_THRESHOLD
并额外检查「题面相同且标准答案也相同」的直接泄题情况。

产物：docs/05-评测集污染审计.md（可入库，作为重建留出集的依据）
用法：.venv\\Scripts\\python.exe scripts\\audit_eval_leak.py
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "data" / "eval" / "dev.jsonl"
SEED = ROOT / "data" / "raw" / "seed.jsonl"
TRAIN = ROOT / "data" / "processed" / "train.jsonl"
OUT = ROOT / "docs" / "05-评测集污染审计.md"

SIM_THRESHOLD = 0.60
PREFIX = re.compile(r"^\[[^\]]{1,8}\]\s*")
NOISE = re.compile(r"[\s，。！？、,.!?·:：;；\"'“”‘’()（）\[\]【】0-9a-zA-Z]")


def norm(text: str) -> str:
    """去意图前缀与标点，只留语义主体，用于跨版本题面比对。"""
    return NOISE.sub("", PREFIX.sub("", text or "")).lower()


def bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    ga, gb = bigrams(a), bigrams(b)
    union = ga | gb
    return len(ga & gb) / len(union) if union else 0.0


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def classify(a: str, b: str) -> tuple[str, float]:
    """a=评测题面, b=训练题面，返回 (最严重匹配类型, 相似度)。"""
    if not a or not b:
        return "none", 0.0
    if a == b:
        return "exact", 1.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and short in long_:
        return "contained", similarity(a, b)
    s = similarity(a, b)
    return ("similar", s) if s >= SIM_THRESHOLD else ("none", s)


def main() -> None:
    ap = argparse.ArgumentParser(description="评测集与训练题面重叠审计（只读）")
    ap.add_argument("--eval", default=str(EVAL), help="待审计的评测集 jsonl")
    ap.add_argument("--out", default=str(OUT), help="审计报告输出路径")
    args = ap.parse_args()
    eval_path, out_path = Path(args.eval), Path(args.out)

    evals = load(eval_path)
    seeds = load(SEED)
    trains = load(TRAIN)
    print(f"[audit] eval={len(evals)}({eval_path.name})  seed={len(seeds)}  train={len(trains)}")

    # 训练侧题面索引：去重后保留所有答案，便于查「连答案一起泄」
    index: dict[str, list[dict]] = defaultdict(list)
    for src, rows in (("seed", seeds), ("train", trains)):
        for row in rows:
            key = norm(row.get("instruction", ""))
            if key:
                index[key].append({"src": src, "row": row})
    keys = list(index)
    print(f"[audit] 训练侧去重题面 {len(keys)} 条，开始逐条比对…")

    rank = {"none": 0, "similar": 1, "contained": 2, "exact": 3}
    findings = []
    for ev in evals:
        q = norm(ev.get("instruction", ""))
        best = ("none", 0.0, "")
        exact_hits = []
        for k in keys:
            kind, score = classify(q, k)
            if kind == "exact":
                exact_hits.append(k)
            # 同类匹配里保留相似度最高的那条，方便人工核对
            better = rank[kind] > rank[best[0]] or (
                kind == best[0] and kind != "none" and score > best[1]
            )
            if better:
                best = (kind, score, k)

        # 泄答案检查：题面完全一致时，训练样本的 output 是否就是评测期望
        leaked_answers = []
        for k in exact_hits:
            for item in index[k]:
                out = (item["row"].get("output") or "").strip()
                if out:
                    leaked_answers.append((item["src"], out))

        findings.append({
            "eval": ev,
            "q": q,
            "kind": best[0],
            "score": best[1],
            "match": best[2],
            "exact_count": len(exact_hits),
            "answers": leaked_answers[:3],
        })

    by_kind = Counter(f["kind"] for f in findings)
    by_cat = defaultdict(Counter)
    for f in findings:
        by_cat[f["eval"]["category"]][f["kind"]] += 1

    lines = [
        "# 05 · 评测集污染审计",
        "",
        "> A 阶段第一步。只读审计，不修改任何数据文件。",
        "> 结论用于重建一套与训练零重叠的独立留出集。",
        "",
        "## 口径",
        "",
        f"- 比对对象：评测集 `{eval_path.relative_to(ROOT)}`（{len(evals)} 条）",
        f"- 训练集：`{TRAIN.relative_to(ROOT)}`（{len(trains)} 条）与 `{SEED.relative_to(ROOT)}`（{len(seeds)} 条）",
        "- 归一化：去掉 `[提醒]` / `[边界]` 等意图前缀、去标点空白数字字母、转小写",
        f"- 判定：`exact` 完全相同；`contained` 一方为另一方子串（短方 >= 4 字）；`similar` 字符 2-gram Jaccard >= {SIM_THRESHOLD}",
        "- 只比对 `instruction` 题面；`input` 场景串本次未纳入（后续可扩展）",
        "",
        "## 总览",
        "",
        "| 匹配类型 | 题数 | 占评测集 |",
        "|---|---:|---:|",
    ]
    for kind, label in (("exact", "精确相同"), ("contained", "子串包含"),
                        ("similar", "高相似"), ("none", "未发现重叠")):
        n = by_kind.get(kind, 0)
        lines.append(f"| {label} (`{kind}`) | {n} | {n / max(1, len(evals)) * 100:.1f}% |")
    contaminated = sum(by_kind.get(k, 0) for k in ("exact", "contained", "similar"))
    lines += [
        "",
        f"**被污染的题：{contaminated}/{len(evals)} = {contaminated / max(1, len(evals)) * 100:.1f}%**。"
        + (
            "这些题的分数只能说明「记住了训练样本」，不能说明泛化能力。"
            if contaminated
            else "题面与训练题面无重叠，可作为泛化能力评测的基准（仍需按意图簇人工复核）。"
        ),
        "",
        "## 按类别",
        "",
        "| 类别 | 总数 | exact | contained | similar | 干净 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for cat, c in by_cat.items():
        total = sum(c.values())
        lines.append(
            f"| {cat} | {total} | {c.get('exact', 0)} | {c.get('contained', 0)} | "
            f"{c.get('similar', 0)} | {c.get('none', 0)} |"
        )

    lines += [
        "",
        "## 逐条明细（仅列出发现重叠的题）",
        "",
        "| # | 类别 | 评测题面 | 判定 | 相似度 | 命中的训练题面 | 训练答案（节选） |",
        "|---:|---|---|---|---:|---|---|",
    ]
    for i, f in enumerate(findings, 1):
        if f["kind"] == "none":
            continue
        ev = f["eval"]
        ans = " / ".join(o for _, o in f["answers"]) or "-"
        ans = ans.replace("|", "\\|")[:60]
        lines.append(
            f"| {i} | {ev['category']} | {ev['instruction']} | `{f['kind']}` | "
            f"{f['score']:.2f} | {f['match'][:28]} | {ans} |"
        )

    lines += [
        "",
        "## 干净题面（可暂留作新留出集候选，但需按意图簇复核）",
        "",
    ]
    clean = [f for f in findings if f["kind"] == "none"]
    for f in clean:
        lines.append(f"- [{f['eval']['category']}] {f['eval']['instruction']}")

    lines += [
        "",
        "## 结论与后续动作",
        "",
        "1. 现有 95.0% 不能作为模型能力证据：污染题等于开卷考，干净题量少且同样由模板生成。",
        "2. 重建留出集必须按**意图簇**隔离，不是只删同句：同一意图的变体句也不能同时出现在 train 与 eval。",
        "3. 新留出集应包含真实使用中出现过、而训练数据完全没有的题型（身份认知、多轮追问、时间询问等）。",
        "4. 评测输入必须与 GUI 运行时完全同构（含记忆块、背景拼接方式），详见 A-2。",
        "",
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[audit] 报告已写入 {out_path}")
    print(f"[audit] 污染 {contaminated}/{len(evals)}  "
          f"exact={by_kind.get('exact', 0)} contained={by_kind.get('contained', 0)} "
          f"similar={by_kind.get('similar', 0)} 干净={by_kind.get('none', 0)}")
    for cat, c in by_cat.items():
        dirty = c.get("exact", 0) + c.get("contained", 0) + c.get("similar", 0)
        print(f"  {cat}: {dirty}/{sum(c.values())} 有重叠")


if __name__ == "__main__":
    main()
