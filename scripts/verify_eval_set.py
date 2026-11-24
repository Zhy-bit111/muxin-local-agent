#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测集契约门禁：结构校验 + 零重叠校验（纯标准库，可在 CI 跑）。

为什么要指纹
------------
训练数据 data/raw/*.jsonl 与 data/processed/*.jsonl 体积大且含生成规则，
按 .gitignore 不入库。CI 里若直接跑 audit_eval_leak.py，会读不到训练集，
把「没有可比对象」误判成「零重叠」——这正是本项目踩过的坑（docs/07）。

因此只把训练题面的归一化 sha256 指纹提交到 data/eval/train_fingerprints.json，
CI 用指纹做 exact 门禁；子串包含 / 高相似两类由本地
scripts/audit_eval_leak.py 覆盖，产物见 docs/05-评测集污染审计.md。

用法
----
    python scripts/verify_eval_set.py                      # 校验（CI 用）
    python scripts/verify_eval_set.py --build-fingerprints  # 本地重建指纹
    python scripts/verify_eval_set.py --report              # 输出 Markdown 摘要

退出码：0 通过 / 1 不通过（任一契约违反即失败）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_V2 = ROOT / "data" / "eval" / "dev_v2.jsonl"
EVAL_OLD = ROOT / "data" / "eval" / "dev.jsonl"
FINGERPRINTS = ROOT / "data" / "eval" / "train_fingerprints.json"
RAW_SOURCES = [ROOT / "data" / "raw" / "seed.jsonl", ROOT / "data" / "processed" / "train.jsonl"]

EXPECTED_ROWS = 48
EXPECTED_CLUSTERS = {"identity", "context", "realtime", "boundary", "memory", "chat", "remind"}
VALID_SPLITS = {"generalization", "functional"}
REQUIRED_FIELDS = ("instruction", "expect", "cluster", "split")

PREFIX = re.compile(r"^\[[^\]]{1,8}\]\s*")
NOISE = re.compile(r"[\s，。！？、,.!?·:：;；\"'“”‘’()（）\[\]【】0-9a-zA-Z]")


def norm(text: str) -> str:
    """与 scripts/audit_eval_leak.py 完全一致的归一化口径。"""
    return NOISE.sub("", PREFIX.sub("", text or "")).lower()


def fingerprint(text: str) -> str:
    return hashlib.sha256(norm(text).encode("utf-8")).hexdigest()[:16]


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_fingerprints() -> int:
    seen: dict[str, int] = {}
    for src in RAW_SOURCES:
        for row in load_jsonl(src):
            text = row.get("instruction") or ""
            if not norm(text):
                continue
            fp = fingerprint(text)
            seen[fp] = seen.get(fp, 0) + 1
    payload = {
        "_note": "训练题面归一化后的 sha256 前 16 位指纹，不含原文；由 scripts/verify_eval_set.py --build-fingerprints 生成",
        "_normalization": "去 [意图] 前缀 + 去标点空白数字字母 + 转小写，与 scripts/audit_eval_leak.py 一致",
        "count": len(seen),
        "fingerprints": sorted(seen),
    }
    FINGERPRINTS.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(seen)


def verify() -> tuple[bool, dict]:
    errors: list[str] = []
    rows = load_jsonl(EVAL_V2)

    if len(rows) != EXPECTED_ROWS:
        errors.append(f"评测集行数 {len(rows)} != 契约 {EXPECTED_ROWS}")

    for idx, row in enumerate(rows, 1):
        missing = [f for f in REQUIRED_FIELDS if not row.get(f)]
        if missing:
            errors.append(f"第 {idx} 条缺字段 {missing}")
        if row.get("split") and row["split"] not in VALID_SPLITS:
            errors.append(f"第 {idx} 条 split 非法: {row['split']}")

    clusters = Counter(r.get("cluster") for r in rows)
    splits = Counter(r.get("split") for r in rows)
    absent = EXPECTED_CLUSTERS - set(clusters)
    if absent:
        errors.append(f"意图簇缺失: {sorted(absent)}")

    texts = [norm(r.get("instruction", "")) for r in rows]
    if any(not t for t in texts):
        errors.append("存在归一化后为空的题面")
    dup = [t for t, n in Counter(texts).items() if t and n > 1]
    if dup:
        errors.append(f"评测集内部题面重复 {len(dup)} 组: {dup[:3]}")

    # 零重叠门禁：exact 指纹比对
    fp_payload = json.loads(FINGERPRINTS.read_text(encoding="utf-8")) if FINGERPRINTS.exists() else {}
    train_fps = set(fp_payload.get("fingerprints", []))
    if not train_fps:
        errors.append("train_fingerprints.json 缺失或为空，无法证明零重叠（请本地 --build-fingerprints）")
    hits = [i for i, t in enumerate(texts, 1) if t and fingerprint(t) in train_fps]
    if hits:
        errors.append(f"检出 {len(hits)} 条题面与训练集逐字重叠，行号 {hits[:10]}")

    old_rows = load_jsonl(EVAL_OLD)
    summary = {
        "eval_rows": len(rows),
        "clusters": dict(clusters),
        "splits": dict(splits),
        "train_fingerprints": len(train_fps),
        "exact_overlap": len(hits),
        "old_eval_rows_kept_as_history": len(old_rows),
        "errors": errors,
    }
    return not errors, summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-fingerprints", action="store_true", help="从本地训练集重建指纹文件")
    ap.add_argument("--report", action="store_true", help="输出 Markdown 摘要")
    args = ap.parse_args()

    if args.build_fingerprints:
        n = build_fingerprints()
        print(f"[ok] 已生成 {FINGERPRINTS.relative_to(ROOT)}，去重指纹 {n} 条")
        return 0

    ok, summary = verify()
    if args.report:
        print("| 项 | 值 |")
        print("|---|---|")
        print(f"| 评测集条数 | {summary['eval_rows']} |")
        print(f"| 口径拆分 | {summary['splits']} |")
        print(f"| 意图簇 | {len(summary['clusters'])} 个 |")
        print(f"| 训练题面指纹 | {summary['train_fingerprints']} 条 |")
        print(f"| 逐字重叠 | {summary['exact_overlap']} 条 |")
        print(f"| 结论 | {'通过' if ok else '不通过'} |")
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not ok:
        for e in summary["errors"]:
            print(f"[fail] {e}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
