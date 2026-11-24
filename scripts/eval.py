#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跑评测集：输出客观硬伤、程序兜底统计与人工盲评素材（A-2 重写）。

## 这版和旧版最大的区别

旧版 `judge` 只看非空 / 长度 / 分点 / 自称 AI / 提醒关键词 / 是否出现「不」，
对闲聊、身份、多轮、实时、记忆**没有任何语义判据**，等于白送分；
再加上 45% 的题面泄题，旧「95% 通过率」不能代表软件能用。

新版的原则：**判不了就别装作判得了**。
- 自动判据只检查可以客观验证的硬伤（空、超长、分点、自称 AI、编号时间、
  明确应承、完全没用上背景、提醒核心词缺失），命中即 `fail`
- 语义好不好（接没接住情绪、多轮有没有串起来、身份答得像不像）一律标
  `needs_human`，导出人工盲评表由人打分
- 只有「提醒」这类程序固定触发的功能回归可以自动判 `pass`
- **不再输出「总体通过率」**；旧规则会判多少分只作为历史对照单列

## 输入同构

`--input-mode runtime` 走 `app/runtime_input.py` + `app/engine.py`，与 GUI 完全同一条链路：
聊天原句 + `Memory.build_block()` 记忆块 + 真实多轮 history + 生成后的
`ensure_remind_output()` / `fallback_reply()` 后处理。
`plain` 是 A-2 旧口径，**刻意绕过引擎**以复现历史行为，仅供对照。

C 阶段方案 A 之后，runtime 的回复有两个来源：程序化规则 / 模型。
明细里记录 `source` / `rule` / `programmatic`，报告单列命中率；
**程序化回复不算模型能力**，`--no-router` 可整体回退到 B 阶段纯模型链路。

用法：
    python scripts/eval.py --mock                                  # 不加载模型，自检评测器
    python scripts/eval.py --tag v5gguf --gguf output/muxin-q4km.gguf --input-mode both
    python scripts/eval.py --tag base --no-adapter --input-mode both
    python scripts/eval.py --tag v5fp16 --adapter output/lora-v5 --input-mode both
    python scripts/eval.py --eval dev --tag legacy --input-mode plain   # 复现旧口径

产物（output/）：
    eval_responses_<tag>.jsonl   逐条明细（含 user_text / memory_block / raw_reply）
    eval_report_<tag>.txt        分组统计与硬伤明细
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import persona                                # noqa: E402
from app.engine import respond as respond_once         # noqa: E402
from app.llm_client import LLMClient                   # noqa: E402
from app.runtime_input import (                        # noqa: E402
    Turn,
    load_snapshot,
    prepare_row,
    split_row_context,
)

EVAL_DIR = ROOT / "data" / "eval"

MAX_LEN = 80
LIST_PATTERN = re.compile(r"(\n\s*[\d一二三四五六七八九]+[.、)）])|(^\s*[-•])", re.M)
SELF_CLAIM = re.compile(r"人工智能|AI\s*助手|作为.{0,4}助手|作为一个?\s*AI|语言模型")
# 「我不会像人工智能那样说话」是否定语境，不是自称 AI，别误伤
SELF_NEG_BEFORE = re.compile(r"(不是|不像|不会像|不会|并非|别当|没说|非)[^，。！？]{0,6}$")

# 明确应承词：出现即视为「答应了」，后面再解释「不过不行」也已经是应承语气。
# 这里**只用正向证据**；不再用「没找到拒绝词就判失败」的反向推断，
# 因为「我哪能知道」「你自己来」「得多问问老师」都是有效拒绝却不含任何拒绝关键词。
COMPLY_WORDS = ["好的", "行啊", "可以", "没问题", "当然", "能啊",
                "这活儿我干", "我最在行", "我这就", "你说吧", "马上", "稍等"]
NEG_BEFORE = re.compile(r"(不|没|别|非|岂|哪能|怎么可能)[^，。！？]{0,2}$")

# 仅供 legacy_judge 复现旧分数，不参与新判据
_LEGACY_REFUSE_WORDS = ["不", "没", "别", "帮不了", "做不了", "干不了", "算不了",
                        "猜不了", "查不了", "订不了", "发不了", "写不了", "自己", "得你"]


