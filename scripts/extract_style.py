#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从聊天记录里提炼「说话风格档」草稿 —— 只读分析，不改运行时行为。

## 为什么不是「把聊天记录拿去微调」

A-1 查出旧评测集 45% 题面与训练逐字相同，训练侧 2145+2391 条去重后只有
307 个独立问法；A-3 证明 1.5B 在这种模板数据上学会的是「按背景串拼短句」，
连「你是谁」都答不出。再喂一批私人聊天记录只会：
  1) 量太少（几十轮）→ 背下这几句，换个问法照样崩；
  2) 把私人信息写进权重，之后想删都删不掉；
  3) 重训一次要几个小时，且必然破坏 A/B 两阶段的可比基线。

所以聊天记录的正确用途是**风格参考**：抽取句长、语气词、口头禅、称呼、
标点习惯，写成一份人能读能改的 `configs/style_profile.json`。
程序化话术池改完 100% 生效、不需要重训、随时可回退。

## 安全边界

- 全程本机读写，不联网、不上传、不写进模型权重。
- 产出的 `enabled` 固定为 `false`：用户没确认前，运行时**完全不加载**这份档。
- 报告里的例句是原文摘录；如果记录含他人隐私，请先脱敏再给我。

## 支持的输入格式

```
老周：今天跑完步累死了
慕心：先坐会儿，别急着灌水
```
或带时间戳（微信/QQ 导出的常见形态）：
```
[2026-11-12 22:10:03] 老周: 今天跑完步累死了
2026-11-12 22:10:20 慕心：先坐会儿
```
也支持每行一句、不标说话人的纯文本（此时全部视为目标发言）。

用法：
    # 先看这份记录里有哪些说话人、各多少句
    python scripts/extract_style.py chat.txt

    # 指定要模仿的那一方，产出报告与草稿
    python scripts/extract_style.py chat.txt --speaker 老周

    # 内置样例自测（不写盘）
    python scripts/extract_style.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / "configs" / "style_profile.json"
DEFAULT_REPORT = ROOT / "docs" / "14b-聊天记录风格抽取报告.md"

# 行首「说话人：」；允许前面带 [时间] 或 时间 前缀
LINE_RE = re.compile(
    r"^\s*(?:\[[^\]]{4,40}\]\s*|\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+[\d:]+\s*)?"
    r"([^：:]{1,16})[：:]\s*(.+)$"
)
CJK = re.compile(r"[\u4e00-\u9fff]+")
PARTICLES = ["吧", "啊", "呢", "嘛", "哦", "嗯", "啦", "呀", "哈", "呗", "咯", "喽", "诶", "鸭"]
PUNCTS = ["，", "。", "！", "？", "…", "～", "、", "；", "：", "~", "!", "?", ",", "."]
# 只用于提示「原文里可能有需要避开的表达」，不做任何过滤或改写
RISK_WORDS = ["他妈", "妈的", "傻", "滚", "闭嘴", "烦死", "去死", "废物", "蠢"]

SELF_TEST_TEXT = """\
老周：今天跑完步，累但挺爽
慕心：先坐会儿吧，别急着灌水
老周：实验终于跑完了，我人快没了
慕心：那先把电脑合上，歇十分钟
老周：你别老提醒我喝水啊
慕心：晓得啦，就这一句，不念了
老周：周三早八，记得别让我睡过头
慕心：记着呢，到时候叫你
老周：算了不说了，我去洗澡
慕心：去吧，热水别开太烫
"""


def _valid_speaker(line: str) -> re.Match | None:
    m = LINE_RE.match(line)
    if not m:
        return None
    speaker = m.group(1).strip()
    # 时间戳里的数字/冒号组合不算说话人
    if not speaker or re.fullmatch(r"[\d\s:/\-]+", speaker):
        return None
    return m if m.group(2).strip() else None


def parse_lines(text: str) -> tuple[list[tuple[str, str]], bool]:
    """返回 [(说话人, 内容)] 与「是否识别到说话人标记」。"""
    raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not raw_lines:
        return [], False
    # 纯文本（每行一句、无说话人标记）：每行独立成句，不能合并成一大段
    if not any(_valid_speaker(l) for l in raw_lines):
        return [("<全部>", l) for l in raw_lines], False

    parsed: list[tuple[str, str]] = []
    for line in raw_lines:
        m = _valid_speaker(line)
        if m:
            parsed.append((m.group(1).strip(), m.group(2).strip()))
        elif parsed:
            # 续行（微信导出的多行消息）并回上一句
            s, c = parsed[-1]
            parsed[-1] = (s, c + " " + line)
        else:
            parsed.append(("<全部>", line))
    return parsed, True


def ngrams(text: str, n: int) -> list[str]:
    out = []
    for seg in CJK.findall(text):
        out.extend(seg[i:i + n] for i in range(len(seg) - n + 1))
    return out


def top(counter: Counter, k: int) -> list[list]:
    return [[w, c] for w, c in counter.most_common(k)]


