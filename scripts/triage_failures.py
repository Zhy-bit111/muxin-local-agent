#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测失败归因：把逐条明细聚类成四类归因 + 三级报告（纯标准库，离线可跑）。

为什么做这个
------------
自动判据只会给 fail / pass / needs_human 三态，但「为什么失败」需要单独归因。
本脚本把 output/eval_responses_<tag>.jsonl 按证据聚类，输出：
  L1 总览：三态分布、回复来源分布、旧规则误判量
  L2 按归因类 / 意图簇聚合，附风险分级
  L3 逐条可复现明细

四类归因（互斥，贴合本项目实际，不套用外部平台口径）
  model_defect     模型输出命中硬伤
  functional_pass  提醒类功能回归自动判过（只证明程序说了话，不证明说得对）
  programmatic_cover 模型没答上来，靠程序化规则 / 兜底顶上（功能通过 ≠ 能力具备）
  needs_human      自动判据判不了，必须人工盲评
另设交叉指标 legacy_false_pass：旧规则会判通过但新判据不判通过，
是旧 95.0% 白送分的直接证据（不占互斥归因，单独统计）。

用法
----
    python scripts/triage_failures.py --input output/eval_responses_style_gguf_dev.jsonl
    python scripts/triage_failures.py --latest --out docs/23-失败归因报告.md
退出码：0 成功 / 1 输入缺失
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "output"
DEFAULT_REPORT = ROOT / "docs" / "23-失败归因报告.md"

ATTRIBUTION = {
    "model_defect": "模型输出缺陷",
    "functional_pass": "功能回归通过",
    "programmatic_cover": "程序兜底覆盖",
    "needs_human": "待人工盲评",
}
RISK = {
    "model_defect": "高",
    "functional_pass": "中",
    "programmatic_cover": "中",
    "needs_human": "低",
}
RISK_LABEL = {"high": "高", "medium": "中", "low": "低"}


def attribute(row: dict) -> str:
    verdict = row.get("verdict")
    if verdict == "fail":
        return "model_defect"
    if verdict == "pass":
        return "functional_pass"
    if row.get("programmatic") or row.get("source") == "programmatic":
        return "programmatic_cover"
    return "needs_human"