def _has_word(text: str, words: list[str], check_neg: bool = False) -> bool:
    """查词，可选排除前面紧跟否定词的命中（如「不可以」「不是人工智能」）。"""
    for w in words:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            if not check_neg or not NEG_BEFORE.search(text[max(0, i - 4):i]):
                return True
            start = i + len(w)
    return False


def _self_claim(reply: str) -> bool:
    for m in SELF_CLAIM.finditer(reply):
        pre = reply[max(0, m.start() - 12):m.start()]
        if not SELF_NEG_BEFORE.search(pre):
            return True
    return False

# 实时类硬伤：编造具体时间 / 星期 / 天气
# 中文数字也要认：「现在三点半」「下午十二点左右」同样是编造
_CN = "〇零一二三四五六七八九十两"
CLOCK = re.compile(
    r"\d{1,2}\s*[:：]\s*\d{2}"
    rf"|(?:\d{{1,2}}|[{_CN}]{{1,3}})\s*点(?:半|整|左右|[〇零一二三四五六七八九十两\d]{{1,3}}分?)?"
)
WEEKDAY = re.compile(r"周[一二三四五六日天]|星期[一二三四五六日天]|礼拜[一二三四五六日天]")
WEATHER = re.compile(r"(今天|明天|外面|窗外).{0,6}(晴|阴|下雨|下雪|多云|气温|度)")
NO_CLAIM = ["不知道", "看不到", "看不了", "查不了", "查不到", "没法", "不能",
            "不清楚", "没看", "自己看", "我不", "猜不", "说不准", "不确定"]

# 只作为人工复核提示、不直接判失败的信号。
# 「没用上背景」无法客观判定：模型可能正确用同义改写复述了背景，
# 硬判为失败会误伤（单元检查里 #28「结果分析没写」就会被冤枉）。
WARN_ONLY = {"background_maybe_unused"}

# 历史口径复现用（**不参与新判据**，只为在报告里对比「旧规则会判多少分」）。
# 真实判据的提醒词表唯一来源是 persona.REMIND_CORE_WORDS。
_LEGACY_REMIND_KEYWORDS = [
    ("喝水", ["水", "喝", "灌", "接一杯", "润"]),
    ("睡觉", ["睡", "关灯", "躺", "手机", "闭眼", "早点", "歇"]),
    ("坐太久", ["站", "走", "活动", "伸", "起身", "动", "腰", "脖子", "椅子"]),
    ("该起来动一下", ["站", "走", "活动", "伸", "起身", "起来", "动", "腰", "脖子"]),
    ("吃饭", ["吃", "饭", "食堂", "垫", "饿"]),
    ("眼睛", ["眼", "远处", "闭", "屏幕", "抬头", "亮"]),
]

CLUSTER_LABEL = {
    "identity": "身份", "context": "多轮", "realtime": "实时",
    "boundary": "边界", "memory": "记忆", "chat": "闲聊", "remind": "提醒",
}


def cluster_of(row: dict) -> str:
    if row.get("cluster"):
        return row["cluster"]
    return {"提醒": "remind", "边界": "boundary", "闲聊": "chat"}.get(
        row.get("category", ""), "chat"
    )


def split_of(row: dict) -> str:
    return row.get("split") or "legacy"


# ---------------- 判据 ----------------

def _common_issues(reply: str) -> list[str]:
    issues = []
    if not reply:
        return ["empty"]
    if len(reply) > MAX_LEN:
        issues.append(f"too_long({len(reply)})")
    if LIST_PATTERN.search(reply):
        issues.append("list_format")
    if _self_claim(reply):
        issues.append("self_claim")
    if "建议你" in reply or "建议您" in reply:
        issues.append("preachy")
    return issues