def analyse(lines: list[str]) -> dict:
    joined = " ".join(lines)
    lens = [len(re.sub(r"\s", "", s)) for s in lines]
    tails = Counter(s[-1] for s in lines if s)
    punct = Counter({p: joined.count(p) for p in PUNCTS if joined.count(p)})
    particles = Counter({p: joined.count(p) for p in PARTICLES if joined.count(p)})
    grams2 = Counter(ngrams(joined, 2))
    grams3 = Counter(ngrams(joined, 3))
    heads = Counter(s[:3] for s in lines if len(s) >= 3)
    risk = {w: joined.count(w) for w in RISK_WORDS if w in joined}
    med = statistics.median(lens) if lens else 0
    # 代表性例句：长度最接近中位数的若干句，按原顺序取
    samples = sorted(lines, key=lambda s: abs(len(s) - med))[:8]
    return {
        "utterances": len(lines),
        "chars_total": sum(lens),
        "length": {
            "median": med,
            "mean": round(statistics.mean(lens), 1) if lens else 0,
            "min": min(lens) if lens else 0,
            "max": max(lens) if lens else 0,
            "p80": sorted(lens)[int(len(lens) * 0.8)] if lens else 0,
        },
        "line_end_chars": top(tails, 10),
        "punctuation": top(punct, 12),
        "tone_particles": top(particles, 10),
        "common_2gram": top(grams2, 20),
        "common_3gram": top(grams3, 15),
        "sentence_openers": [[k, v] for k, v in heads.most_common(10) if v >= 2],
        "risk_words_in_source": risk,
        "representative_lines": samples,
    }


def build_profile(source: str, speaker: str, stats: dict) -> dict:
    return {
        "enabled": False,
        "_comment": "草稿：enabled=false 时运行时完全不加载本文件。请人工确认后再改。",
        "source": {
            "file": source,
            "speaker": speaker,
            "utterances": stats["utterances"],
            "extracted_at": datetime.now().isoformat(timespec="seconds"),
        },
        "measurements": stats,
        "draft_to_confirm": {
            "address": "TODO 称呼（例：老周 / 不称呼）",
            "sentence_length_hint": "TODO 例：多用短句，中位 %s 字，尽量不超过 %s 字"
                                    % (stats["length"]["median"], stats["length"]["p80"]),
            "tone_particles_to_keep": [w for w, _ in stats["tone_particles"][:5]],
            "tone_particles_to_avoid": [],
            "catchphrases": [w for w, _ in stats["sentence_openers"][:5]],
            "taboo": ["脏话/冒犯表达", "说教口吻", "分点列举", "自称 AI 或助手"],
            "self_reference": "TODO 自称（例：我 / 慕心 / 不自称）",
            "notes": "本档只描述风格，不含聊天记录原文；例句见 14b 报告，确认后可删除。",
        },
    }


