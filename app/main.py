#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""主程序：命令行对话 + 后台定时提醒。

用法：
    python app/main.py                    # 用基座（未微调）
    python app/main.py --adapter output/lora-qwen25-1.5b
    python app/main.py --mock             # 不加载模型，只验证调度链路
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm_client import LLMClient            # noqa: E402
from app.persona import build_user_text, ensure_remind_output  # noqa: E402
from app.scheduler import ReminderScheduler      # noqa: E402

DEFAULT_MODEL = "models/Qwen2.5-1.5B-Instruct"


def resolve_model(path_str: str) -> str:
    p = Path(path_str)
    if p.exists():
        return str(p)
    alt = ROOT / path_str
    if alt.exists():
        return str(alt)
    # modelscope 下载后常带组织名目录：models/Qwen/Qwen2.5-1.5B-Instruct
    alt2 = ROOT / "models" / "Qwen" / p.name
    if alt2.exists():
        return str(alt2)
    return path_str  # 交给 transformers 当 HF id 处理


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--adapter", default=str(ROOT / "output" / "lora-v2"),
                    help="默认用 v2（边界样本扩充后的版本，评测最优）")
    ap.add_argument("--no-adapter", action="store_true")
    ap.add_argument("--mock", action="store_true", help="不加载模型，用兜底话术")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--water-interval", type=int, default=60, help="喝水提醒间隔（分钟）")
    ap.add_argument("--sleep-time", default="23:30")
    args = ap.parse_args()

    model_path = resolve_model(args.model)
    adapter = None if args.no_adapter else args.adapter
    client = LLMClient(model_path, adapter_path=adapter, device=args.device, mock=args.mock)

    scheduler = ReminderScheduler(
        state_path=ROOT / "data" / "state.json",
        water_interval_min=args.water_interval,
        sleep_time=args.sleep_time,
    )

    def on_event(kind: str, instruction: str, context: str):
        user_text = build_user_text(instruction, context)
        reply = client.chat(user_text, kind=kind)
        # 提醒内容兜底：模型丢了核心词就换模板句（功能不能靠概率）
        reply = ensure_remind_output(kind, reply)
        print(f"\n[小林 {datetime.now():%H:%M}] {reply}")
        print("> ", end="", flush=True)

    scheduler.start(on_event)

    print("=" * 46)
    print("小林已就位（Ctrl+C 退出）")
    print("  直接说话即可闲聊；/status 看提醒；/quiet 暂停；/quit 退出")
    print(f"  模式：{'mock（不加载模型）' if args.mock else ('基座+LoRA' if adapter else '纯基座')}")
    print("=" * 46)

    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line in ("/quit", "/exit", "exit"):
                break
            if line == "/status":
                print(scheduler.status())
                continue
            if line == "/quiet":
                scheduler.pause()
                print("提醒已暂停。恢复用 /resume")
                continue
            if line == "/resume":
                scheduler.resume()
                print("提醒已恢复。")
                continue
            reply = client.chat(line, kind="chat")
            print(f"[小林] {reply}")
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        print("\n已退出。")


if __name__ == "__main__":
    main()
