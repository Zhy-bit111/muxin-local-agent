#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 LoRA adapter 合并进基座，导出可直接加载的完整模型。

用法：
    python scripts/merge_lora.py
产物：
    output/merged-qwen25-1.5b/
"""

import argparse
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="models/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--adapter", default="output/lora-qwen25-1.5b")
    ap.add_argument("--out", default="output/merged-qwen25-1.5b")
    args = ap.parse_args()

    base_path = Path(args.base)
    if not base_path.exists():
        base_path = ROOT / args.base
    if not base_path.exists():
        base_path = ROOT / "models" / "Qwen" / Path(args.base).name
    adapter_path = Path(args.adapter)
    if not adapter_path.exists():
        adapter_path = ROOT / args.adapter
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / args.out

    if not adapter_path.exists():
        raise SystemExit(f"找不到 adapter: {adapter_path}")

    print(f"[1/4] 加载基座（fp32 合并更稳）: {base_path}")
    base = AutoModelForCausalLM.from_pretrained(
        str(base_path), torch_dtype=torch.float32, trust_remote_code=True
    )
    tokenizer = AutoTokenizer.from_pretrained(str(base_path), trust_remote_code=True)

    print(f"[2/4] 挂载 adapter: {adapter_path}")
    model = PeftModel.from_pretrained(base, str(adapter_path))

    print("[3/4] 合并并卸载 adapter")
    merged = model.merge_and_unload()

    print(f"[4/4] 导出到: {out_path}")
    out_path.mkdir(parents=True, exist_ok=True)
    merged.half().save_pretrained(str(out_path), safe_serialization=True)
    tokenizer.save_pretrained(str(out_path))
    print("完成。可直接用 --model 指向该目录运行 app/main.py，或转 GGUF。")


if __name__ == "__main__":
    main()
