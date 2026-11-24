#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方案 A · 程序化能力层冒烟测试。

比「规则有没有生效」更重要的是「规则是不是又在背考题」。所以本测试有四道关：

1. **静态隔离**：router.py 不得出现 `expect`、评测集文件名或任何题面原句
2. **题库命中矩阵**：确定性意图应命中；闲聊/多轮/提醒必须**不**命中（那是模型的活）
3. **题库外同义问法**：没在 dev_v2 / real_usage 出现过的问法也要能命中
4. **同尺子硬伤检查**：程序化回复喂给 scripts/eval.py 的 judge，不得判 fail

用法：
    ./.venv/Scripts/python.exe scripts/smoke_router.py
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import router                                  # noqa: E402
from app.engine import facts_for, respond              # noqa: E402
from app.runtime_input import (                         # noqa: E402
    Turn,
    load_snapshot,
    prepare_row,
)


def load_eval_module():
    spec = importlib.util.spec_from_file_location("_eval_for_smoke", ROOT / "scripts" / "eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    """只用来验证回退开关：记录是否被调用，不真的生成。"""

    def __init__(self):
        self.calls = 0

    def chat(self, user_text, kind="chat", memory_block="", seed=None, history=None):
        self.calls += 1
        return "（模型回复占位）"


def load_rows(name: str) -> list[dict]:
    p = ROOT / "data" / "eval" / f"{name}.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def decide_for_row(row: dict, snapshot: dict, ev):
    prep = prepare_row(row, snapshot, runtime=True)
    facts = facts_for(prep.get("memory"), prep["user_text"])
    d = router.decide(
        prep["user_text"], facts=facts, kind=prep["kind"],
        seed=42, history=prep.get("history", []),
    )
    return prep, d


def main() -> int:
    ev = load_eval_module()
    failures: list[str] = []

    # ---------- 1. 静态隔离 ----------
    # 只检查「代码里的字符串字面量」，注释与 docstring 不算：
    # 文档里当然要写清「不读 dev_v2 / expect」，那是设计说明，不是作弊。
    src = (ROOT / "app" / "router.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docs.add(d)
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value not in docs]
    code_lit = "\n".join(literals)
    meta_hit = [w for w in ("expect", "dev_v2", "real_usage", "eval.py") if w in code_lit]
    # 整句题面被写进规则 = 换一种形式的开卷考；只查「完整相等」，
    # 不查子串（「喵」「几点」这类语义特征是合法的规则词）。
    surfaces = ["现在几点了", "你是谁啊", "帮我把论文摘要翻成英文", "喵一个",
                "明天天气怎么样", "还记得我说过什么吗"]
    surface_hit = [q for q in surfaces if any(lit == q for lit in literals)]
    print("[1] 静态隔离：代码字面量不含评测元数据/完整题面")
    if meta_hit or surface_hit:
        failures.append(f"router.py 静态隔离失败: meta={meta_hit} 题面={surface_hit}")
        print(f"    FAIL  meta={meta_hit} 题面={surface_hit}")
    else:
        print("    PASS  （注释/docstring 中的说明文字不计）")

    # ---------- 2. 题库命中矩阵 ----------
    snapshot = load_snapshot()
    dev = load_rows("dev_v2")
    real = load_rows("real_usage")
    print("\n[2] 题库命中矩阵（seed=42）")
    by_cluster: dict[str, Counter] = {}
    decisions: list[tuple[dict, object, dict]] = []
    for row in dev:
        prep, d = decide_for_row(row, snapshot, ev)
        cl = ev.cluster_of(row)
        by_cluster.setdefault(cl, Counter())["hit" if d else "miss"] += 1
        decisions.append((row, d, prep))
        if d:
            verdict, issues = ev.judge(row, d.reply)
            if verdict == "fail":
                failures.append(f"dev_v2 #{decisions.index((row, d, prep)) + 1} 程序化回复触发硬伤: {issues}")

    want_hit = {"identity", "realtime", "boundary", "memory"}
    for cl in ("identity", "realtime", "boundary", "memory", "context", "chat", "remind"):
        c = by_cluster.get(cl, Counter())
        n = c["hit"] + c["miss"]
        ok = (c["hit"] == n) if cl in want_hit else (c["miss"] == n)
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failures.append(f"{cl} 命中矩阵不符: {dict(c)}")
        print(f"    {mark}  {cl:<10} 命中 {c['hit']:>2}/{n:<2} "
              f"（{'应命中' if cl in want_hit else '应交模型'}）")

    print("\n    真实使用回归集（10 条）")
    for i, row in enumerate(real, 1):
        prep, d = decide_for_row(row, snapshot, ev)
        print(f"      #{i:<2} {row['instruction'][:14]:<16} -> "
              f"{d.rule if d else '模型'}")
        if d:
            verdict, issues = ev.judge(row, d.reply)
            if verdict == "fail":
                failures.append(f"real_usage #{i} 程序化回复触发硬伤: {issues}")

    # ---------- 3. 题库外同义问法 ----------
    # 这些句子都不在 dev_v2 / real_usage 里；规则若能命中，说明学的是语义类别而非题面。
    unseen = [
        ("敢问你是哪位", "identity"),
        ("报一下你的名字呗", "identity"),
        ("你是一只猫？", "identity"),
        ("这会儿几点了呀", "realtime"),
        ("今天星期几来着", "realtime"),
        ("外头下雨没", "realtime"),
        ("帮我把这段译成法语", "boundary"),
        ("帮我预测下期开奖号", "boundary"),
        ("能替我打个电话吗", "boundary"),
        ("我能不能联网搜个东西", "boundary"),
        ("来，学声猫叫", "chat"),
        ("你压根没懂我意思", "chat"),
        ("你说话怎么一股天津味", "chat"),
    ]
    all_texts = {r["instruction"] for r in dev + real}
    overlap = [q for q, _ in unseen if q in all_texts]
    print("\n[3] 题库外同义问法泛化")
    if overlap:
        failures.append(f"泛化样例混入了题库题面: {overlap}")
        print("    FAIL  样例不干净:", overlap)
    passed = 0
    for q, want in unseen:
        d = router.decide(q, facts=[], kind="chat", seed=42)
        ok = d is not None and d.cluster == want
        passed += bool(ok)
        print(f"    {'PASS' if ok else 'FAIL'}  {q:<18} -> {d.rule if d else '未命中(交模型)'}")
    rate = passed / len(unseen)
    print(f"    泛化命中 {passed}/{len(unseen)} = {rate:.0%}（门槛 80%）")
    if rate < 0.8:
        failures.append(f"题库外问法命中率仅 {rate:.0%}，规则疑似过拟合题面")

    # ---------- 4. 记忆题确实复述了事实 ----------
    print("\n[4] 记忆规则复述事实")
    mem_rows = [(r, d) for r, d, _ in decisions if ev.cluster_of(r) == "memory"]
    for r, d in mem_rows:
        keys = ev._background_keys(r)
        ok = bool(d) and any(k in d.reply for k in keys)
        if not ok:
            failures.append(f"记忆题没用上背景: {r['instruction']} -> {d.reply if d else 'None'}")
        print(f"    {'PASS' if ok else 'FAIL'}  {r['instruction'][:12]:<14} -> {d.reply if d else 'None'}")

    # ---------- 5. 可复现性 / 回退开关 ----------
    print("\n[5] 可复现性与回退开关")
    q = "你是谁啊"
    a = router.decide(q, seed=42)
    b = router.decide(q, seed=42)
    ok = a is not None and a == b
    print(f"    {'PASS' if ok else 'FAIL'}  同 seed 结果一致")
    if not ok:
        failures.append("同 seed 结果不一致，评测不可复现")

    turn = Turn(user_text=q, memory_block="", history=[], kind="chat", trace={})
    fc = FakeClient()
    on = respond(fc, turn, memory=None, seed=42, enable_router=True)
    calls_with_router = fc.calls
    off = respond(fc, turn, memory=None, seed=42, enable_router=False)
    calls_without_router = fc.calls
    ok = (on.source == "programmatic" and calls_with_router == 0
          and off.source == "model" and calls_without_router == 1)
    print(f"    {'PASS' if ok else 'FAIL'}  enable_router=False 回退到纯模型"
          f"（开=模型调用 {calls_with_router} 次，关={calls_without_router} 次）")
    if not ok:
        failures.append("回退开关不生效")

    # ---------- 汇总 ----------
    print("\n" + "=" * 56)
    if failures:
        print(f"FAILED: {len(failures)} 项")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL PASS：规则按语义类别生效，未背考题，程序化回复无硬伤，可回退")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
