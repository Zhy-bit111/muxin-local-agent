#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「慕心」打成可以直接发给别人的目录 + zip。

产物：
    dist/慕心/             （双击 慕心.exe 就能用，不需要 Python）
    慕心-portable.zip      （整个目录压成一个包，发给别人解压即可）

为什么这样打包：
- 用 PyInstaller --onedir 而不是 --onefile：onefile 每次启动要把 1GB 模型解到临时目录，
  启动要几十秒，还会重复占磁盘；onedir 启动只等模型加载
- 模型放在 exe 同级的 model/ 下，不打进 PyInstaller 的 _internal，
  这样换模型只需替换一个文件，不用重新打包
- torch / transformers / peft 全部排除：分发版只走 llama.cpp，带上它们会白多 2.5GB
"""

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

EXCLUDES = [
    "torch", "torchvision", "torchaudio", "transformers", "peft", "accelerate",
    "safetensors", "tokenizers", "triton", "sentencepiece", "scipy", "pandas",
    "matplotlib", "notebook", "IPython", "pytest",
]


def run(cmd):
    print("> " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gguf", default="output/muxin-q4km.gguf")
    ap.add_argument("--name", default="慕心")
    ap.add_argument("--no-zip", action="store_true", help="只出目录，不压缩")
    ap.add_argument("--clean", action="store_true", help="先删旧 build/dist")
    ap.add_argument("--zip-only", action="store_true", help="不重新打包，只把已有目录压成 zip")
    args = ap.parse_args()

    gguf = Path(args.gguf)
    if not gguf.is_absolute():
        gguf = ROOT / gguf
    if not gguf.exists():
        raise SystemExit(f"找不到 GGUF 模型: {gguf}\n先跑 scripts/build_gguf.py")

    dist = ROOT / "dist"
    build = ROOT / "build"
    app_dir = dist / args.name
    if args.clean:
        for p in (dist, build):
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)

    if args.zip_only:
        if not app_dir.exists():
            raise SystemExit(f"目录不存在: {app_dir}")
        zpath = dist / f"{args.name}-portable.zip"
        print(f"[zip] 压缩 {app_dir} -> {zpath}")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for f in app_dir.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(dist))
        print(f"      完成: {zpath} ({zpath.stat().st_size / 1e6:.0f} MB)")
        return

    # ---------- 1. PyInstaller ----------
    print("[1/4] PyInstaller 打包（不含 torch 系列）")
    cmd = [
        PY, "-m", "PyInstaller",
        "--noconfirm", "--onedir", "--windowed",
        "--name", args.name,
        "--icon", str(ROOT / "assets" / "muxin.ico"),
        "--add-data", f"{ROOT / 'assets'};assets",
        "--paths", str(ROOT),
        "--distpath", str(dist),
        "--workpath", str(build),
        "--specpath", str(build),
    ]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    cmd.append(str(ROOT / "app" / "muxin_gui.py"))
    run(cmd)

    # ---------- 2. llama.cpp 运行时 ----------
    print("[2/4] 复制 llama.cpp 运行时")
    runtime_src = ROOT / "tools" / "llama-cpu"
    if not runtime_src.exists():
        raise SystemExit(f"找不到 {runtime_src}（先下载 llama.cpp CPU 包并解压）")
    runtime_dst = app_dir / "runtime"
    if runtime_dst.exists():
        shutil.rmtree(runtime_dst)
    shutil.copytree(runtime_src, runtime_dst)

    # ---------- 3. 模型与目录 ----------
    print("[3/4] 放模型与数据目录")
    model_dst = app_dir / "model"
    model_dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(gguf, model_dst / "muxin.gguf")
    (app_dir / "data").mkdir(parents=True, exist_ok=True)

    # 启动引导：写一个 bat，把参数固定好（exe 自己不带参数时也能跑）
    launcher = app_dir / "启动慕心.bat"
    launcher.write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        "start \"\" \"慕心.exe\" --gguf \"%~dp0model\\muxin.gguf\"\r\n",
        encoding="utf-8",
    )

    readme = app_dir / "使用说明.txt"
    readme.write_text(
        "慕心 · 本地运行的桌面陪伴助手\n"
        "================================\n\n"
        "1. 解压到任意目录（不要放在需要管理员权限的地方）\n"
        "2. 双击「启动慕心.bat」或「慕心.exe」\n"
        "3. 第一次启动要加载模型，黑屏等 5-15 秒是正常的\n\n"
        "不需要安装 Python，不需要联网。\n"
        "模型和数据都在这台电脑上，聊天记录存在 data\\memory.json。\n",
        encoding="utf-8",
    )

    # ---------- 4. 压缩 ----------
    total_mb = sum(f.stat().st_size for f in app_dir.rglob("*") if f.is_file()) / 1e6
    print(f"[4/4] 目录大小 {total_mb:.0f} MB -> {app_dir}")
    if not args.no_zip:
        zpath = dist / f"{args.name}-portable.zip"
        print(f"      压缩到 {zpath}（1GB 模型压缩率不高，会有点慢）")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for f in app_dir.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(dist))
        print(f"      完成: {zpath} ({zpath.stat().st_size / 1e6:.0f} MB)")

    print("\n交付物：")
    print(f"  目录: {app_dir}")
    if not args.no_zip:
        print(f"  压缩包: {dist / (args.name + '-portable.zip')}")


if __name__ == "__main__":
    main()
