#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LoRA adapter → 合并 → GGUF → 量化，一条命令走完。

三步各自单独可跑，出错时按阶段定位：
  1. merge_lora.py        adapter + 基座 → 完整 HF 模型（fp16）
  2. convert_hf_to_gguf.py HF → GGUF 的 f16 版本（约 3.1GB）
  3. llama-quantize.exe   f16 → Q4_K_M（约 1.0GB，CPU 上 10-20 字/秒）

用法：
    python scripts/build_gguf.py --adapter output/lora-v5 --quant Q4_K_M
产物：
    output/muxin-q4km.gguf
"""

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def run(cmd, cwd=None):
    print("> " + " ".join(str(c) for c in cmd))
    t0 = time.time()
    subprocess.run([str(c) for c in cmd], check=True, cwd=cwd)
    print(f"  ({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default="output/lora-v5")
    ap.add_argument("--base", default="models/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--quant", default="Q4_K_M", help="Q4_K_M / Q5_K_M / Q8_0")
    ap.add_argument("--merged", default="output/merged-muxin")
    ap.add_argument("--out", default=None, help="默认 output/muxin-<quant>.gguf")
    ap.add_argument("--keep-f16", action="store_true", help="保留中间 f16 GGUF（3GB）")
    args = ap.parse_args()

    adapter = ROOT / args.adapter
    if not adapter.exists():
        raise SystemExit(f"找不到 adapter: {adapter}")

    out = Path(args.out) if args.out else ROOT / "output" / f"muxin-{args.quant.lower().replace('_', '')}.gguf"
    if not out.is_absolute():
        out = ROOT / out
    merged = ROOT / args.merged
    f16 = ROOT / "output" / "_muxin-f16.gguf"

    convert = ROOT / "tools" / "llama-src" / "llama.cpp-master" / "convert_hf_to_gguf.py"
    quantize_exe = ROOT / "tools" / "llama-cpu" / "llama-quantize.exe"
    for p in (convert, quantize_exe):
        if not p.exists():
            raise SystemExit(f"缺少工具: {p}")

    # ---------- 1. merge ----------
    if (merged / "model.safetensors").exists() or any(merged.glob("*.safetensors")):
        print(f"[1/3] 已存在合并模型，跳过: {merged}")
    else:
        print("[1/3] 合并 LoRA（fp32 加载，内存峰值约 10GB）")
        run([PY, ROOT / "scripts" / "merge_lora.py",
             "--base", args.base, "--adapter", args.adapter, "--out", args.merged])

    # ---------- 2. convert ----------
    print("[2/3] 转 GGUF f16")
    run([PY, convert, merged, "--outfile", f16, "--outtype", "f16"], cwd=convert.parent)

    # ---------- 3. quantize ----------
    print(f"[3/3] 量化 {args.quant}")
    run([quantize_exe, f16, out, args.quant])

    if not args.keep_f16 and f16.exists():
        f16.unlink()
        print(f"  已删除中间文件 {f16.name}")

    print("\n完成:")
    print(f"  GGUF: {out} ({out.stat().st_size / 1e6:.0f} MB)")
    print(f"  用它跑: python -m app.muxin_gui --gguf \"{out}\"")


if __name__ == "__main__":
    main()