def legacy_false_pass(row: dict) -> bool:
    return bool(row.get("legacy_pass")) and row.get("verdict") != "pass"


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build(rows: list[dict], source_name: str) -> str:
    total = len(rows)
    verdicts = Counter(r.get("verdict", "unknown") for r in rows)
    sources = Counter(r.get("source", "unknown") for r in rows)
    attrib = Counter(attribute(r) for r in rows)
    by_cluster: dict[str, Counter] = defaultdict(Counter)
    by_attrib: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        a = attribute(r)
        by_cluster[r.get("cluster", "unknown")][a] += 1
        by_attrib[a].append(r)

    legacy_pass = sum(1 for r in rows if r.get("legacy_pass"))
    legacy_false = sum(1 for r in rows if legacy_false_pass(r))
    L: list[str] = []
    L.append("# 23 · 评测失败归因报告")
    L.append("")
    L.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　来源：`{source_name}`　样本：{total} 条")
    L.append("> 本报告由 `scripts/triage_failures.py` 自动生成，判据口径见 `docs/07-评测口径手册.md`。")
    L.append("")
    L.append("## L1 总览")
    L.append("")
    L.append("| 指标 | 值 |")
    L.append("|---|---|")
    L.append(f"| 样本总数 | {total} |")
    for k in ("pass", "fail", "needs_human"):
        L.append(f"| 三态 · {k} | {verdicts.get(k, 0)} |")
    L.append(f"| 程序化回复 / 模型回复 | {sources.get('programmatic', 0)} / {sources.get('model', 0)} |")
    L.append(f"| 旧规则会判通过 | {legacy_pass} |")
    L.append(f"| 其中新判据不认（白送分） | {legacy_false} |")
    L.append("")
    L.append(f"> **交叉证据**：旧宽松规则会把这 {legacy_false} 条"
             f"（{legacy_false / total * 100:.1f}%）判为通过，")
    L.append(f"> 而新判据只认 {attrib.get('functional_pass', 0)} 条功能回归，其余一律 `needs_human`。")
    L.append("> 这是旧「95.0% 通过率」不可信的直接量化证据，也是引入三态判据的原因。")
    L.append("")
    L.append("## L2 归因聚合与风险分级")
    L.append("")
    L.append("| 归因类 | 条数 | 占比 | 风险 | 判读 |")
    L.append("|---|---:|---:|---|---|")
    reading = {
        "model_defect": "命中可客观验证的硬伤，需修模型或数据",
        "functional_pass": "提醒类功能回归，只证明程序说了话，不证明说得对",
        "programmatic_cover": "回复由程序化规则顶上，不计入模型能力",
        "needs_human": "自动判据给不出结论，必须人工盲评，不等于通过",
    }
    for a, label in ATTRIBUTION.items():
        n = attrib.get(a, 0)
        L.append(f"| {label} | {n} | {n / total * 100:.1f}% | {RISK[a]} | {reading[a]} |")
    L.append("")
    L.append("### 按意图簇")
    L.append("")
    L.append("| 意图簇 | 样本 | 模型缺陷 | 功能回归 | 程序兜底 | 待人工 |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for c, cnt in sorted(by_cluster.items(), key=lambda x: -sum(x[1].values())):
        L.append(f"| {c} | {sum(cnt.values())} | {cnt.get('model_defect', 0)} | "
                 f"{cnt.get('functional_pass', 0)} | {cnt.get('programmatic_cover', 0)} | "
                 f"{cnt.get('needs_human', 0)} |")
    L.append("")
    L.append("## L3 逐条明细（高风险优先）")
    L.append("")
    for a in ("model_defect", "functional_pass", "programmatic_cover", "needs_human"):
        group = by_attrib.get(a, [])
        if not group:
            continue
        L.append(f"### {ATTRIBUTION[a]}（{len(group)} 条）")
        L.append("")
        L.append("| eid | 意图簇 | 题面 | 回复 | 判据 | 硬伤 |")
        L.append("|---:|---|---|---|---|---|")
        for r in group[:30]:
            instr = str(r.get("instruction", "")).replace("|", "\\|")[:34]
            reply = str(r.get("reply", "")).replace("|", "\\|").replace("\n", " ")[:44]
            issues = ", ".join(r.get("issues", [])) or "—"
            L.append(f"| {r.get('eid', '?')} | {r.get('cluster', '?')} | {instr} | {reply} | "
                     f"{r.get('verdict', '?')} | {issues} |")
        if len(group) > 30:
            L.append(f"")
            L.append(f"> 另有 {len(group) - 30} 条，见源 jsonl。")
        L.append("")
    L.append("## 口径边界（面试要说清）")
    L.append("")
    L.append("- `needs_human` **不是通过**：自动判据判不了，不等于模型答对了。")
    L.append("- 程序化回复不计入模型能力，故单列 `source`。")
    L.append("- 本归因基于 `dev_v2.jsonl`（48 条、与训练 0 重叠）与离线统一重算的判据，")
    L.append("  不是生产指标；旧 60 条 dev.jsonl 与 95.0% 已作废，仅作历史对照。")
    L.append("- 归因类别按本项目实际设计，不代表任何外部平台的四类归因口径。")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="指定 eval_responses_<tag>.jsonl")
    ap.add_argument("--latest", action="store_true", help="取 output/ 下最新的 *dev*.jsonl")
    ap.add_argument("--out", default=str(DEFAULT_REPORT), help="报告输出路径")
    args = ap.parse_args()

    if args.input:
        src = ROOT / args.input if not Path(args.input).is_absolute() else Path(args.input)
    elif args.latest:
        cand = sorted(OUT_DIR.glob("eval_responses*dev*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        src = cand[0] if cand else None
    else:
        cand = sorted(OUT_DIR.glob("eval_responses*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        src = cand[0] if cand else None

    if not src or not src.exists():
        print("[fail] 找不到评测明细，请用 --input 指定，或先跑 scripts/eval.py --mock", file=__import__("sys").stderr)
        return 1

    rows = load(src)
    report = build(rows, src.name)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"[ok] 报告已写入 {out}")
    print(f"[ok] 样本 {len(rows)} 条，旧口径误判 {sum(1 for r in rows if legacy_false_pass(r))} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
