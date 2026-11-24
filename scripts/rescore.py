#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""不重跑模型，只用当前判据重算已有评测明细（A-3 配套）。

## 为什么需要它

判据是会演进的（例如发现「单字『不』让边界题几乎不判失败」），但三版模型的
生成结果必须留在**同一套判据**下才可比。重跑生成要几小时，而且采样结果会变；
`eval_responses_<tag>.jsonl` 里已经存了 `raw_reply` / `reply` / `user_text` /
`memory_block`，判据只依赖这些字段，所以完全可以离线重算。

用法：
    python scripts/rescore.py --tags v2_base v2_v5fp16 v2_v5gguf --eval dev_v2
    python scripts/rescore.py --tag v2_base --eval dev_v2 --dry-run
产物：原地更新同名 jsonl 的 verdict / issues / legacy_pass，并重写 eval_report_<tag>.txt
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"

# 报告里的模型标识：明细 jsonl 没存模型路径，重算时按 tag 还原，
# 否则报告会退化成 "v2_v5fp16" 这种看不出模型的信息
MODEL_DESC = {
    "v2_base": "no-adapter (Qwen2.5-1.5B-Instruct 基座)",
    "v2_v5fp16": "output/lora-v5 (fp16 基座 + LoRA v5)",
    "v2_v5gguf": "output/muxin-q4km.gguf (Q4_K_M + llama.cpp)",
}


def load_eval_module():
    """按文件路径导入 scripts/eval.py，复用它的 judge / legacy_judge / report。"""
    path = ROOT / "scripts" / "eval.py"
    spec = importlib.util.spec_from_file_location("_eval_module", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rescore(tag: str, ev, eval_path: Path, dry_run: bool) -> dict:
    p = OUT / f"eval_responses_{tag}.jsonl"
    if not p.exists():
        raise FileNotFoundError(p)
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    changed = 0
    for r in rows:
        # A-3 明细还没记录输入形态字段，按当时存下的 user_text / memory_block 补算，
        # 这样修前修后能直接对比「最终 user 消息里有几个（背景：）」
        if "background_count" not in r:
            fu = r["user_text"] + (
                f"\n（背景：{r['memory_block']}）" if r.get("memory_block") else ""
            )
            r["final_user"] = fu
            r["background_count"] = fu.count("（背景：")
            r["history_turns"] = 0
        row = {
            "instruction": r["instruction"],
            "input": r.get("input", ""),
            "expect": r.get("expect", ""),
            "category": r.get("category", ""),
            "cluster": r.get("cluster", ""),
            "split": r.get("split", ""),
            "kind": r.get("kind"),
        }
        verdict, issues = ev.judge(row, r["reply"])
        legacy_ok, _ = ev.legacy_judge(row, r["reply"])
        if (verdict, issues, legacy_ok) != (r["verdict"], r["issues"], r["legacy_pass"]):
            changed += 1
        r["verdict"], r["issues"], r["legacy_pass"] = verdict, issues, legacy_ok

    args = SimpleNamespace(
        gguf=MODEL_DESC.get(tag, tag),
        no_adapter=False,
        adapter=tag,
        mock=False,
        seed=42,
    )
    report = ev.report(rows, args, eval_path)
    report += (
        "\n\n=== 判据重算说明 ===\n"
        "本报告由 scripts/rescore.py 用当前判据离线重算；模型生成结果\n"
        "（raw_reply / reply / user_text / memory_block）未改动，仅\n"
        "verdict / issues / legacy_pass 可能变化。三版共用同一套判据与同一批模型输出。\n"
        f"重算时间：{datetime.now():%Y-%m-%d %H:%M}\n"
    )
    if not dry_run:
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                     encoding="utf-8")
        (OUT / f"eval_report_{tag}.txt").write_text(report, encoding="utf-8")
    return {"tag": tag, "rows": len(rows), "changed": changed, "report": report}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", nargs="+", default=[])
    ap.add_argument("--tag", default="")
    ap.add_argument("--eval", default="dev_v2")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ev = load_eval_module()
    eval_path = ev.resolve_eval(args.eval)
    tags = args.tags or ([args.tag] if args.tag else [])
    if not tags:
        ap.error("需要 --tags 或 --tag")
    for tag in tags:
        r = rescore(tag, ev, eval_path, args.dry_run)
        print(f"[rescore] {tag}: {r['rows']} 条，判据变化 {r['changed']} 条"
              f"{'（dry-run，未写盘）' if args.dry_run else ''}")


if __name__ == "__main__":
    main()