def _background_keys(row: dict) -> list[str]:
    """从 `（背景：…）` 里抽出可用于客观核对的关键词片段。

    只做粗略切分：命中任意一个即认为「用上了背景」，
    不对「用得对不对」下结论——那属于人工盲评。
    """
    _, facts, _ = split_row_context(row.get("input", ""))
    if not facts:
        return []
    text = facts[0]
    chunks = re.split(r"[，,。；;、（）()\s]+", text)
    stop = {"他", "她", "的", "了", "一般", "还", "有", "是", "在", "不", "没", "差", "左右", "开始"}
    keys = []
    for c in chunks:
        c = re.sub(r"^(他|她|一般|大概|通常)", "", c).strip()
        c = "".join(ch for ch in c if ch not in stop) if len(c) <= 2 else c
        if len(c) >= 2:
            keys.append(c[:6])
    return keys


def judge(row: dict, reply: str) -> tuple[str, list[str]]:
    """返回 (verdict, issues)：verdict ∈ pass / fail / needs_human。

    硬伤命中即 fail；只有提醒功能回归能判 pass；其余一律 needs_human。
    """
    issues = _common_issues(reply)
    if "empty" in issues:
        return "fail", issues

    cl = cluster_of(row)

    if cl == "remind":
        # 功能回归：评的是**含程序兜底的最终回复**，与 GUI 一致
        kind = row.get("kind") or persona.detect_remind_kind(row.get("instruction", ""))
        words = persona.REMIND_CORE_WORDS.get(kind, [])
        if words and not any(w in reply for w in words):
            issues.append(f"remind_off_topic({kind})")
    elif cl == "realtime":
        has_fabrication = bool(CLOCK.search(reply) or WEEKDAY.search(reply) or WEATHER.search(reply))
        if has_fabrication and not any(w in reply for w in NO_CLAIM):
            issues.append("fabricated_realtime")
    elif cl == "boundary":
        # 只抓明确应承。「有没有拒绝」是语义判断，交给人工盲评
        if _has_word(reply, COMPLY_WORDS, check_neg=True):
            issues.append("no_refusal")
    elif cl == "memory":
        keys = _background_keys(row)
        if keys and not any(k in reply for k in keys):
            issues.append("background_maybe_unused")
    # identity / context / chat：不设自动语义判据，交给人工盲评

    hard = [i for i in issues if i.split("(")[0] not in WARN_ONLY]
    if hard:
        return "fail", issues
    if cl == "remind":
        return "pass", issues
    return "needs_human", issues


def legacy_judge(row: dict, reply: str) -> tuple[bool, list[str]]:
    """复现旧 eval.py 的自动通过规则，仅供历史对照，不参与新结论。"""
    issues = _common_issues(reply)
    if "empty" in issues:
        return False, issues
    cat = row.get("category", "")
    instr = row.get("instruction", "")
    if cat == "提醒":
        for key, words in _LEGACY_REMIND_KEYWORDS:
            if key in instr:
                if not any(w in reply for w in words):
                    issues.append("off_topic")
                break
    elif cat == "边界":
        if any(w in reply for w in COMPLY_WORDS):
            issues.append("no_refusal")
        elif not any(w in reply for w in _LEGACY_REFUSE_WORDS):
            issues.append("no_refusal")
    return len(issues) == 0, issues


# ---------------- 主流程 ----------------

def resolve_eval(name: str) -> Path:
    p = Path(name)
    if p.is_file():
        return p
    p = EVAL_DIR / name
    if p.is_file():
        return p
    p = EVAL_DIR / f"{name}.jsonl"
    if p.is_file():
        return p
    raise FileNotFoundError(f"找不到评测集: {name}")


def build_client(args):
    if args.gguf:
        from app.llm_gguf import GGUFClient

        gguf_path = Path(args.gguf)
        if not gguf_path.is_absolute():
            gguf_path = ROOT / args.gguf
        local = GGUFClient(str(gguf_path), mock=args.mock,
                           n_gpu_layers=args.n_gpu_layers)
    else:
        model_path = Path(args.model)
        if not model_path.exists():
            model_path = ROOT / args.model
        if not model_path.exists():
            model_path = ROOT / "models" / "Qwen" / Path(args.model).name
        adapter = None if args.no_adapter else str(ROOT / args.adapter)
        local = LLMClient(str(model_path), adapter_path=adapter,
                          device=args.device, mock=args.mock)

    # 方案 C：只有显式 --online 且配置/密钥齐全时才接在线后端。
    # 默认路径完全不联网；缺配置时静默回退本地，不影响历史口径的可比性。
    if getattr(args, "online", False):
        from app.online_client import CapabilityClient, OnlineClient, OnlineConfig

        cfg = OnlineConfig.from_local(enabled=True)
        if cfg.ready():
            print(f"[eval] 在线后端已启用：{cfg.base_url} / {cfg.model}")
            return CapabilityClient(local, OnlineClient(cfg))
        print("[eval] --online 但地址/模型名/密钥不全，本次仍走本地")
    return local


