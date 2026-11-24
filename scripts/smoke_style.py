#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""风格层冒烟测试：证明「风格变了，但硬伤一个没被洗掉」。

风格是最容易悄悄粉饰评测的地方：把长句截断、`too_long` 就消失；
把「人工智能」删掉、`self_claim` 就消失。本测试逐条堵死这些口子：

1. 只增不减：任何 `polish()` 输出长度 >= 输入长度
2. 同尺子判据：polish 前后 `eval.judge()` 的 verdict 与 issues **完全一致**
3. 历史明细回归：output/eval_responses_*.jsonl 里的全部真实回复逐条复核
4. 回退开关：enable_style=False 时文本逐字不变
5. 频率与互斥：喵/称呼低频出现，且一句里不会同时出现两种装饰

用法：
    ./.venv/Scripts/python.exe scripts/smoke_style.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import style                                   # noqa: E402
from app.engine import respond                          # noqa: E402
from app.runtime_input import Turn                      # noqa: E402


def load_eval_module():
    spec = importlib.util.spec_from_file_location("_eval_for_style", ROOT / "scripts" / "eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    def chat(self, user_text, kind="chat", memory_block="", seed=None, history=None):
        return "老周 这点儿活儿我来弄"


def main() -> int:
    ev = load_eval_module()
    failures: list[str] = []

    print("[0] 配置状态")
    enabled = style.is_enabled()
    print(f"    style_profile.enabled = {enabled}")
    if not enabled:
        print("    FAIL  风格档未启用，本测试没有意义")
        return 1

    # ---------- 1. 定向样本：只增不减 + 判据不变 ----------
    samples = [
        ("老周，起来接杯水吧", "chat", "chat"),
        ("这点儿活儿我帮你弄", "chat", "chat"),
        ("我是一个人工智能助手", "chat", "identity"),
        ("建议你先休息一下", "chat", "chat"),
        ("1. 先喝水 2. 早点睡", "chat", "chat"),
        ("好的，我这就帮你写", "chat", "boundary"),
        ("现在三点半了", "chat", "realtime"),
        ("嗯，我在呢", "chat", "chat"),
        ("该喝水了", "water", "remind"),
        ("字" * 90, "chat", "chat"),
    ]
    print("\n[1] 只增不减 + 同尺子判据不变（定向样本）")
    for text, kind, cluster in samples:
        row = {"instruction": "测试", "input": "", "kind": kind, "cluster": cluster}
        out = style.polish(text, kind=kind, enabled=True)
        v0, i0 = ev.judge(row, text)
        v1, i1 = ev.judge(row, out)
        ok = len(out) >= len(text) and (v0, i0) == (v1, i1)
        if not ok:
            failures.append(f"样本 {text[:12]!r}: 长度 {len(text)}→{len(out)}, "
                            f"判据 {v0}/{i0} → {v1}/{i1}")
        shown = out if len(out) < 30 else out[:26] + "…"
        print(f"    {'PASS' if ok else 'FAIL'}  {text[:14]:<16} -> {shown}")

    # ---------- 2. 称呼等长替换；儿化音不改写（改写会洗掉硬伤，见 18:28 实测） ----------
    print("\n[2] 称呼等长替换 / 儿化音不改写")
    for text, want in (("老周，喝水了", "姐姐，喝水了"),
                       ("这点儿活儿味儿真冲", "这点儿活儿味儿真冲")):
        out = style.polish(text, kind="water", enabled=True)
        ok = out == want and len(out) == len(text)
        print(f"    {'PASS' if ok else 'FAIL'}  {text} -> {out}")
        if not ok:
            failures.append(f"替换不符合预期: {text} -> {out}，期望 {want}")

    # ---------- 3. 历史评测明细全量复核 ----------
    files = sorted((ROOT / "output").glob("eval_responses_*.jsonl"))
    checked = 0
    print(f"\n[3] 历史真实回复复核（{len(files)} 份明细）")
    if not files:
        print("    SKIP  没有历史明细，跳过（不影响其它关）")
    for p in files:
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            reply = r.get("reply") or ""
            out = style.polish(reply, kind=r.get("kind") or "chat", enabled=True)
            checked += 1
            if len(out) < len(reply):
                failures.append(f"{p.name} #{r.get('eid')} 回复被缩短")
            v0, i0 = ev.judge(r, reply)
            v1, i1 = ev.judge(r, out)
            if (v0, i0) != (v1, i1):
                failures.append(f"{p.name} #{r.get('eid')} 判据被风格层改变: "
                                f"{v0}/{i0} → {v1}/{i1} | 原={reply!r} 新={out!r}")
    print(f"    {'PASS' if not any('判据被风格层改变' in f or '回复被缩短' in f for f in failures) else 'FAIL'}"
          f"  复核 {checked} 条真实回复")

    # ---------- 4. 回退开关 ----------
    print("\n[4] enable_style 回退开关")
    turn = Turn(user_text="随便说句", memory_block="", history=[], kind="chat", trace={})
    a = respond(FakeClient(), turn, seed=1, enable_router=False, enable_style=False)
    b = respond(FakeClient(), turn, seed=1, enable_router=False, enable_style=True)
    ok = (not a.styled and a.text == "老周 这点儿活儿我来弄"
          and b.styled and "老周" not in b.text)
    print(f"    {'PASS' if ok else 'FAIL'}  关={'老周 这点儿活儿我来弄'!r} / 开={b.text!r}")
    if not ok:
        failures.append("enable_style 回退开关不生效")

    # ---------- 5. 频率与互斥 ----------
    print("\n[5] 装饰频率（900 句不同文本）")
    outs = [style.polish(f"测试句子编号{i}呀", kind="chat", enabled=True)
            for i in range(900)]
    cat = sum("喵（" in o for o in outs)
    addr = sum(o.startswith(("姐姐，", "主人，")) for o in outs)
    both = sum("喵（" in o and o.startswith(("姐姐，", "主人，")) for o in outs)
    cat_rate, addr_rate = cat / len(outs), addr / len(outs)
    ok = (0 < cat_rate < 0.2 and 0 < addr_rate < 0.2 and both == 0)
    print(f"    {'PASS' if ok else 'FAIL'}  喵 {cat}/900={cat_rate:.1%}（设定 ~11%）"
          f" | 称呼 {addr}/900={addr_rate:.1%}（设定 ~13%） | 同句叠加 {both}")
    if not ok:
        failures.append(f"装饰频率异常: cat={cat_rate:.1%} addr={addr_rate:.1%} both={both}")

    remind = [style.polish("喝口水嘛 别又忘了", kind="water", enabled=True)
              for _ in range(50)]
    ok_remind = all("喵（" not in r and not r.startswith(("姐姐，", "主人，")) for r in remind)
    print(f"    {'PASS' if ok_remind else 'FAIL'}  提醒类 50 次均不加装饰")
    if not ok_remind:
        failures.append("提醒类被加了装饰")

    # ---------- 汇总 ----------
    print("\n" + "=" * 56)
    if failures:
        print(f"FAILED: {len(failures)} 项")
        for f in failures[:20]:
            print("  -", f)
        return 1
    print("ALL PASS：风格生效，硬伤数量不变，可回退，装饰低频且互斥")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
