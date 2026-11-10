#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下载基座模型到项目 models 目录（国内走 modelscope 更快）。

用法：
    python scripts/download_model.py
产物：
    models/Qwen/Qwen2.5-1.5B-Instruct/
"""

from pathlib import Path

from modelscope import snapshot_download

ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"


def main():
    target = ROOT / "models" / "Qwen2.5-1.5B-Instruct"
    print(f"[download] {MODEL_ID} -> {target}")
    # 用 local_dir 直接落到目标目录，避免 modelscope 的 cache_dir 多套一层
    try:
        path = snapshot_download(MODEL_ID, local_dir=str(target))
    except TypeError:
        # 旧版 modelscope 不支持 local_dir，退回 cache_dir
        path = snapshot_download(MODEL_ID, cache_dir=str(ROOT / "models"))
    print(f"[done] {path}")


if __name__ == "__main__":
    main()