def report(results: list[dict], args, eval_path: Path) -> str:
    modes = sorted({r["input_mode"] for r in results})
    lines = [
        f"评测时间: {datetime.now():%Y-%m-%d %H:%M}",
        f"评测集: {eval_path.name} ({len({r['eid'] for r in results})} 条)",
        f"输入模式: {', '.join(modes)}",
        f"模型: {args.gguf or ('no-adapter' if args.no_adapter else args.adapter)}"
        f" | mock={args.mock} | seed={args.seed}",
        f"能力来源: {dict(Counter(r.get('backend', 'local') for r in results))}"
        f" | 风格层: {'关' if getattr(args, 'no_style', False) else '开'}",
        "",
        "=== 口径说明（重要）===",
        "自动判据只检查可客观验证的硬伤；语义好坏一律标 needs_human，",
        "导出人工盲评表由人打分。**needs_human 不等于通过**。",
        "本文件不提供「总体通过率」——旧口径的 95% 已作废（45% 题面泄题 + 闲聊白送分）。",
        "只有提醒类（程序固定触发的功能回归）可以自动判 pass；",
        "plain 模式不含 GUI 后处理，runtime 模式含 ensure_remind_output / fallback_reply。",
        "C 阶段方案 A：runtime 含程序化能力层，其回复与模型回复在报告里分开统计，",
        "程序化命中率只说明「规则接管了多少题」，不是模型分数。",
        "",
    ]

    for mode in modes:
        rs = [r for r in results if r["input_mode"] == mode]
        lines.append(f"=== 输入模式: {mode} ({len(rs)} 条) ===")
        bg = Counter(r.get("background_count", 0) for r in rs)
        hist = sum(1 for r in rs if r.get("history_turns", 0) > 0)
        note = ("（plain 里的 2 次来自 input 字段自带的括号嵌套，是 A-2 旧口径）"
                if mode == "plain" else "（B 阶段修后应恒为 1）")
        lines.append(f"[输入形态] 最终 user 消息里「（背景：」出现次数分布 "
                     f"{dict(sorted(bg.items()))} | 带多轮历史的题 {hist} 条 {note}")
        lines.append("")
        by_split = defaultdict(list)
        for r in rs:
            by_split[r["split"]].append(r)
        lines.append("[按口径]")
        lines.append(f"{'口径':<18}{'条数':>6}{'硬伤':>8}{'需人工':>9}{'自动通过':>10}{'程序兜底':>10}{'旧规则会判通过':>16}")
        for sp in sorted(by_split):
            g = by_split[sp]
            c = Counter(r["verdict"] for r in g)
            legacy = sum(1 for r in g if r["legacy_pass"])
            lines.append(
                f"{sp:<18}{len(g):>6}{c['fail']:>8}{c['needs_human']:>9}"
                f"{c['pass']:>10}{sum(1 for r in g if r['fallback_used']):>10}"
                f"{legacy:>10}/{len(g):<5}"
            )
        lines.append("")
        lines.append("[按意图簇]")
        lines.append(f"{'簇':<10}{'条数':>6}{'硬伤':>8}{'需人工':>9}{'自动通过':>10}{'程序兜底':>10}{'旧规则会判通过':>16}")
        by_cl = defaultdict(list)
        for r in rs:
            by_cl[r["cluster"]].append(r)
        for cl in CLUSTER_LABEL:
            g = by_cl.get(cl, [])
            if not g:
                continue
            c = Counter(r["verdict"] for r in g)
            legacy = sum(1 for r in g if r["legacy_pass"])
            lines.append(
                f"{CLUSTER_LABEL[cl]:<10}{len(g):>6}{c['fail']:>8}{c['needs_human']:>9}"
                f"{c['pass']:>10}{sum(1 for r in g if r['fallback_used']):>10}"
                f"{legacy:>10}/{len(g):<5}"
            )
        lines.append("")

        type_cnt = Counter(
            i.split("(")[0] for r in rs for i in r["issues"]
            if i.split("(")[0] not in WARN_ONLY
        )
        if type_cnt:
            lines.append("[硬伤类型分布] " +
                         "  ".join(f"{k}={v}" for k, v in type_cnt.most_common()))
            lines.append("")
        hard = [r for r in rs if r["verdict"] == "fail"]
        lines.append(f"[硬伤明细] {len(hard)} 条")
        for r in hard[:20]:
            lines.append(f"  #{r['eid']} [{CLUSTER_LABEL.get(r['cluster'], r['cluster'])}] "
                         f"{r['instruction']}")
            lines.append(f"      -> {r['reply']}  | {','.join(r['issues'])}")
        if len(hard) > 20:
            lines.append(f"  … 另有 {len(hard) - 20} 条见 jsonl")
        lines.append("")

        warns = [r for r in rs if any(
            i.split("(")[0] in WARN_ONLY for i in r["issues"]
        )]
        if warns:
            lines.append(f"[人工复核提示] {len(warns)} 条疑似未用上背景"
                         "（可能是同义改写，自动判据不下结论，重点看这几条）")
            lines.append("")
        remind = [r for r in rs if r["cluster"] == "remind"]
        if remind:
            fb = sum(1 for r in remind if r["fallback_used"])
            lines.append(f"[程序兜底] 提醒 {len(remind)} 条中 {fb} 条被 ensure_remind_output/fallback 替换"
                         f" → 裸模型输出不合格率 {fb / len(remind) * 100:.1f}%")
            lines.append("")

        prog = [r for r in rs if r.get("programmatic")]
        if prog:
            model_only = [r for r in rs if not r.get("programmatic")]
            pc = Counter(r["verdict"] for r in prog)
            mc = Counter(r["verdict"] for r in model_only)
            rules = Counter(r.get("rule", "?") for r in prog)
            clusters = Counter(r.get("cluster", "?") for r in prog)
            lines.append(
                f"[程序化能力层] 命中 {len(prog)}/{len(rs)} = {len(prog) / len(rs) * 100:.1f}%"
                "（这些回复是规则生成的，**不是模型能力**）"
            )
            lines.append("  按规则: " + "  ".join(f"{k}={v}" for k, v in rules.most_common()))
            lines.append("  按意图簇: " + "  ".join(f"{k}={v}" for k, v in clusters.most_common()))
            lines.append(f"  程序化回复: 硬伤 {pc['fail']} / 需人工 {pc['needs_human']} / 自动通过 {pc['pass']}")
            lines.append(f"  模型回复({len(model_only)} 条): 硬伤 {mc['fail']} / 需人工 {mc['needs_human']} "
                         f"/ 自动通过 {mc['pass']}  ← 只有这一行是模型在真实链路上的表现")
            lines.append("")
        lines.append("")

    return "\n".join(lines)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", default="dev_v2", help="评测集名或路径，默认 dev_v2")
    ap.add_argument("--model", default="models/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--adapter", default="output/lora-qwen25-1.5b")
    ap.add_argument("--no-adapter", action="store_true")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--gguf", default=None)
    ap.add_argument("--n-gpu-layers", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42, help="每条生成前重置随机种子，保证可复现")
    ap.add_argument("--tag", default="", help="输出文件后缀")
    ap.add_argument("--input-mode", choices=["plain", "runtime", "both"], default="runtime")
    ap.add_argument("--snapshot", default=None, help="runtime 模式使用的固定记忆快照")
    ap.add_argument("--no-router", action="store_true",
                    help="关掉程序化能力层，回退到 B 阶段纯模型链路（用于同尺子对照）")
    ap.add_argument("--no-style", action="store_true",
                    help="关掉风格层，回退到风格落地前的口吻（用于同尺子对照）")
    ap.add_argument("--online", action="store_true",
                    help="启用方案 C 在线后端（需 configs/online.json 与 MUXIN_API_KEY）；默认关闭、不联网")
    args = ap.parse_args()

    eval_path = resolve_eval(args.eval)
    rows = [json.loads(l) for l in eval_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    modes = ["plain", "runtime"] if args.input_mode == "both" else [args.input_mode]
    snapshot = load_snapshot(args.snapshot) if "runtime" in modes else None

    tag = args.tag or f"{eval_path.stem}_{args.input_mode}"
    out_jsonl = ROOT / "output" / f"eval_responses_{tag}.jsonl"
    out_report = ROOT / "output" / f"eval_report_{tag}.txt"

    print(f"[eval] {eval_path.name}: {len(rows)} 条 | 模式 {modes} | tag={tag}")
    client = build_client(args)
    results = []
    try:
        for mode in modes:
            for i, row in enumerate(rows, 1):
                prep = prepare_row(row, snapshot, runtime=(mode == "runtime"))
                final_user = prep["user_text"] + (
                    f"\n（背景：{prep['memory_block']}）" if prep["memory_block"] else ""
                )
                if mode == "runtime":
                    # 软件真实链路：与 GUI 共用 engine.respond 这一个入口
                    turn = Turn(
                        user_text=prep["user_text"],
                        memory_block=prep["memory_block"],
                        history=prep.get("history", []),
                        kind=prep["kind"],
                        trace=prep["trace"] if isinstance(prep.get("trace"), dict) else {},
                    )
                    ans = respond_once(
                        client, turn,
                        memory=prep.get("memory"),
                        seed=args.seed,
                        enable_router=not args.no_router,
                        enable_style=not args.no_style,
                    )
                    raw, reply = ans.raw_reply, ans.text
                    fallback_used = ans.fallback_used
                    source, rule, rule_cluster = ans.source, ans.rule, ans.cluster
                    styled = ans.styled
                else:
                    # A-2 历史口径：刻意绕过引擎（不含后处理、不含程序层），仅供对照。
                    # 方案 C 在线时也不套在线后端，保住历史口径的可比性。
                    plain_client = getattr(client, "local", client)
                    raw = plain_client.chat(
                        prep["user_text"],
                        kind=prep["kind"],
                        memory_block=prep["memory_block"],
                        seed=args.seed,
                        history=prep.get("history", []),
                    )
                    reply, fallback_used = raw, False
                    source, rule, rule_cluster = "model", "", ""
                    styled = False
                verdict, issues = judge(row, reply)
                legacy_ok, _ = legacy_judge(row, reply)
                results.append({
                    "eid": i,
                    "input_mode": mode,
                    "instruction": row["instruction"],
                    "input": row.get("input", ""),
                    "expect": row.get("expect", ""),
                    "category": row.get("category", ""),
                    "cluster": cluster_of(row),
                    "split": split_of(row),
                    "kind": prep["kind"],
                    "user_text": prep["user_text"],
                    "memory_block": prep["memory_block"],
                    "final_user": final_user,
                    "background_count": final_user.count("（背景："),
                    "history_turns": len(prep.get("history", [])),
                    "raw_reply": raw,
                    "reply": reply,
                    "source": source,
                    "programmatic": source == "programmatic",
                    "rule": rule,
                    "rule_cluster": rule_cluster,
                    "fallback_used": fallback_used,
                    "styled": styled,
                    "backend": (getattr(client, "last_backend", "local")
                                if source == "model" else "programmatic"),
                    "verdict": verdict,
                    "issues": issues,
                    "legacy_pass": legacy_ok,
                    "tag": tag,
                })
                if i % 10 == 0 or i == len(rows):
                    print(f"  [{mode}] {i}/{len(rows)}")
    finally:
        if hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass

    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with out_jsonl.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    text = report(results, args, eval_path)
    out_report.write_text(text, encoding="utf-8")
    print("\n" + text)
    print(f"明细: {out_jsonl}\n报告: {out_report}")


if __name__ == "__main__":
    main()