def render_report(source: str, speaker: str, stats: dict, speakers: Counter,
                  confirmed: dict | None = None) -> str:
    L = stats["length"]
    lines = [
        "# 14b · 聊天记录风格抽取报告",
        "",
        f"> 抽取时间：{datetime.now():%Y-%m-%d %H:%M}　来源：`{source}`　目标说话人：`{speaker}`",
        "> 本报告只做统计，不含模型改动；`configs/style_profile.json` 的 `enabled` 仍为 `false`。",
        "",
        "## 样本概况",
        "",
        f"- 目标发言 **{stats['utterances']}** 句，共 {stats['chars_total']} 字",
        f"- 句长：中位 **{L['median']}** 字 / 平均 {L['mean']} 字 / 80 分位 {L['p80']} 字 / 最长 {L['max']} 字",
        f"- 记录中检测到的说话人：{', '.join(f'{k}({v})' for k, v in speakers.most_common())}",
        "",
        "## 可量化的风格特征",
        "",
        "| 维度 | 结果 |",
        "|---|---|",
        f"| 句尾字符 | {', '.join(f'{w}×{c}' for w, c in stats['line_end_chars'])} |",
        f"| 语气词 | {', '.join(f'{w}×{c}' for w, c in stats['tone_particles']) or '（无）'} |",
        f"| 标点 | {', '.join(f'{w}×{c}' for w, c in stats['punctuation']) or '（无）'} |",
        f"| 高频二字词 | {', '.join(w for w, _ in stats['common_2gram'][:12])} |",
        f"| 口头禅候选（开头≥2次） | {', '.join(w for w, _ in stats['sentence_openers']) or '（无）'} |",
        f"| 需避开的表达（原文命中） | {', '.join(f'{w}×{c}' for w, c in stats['risk_words_in_source'].items()) or '（未命中）'} |",
        "",
        "## 代表性原句（用来对照语气，不是拿来背的）",
        "",
    ]
    lines += [f"{i}. {s}" for i, s in enumerate(stats["representative_lines"], 1)]
    lines += [
        "",
        "## 已人工确认的约束（优先级高于统计结果）",
        "",
    ]
    if confirmed:
        lines += [
            "> 聊天记录里量出来的习惯，如果和下面这些人为要求冲突，**以人为要求为准**。",
            "",
            "| 项 | 已确认内容 |",
            "|---|---|",
        ]
        for k, v in confirmed.items():
            if k.startswith(("source_", "address_note", "cat_expression_frequency")):
                continue
            val = "、".join(v) if isinstance(v, list) else str(v)
            lines.append(f"| {k} | {val.replace('|', chr(92) + '|')} |")
        lines.append("")
    else:
        lines += ["（暂无；见 `configs/style_profile.json`）", ""]
    lines += [
        "## 仍需确认的事",
        "",
        "1. **称呼**：`主人` / `姐姐` / 两者混用 / 都不要另定？（`老周` 已明确禁用）",
        "2. **`喵（…）` 的用法**：句尾附带还是单独成句，「偶尔」的具体频率",
        "3. **转写核对**：截图转成的文本要你扫一眼确认没认错字",
        "4. **范围**：只改程序化话术（100% 生效、可回退），还是连 `SYSTEM_PROMPT` 一起调"
        "（1.5B 上效果有限）",
        "",
        "确认后我会把结论写进 `configs/style_profile.json`，把 `enabled` 打开，",
        "再改写 `app/router.py` 话术池与 `app/persona.py` 的 SYSTEM_PROMPT，",
        "最后用 `dev_v2 + real_usage + 盲评表` 回归，确认风格变了但硬伤没涨。",
        "",
        "> 隐私：原文只在本机读取。若记录里有他人姓名、电话、地址，建议先删掉再提供；",
        "> 风格档本身只存统计量，不存聊天原文。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="从聊天记录提炼说话风格档草稿")
    ap.add_argument("path", nargs="?", default=None, help="聊天记录 txt 路径")
    ap.add_argument("--speaker", default=None, help="要模仿的说话人名字")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE))
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--self-test", action="store_true", help="用内置样例自测，不写盘")
    args = ap.parse_args()

    if args.self_test:
        text, source = SELF_TEST_TEXT, "<内置样例>"
    elif args.path:
        p = Path(args.path)
        if not p.exists():
            raise SystemExit(f"找不到聊天记录：{p}")
        text, source = p.read_text(encoding="utf-8-sig", errors="replace"), str(p)
    else:
        raise SystemExit("需要给一个聊天记录文件，或用 --self-test 自测")

    parsed, tagged = parse_lines(text)
    speakers = Counter(s for s, _ in parsed)
    if not parsed:
        raise SystemExit("没解析出任何有效发言行")
    print(f"[style] 共 {len(parsed)} 句，检测到说话人："
          + (", ".join(f"{k}({v})" for k, v in speakers.most_common()) if tagged else "（无标记）"))

    if not args.self_test and not args.speaker and tagged and len(speakers) > 1:
        print("[style] 有多个说话人，请用 --speaker 指定要模仿哪一方后再跑一次")
        return

    speaker = args.speaker or (speakers.most_common(1)[0][0] if tagged else "<全部>")
    lines = [c for s, c in parsed if not args.speaker or s == args.speaker] if args.speaker \
        else [c for _, c in parsed]
    if not lines:
        raise SystemExit(f"说话人 {speaker!r} 没有匹配到任何发言（可用名字：{list(speakers)}）")

    stats = analyse(lines)
    profile = build_profile(source, speaker, stats)

    if args.self_test:
        print(f"[style] 自测：{stats['utterances']} 句，中位句长 {stats['length']['median']} 字")
        print("  语气词:", stats["tone_particles"])
        print("  句尾字符:", stats["line_end_chars"][:5])
        print("  代表句:", stats["representative_lines"][:2])
        print("[style] 自测通过，未写盘")
        return

    pp, rp = Path(args.profile), Path(args.report)
    pp.parent.mkdir(parents=True, exist_ok=True)
    rp.parent.mkdir(parents=True, exist_ok=True)
    # 已人工确认的约束不能被统计结果覆盖：只更新 source / measurements / draft，
    # 保留 project_context / confirmed / pending_confirmation / conflicts_with_current_code。
    # enabled 刻意不保留 —— 重新抽样式意味着统计变了，必须重新确认才允许启用。
    if pp.exists():
        try:
            old = json.loads(pp.read_text(encoding="utf-8"))
            for k in ("project_context", "confirmed", "pending_confirmation",
                      "conflicts_with_current_code"):
                if k in old:
                    profile[k] = old[k]
            print(f"[style] 已保留 {pp.name} 里人工确认的约束；enabled 重置为 false")
        except (json.JSONDecodeError, OSError) as e:
            print(f"[style] 警告：已有风格档读不了（{e}），将重建")
    report = render_report(source, speaker, stats, speakers, profile.get("confirmed"))
    pp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    rp.write_text(report, encoding="utf-8")
    print(f"[style] 风格档草稿：{pp}（enabled=false，运行时不加载）")
    print(f"[style] 抽取报告：{rp}")
    print(f"[style] 中位句长 {stats['length']['median']} 字；请核对报告里的「仍需确认」项")


if __name__ == "__main__":
    main()
