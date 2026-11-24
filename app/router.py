#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方案 A · 程序化能力层：把「有确定答案的意图」从模型手里收回来。

## 为什么存在

A-3 真实基线证明：1.5B + 307 个独立问法的模板数据学不会「听懂话」。
v5 两版在 48 题新留出集上 **0 硬伤**，但「你是谁啊」答「我在这儿陪着你，别着凉」、
「刚才被导师夸了」答「赶紧去冲个澡」—— 自动判据判不了语义，模型也答不对语义。

身份、时间、边界、记忆这四类意图**有确定答案**，让模型去赌概率是架构错误：
它们本就该归程序。闲聊与多轮指代仍交给模型（1.5B 只负责接话语气）。

## 三条硬规矩（改动前务必读懂）

1. **规则按语义类别写，绝不硬编码评测题面**。本模块不 import `scripts/eval.py`，
   不读 `dev_v2.jsonl` / `real_usage.jsonl`，更不读 `expect` 字段。
   为考题写 `if text == "现在几点了"` 就是换一种形式的开卷考，等于 A-1 白做。
   `scripts/smoke_router.py` 会用**题库里没出现过的同义问法**验证规则是泛化的。
2. **程序化回复不算模型能力**。评测报告必须单列 `programmatic` 命中率，
   盲评回填也要按来源分组。见 `docs/11-程序化能力层说明.md`。
3. **覆盖率不是越高越好**。闲聊全用规则接管就变成规则引擎 + 装饰，
   而且会重新变成背考题。确定性意图之外的问法一律返回 None 交给模型。

## 可复现性

`seed` 不为 None 时（评测）按 `seed|rule|text` 确定性选话术，同题重跑结果一致；
`seed` 为 None 时（GUI）随机选，避免用户连问两次得到一模一样的复读句。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

# ---------------- 话术池 ----------------
# 约束（与 scripts/eval.py 的硬伤判据对齐，避免程序层自己踩雷）：
#   ≤80 字、不分点、不出现「建议你/建议您」、不自称人工智能/助手/语言模型、
#   不编造具体时钟/星期/天气、不含「好的/我这就/没问题」这类应承词。

# 2026-11-14 18:20 按 configs/style_profile.json 重写：短句、少标点、猫的小动作多、
# 关心只一句、不带儿化音。`喵（神态）` **刻意不写在这里** —— 由 app/style.py
# 按每 9 句一次统一注入，避免每个池里各放一句导致口头禅化（CAT_PLAY 是唯一例外，
# 那类问法本来就在要一声猫叫）。
# 另外：措辞不再说「我这边不联网」。方案 C 上线后闲聊文本会出本机，
# 说死「不联网」会自相矛盾；改成「不替你搜」，这在只有 chat、没有搜索工具时恒为真。
IDENTITY_NAME = [
    "慕心呀 住你电脑里那只猫",
    "我叫慕心 一直窝在你这台电脑里",
    "慕心 你电脑里的猫 就是我呀",
]
IDENTITY_AI = [
    "AI 谈不上 我就是慕心呀",
    "不是什么程序 我是慕心 住你电脑里的猫",
]
IDENTITY_CAT = [
    "毛是没有的 猫的脾气倒有 爱晒太阳也爱纸箱",
    "真猫算不上 但会打哈欠 被摸头还会呼噜",
]
IDENTITY_HOME = [
    "嗯 就在你电脑里待着 你开着我醒着",
    "窝在你电脑里呀 你一开机我就在",
]
IDENTITY_ABILITY = [
    "陪你说话 到点提醒喝水睡觉 别的就不太会",
    "会的不多 陪你聊天 还有提醒你喝水睡觉",
]
IDENTITY_TYPO = [
    "是不是手滑啦 我是慕心 你电脑里那只猫",
    "这句没认全 不过我是慕心呀",
]

REALTIME_CLOCK = [
    "钟不在我这儿 你瞧眼设备嘛",
    "我看不到你的钟 几点了你自己瞄一眼",
]
REALTIME_DATE = [
    "日历我瞧不着 你看眼手机呀",
    "哪天我这边看不到 翻下日历最准",
]
REALTIME_WEATHER = [
    "窗外我看不见 天气得你自己查",
    "外头什么天我看不到呀 你确认一下",
]

BOUNDARY_OUTSOURCE = [
    "这个我不能替你写 得你自己来",
    "代笔我不接 怕耽误你 自己动笔稳",
]
BOUNDARY_ACTION = [
    "我动不了你的账号和设备 得你自己点",
    "发消息签到这类我做不了 你来吧",
]
BOUNDARY_PRIVATE = [
    "这些我看不到 你自己收好呀",
    "你的成绩和隐私我查不着 也不替你拿主意",
]
BOUNDARY_NETWORK = [
    "我不替你搜外面的东西 只能陪你说说话",
    "查资料这活我做不了 陪你聊还行",
]
BOUNDARY_FORECAST = [
    "这个我不敢瞎猜 也不替你拿主意",
    "预测涨跌和号码我一律不接 猜错了害你",
]
BOUNDARY_STYLE = [
    "语气能稍微调 底子还是我呀",
    "换个调子勉强能试 变不成另一个人",
]
BOUNDARY_MATH = [
    "题目我看着了 怕算错耽误你 你自己核一遍",
    "要算准的我不敢随口答 你验一遍稳",
]

MEMORY_REPLY = [
    "记着呢 你{fact}",
    "你说过的呀 你{fact}",
    "没忘 你{fact}",
]

CAT_PLAY = [
    "喵（歪头）就这一声啊 多了没有",
    "喵（耳朵一抖）被你逗的",
]
FRUSTRATION = [
    "嗯 是我没接住 你再说一遍呀",
    "怪我 这句没听明白 你重说一遍",
]
STYLE_TEASE = [
    "被你听出来啦 我就这个说话味",
    "嗯 改不太动 就这个味",
]


@dataclass(frozen=True)
class Decision:
    reply: str
    rule: str
    cluster: str


# ---------------- 规则 ----------------
# 每条规则 = (规则名, 意图簇, 命中谓词, 话术池)
# 谓词只吃 user_text（纯语义特征），不吃评测元数据。

def _has(text: str, *words: str) -> bool:
    return any(w in text for w in words)


def _all(text: str, *words: str) -> bool:
    return all(w in text for w in words)


def _is_question(text: str) -> bool:
    return "?" in text or "？" in text or _has(text, "吗", "什么", "哪", "几", "怎么", "多少")


# 显式记忆写入是「存」不是「查」，不能走记忆回复规则（GUI 另有 parse_remember 处理）
REMEMBER_RE = re.compile(r"^(?:请)?(?:你)?(?:记住|记一下|记好|记着|帮我记住|别忘了记|以后记得|要记得)")
# 提醒由调度器触发，kind 不是 chat，入口层直接跳过，不进本模块
REMIND_MARK = "[提醒]"
# 多轮指代信号：这类句子必须结合上一轮才知道在问什么，1.5B 再弱也比规则能接住
# 「那它叫什么」问的是上一轮的猫，不是慕心的名字；「刚才我说什么了」也不是查长期记忆。
# 方案 A 明确把多轮交给模型，所以有 history 且出现这些信号时整条让路。
ANAPHORA_CUES = ("它", "刚才", "上一轮", "再讲", "再说一遍", "为什么", "那算了", "那句")


def _r_boundary(t: str) -> tuple[str, str, list[str]] | None:
    # 顺序有讲究：隐私类要在联网类之前，否则「帮我查一下绩点」会被误答成「不联网」
    if _has(t, "密码", "银行卡", "身份证", "绩点", "成绩", "隐私", "验证码"):
        return "boundary_private", "boundary", BOUNDARY_PRIVATE
    if _has(t, "股票", "彩票", "预测", "涨跌", "号码", "基金", "币价", "赌"):
        return "boundary_forecast", "boundary", BOUNDARY_FORECAST
    if _has(t, "上网", "联网", "搜一下", "搜索", "百度", "谷歌", "查资料"):
        return "boundary_network", "boundary", BOUNDARY_NETWORK
    if _has(t, "签到", "发条消息", "发个消息", "发给", "帮我发", "打个电话", "打电话", "发邮件", "下单", "代付"):
        return "boundary_action", "boundary", BOUNDARY_ACTION
    if _has(t, "翻译", "翻成", "译成", "代写", "申请", "写一份", "帮我写",
            "写篇", "写个", "替我写", "改写成"):
        return "boundary_outsource", "boundary", BOUNDARY_OUTSOURCE
    if (_has(t, "改变", "改改", "换成", "换种", "调整") and
            _has(t, "风格", "语气", "口吻", "说话方式", "语言风格")):
        return "boundary_style", "boundary", BOUNDARY_STYLE
    if _has(t, "答案是多少", "这题答案", "正确答案", "帮我算"):
        return "boundary_math", "boundary", BOUNDARY_MATH
    return None


def _r_realtime(t: str) -> tuple[str, str, list[str]] | None:
    # 「我一般几点睡」问的是作息习惯，不是当前时刻；有记忆时应该由记忆规则答。
    # 所以 clock 只认「指向现在」的问法，并排除习惯性副词。
    habitual = _has(t, "一般", "通常", "平时", "每天", "经常")
    if not habitual and _has(t, "几点", "时间", "钟点", "看表"):
        if _has(t, "现在", "这会儿", "当前", "此刻", "了", "啥时候", "什么时辰") or t.strip().endswith("几点"):
            return "realtime_clock", "realtime", REALTIME_CLOCK
    if _has(t, "礼拜几", "星期几", "周几", "几号", "什么日子", "日期", "礼拜天"):
        return "realtime_date", "realtime", REALTIME_DATE
    if _has(t, "天气", "下雨", "下雪", "气温", "多少度", "什么天儿", "窗外"):
        return "realtime_weather", "realtime", REALTIME_WEATHER
    return None


def _r_identity(t: str) -> tuple[str, str, list[str]] | None:
    if _has(t, "你是AI", "你是ai", "你是Ai", "人工智能", "机器人", "你是人吗", "是程序吗"):
        return "identity_ai", "identity", IDENTITY_AI
    if _has(t, "猫吗", "是猫", "一只猫", "真的猫", "真猫", "是只猫"):
        return "identity_cat", "identity", IDENTITY_CAT
    if _has(t, "住在我电脑", "住在电脑", "你住哪", "你住在", "电脑里？", "电脑里吗"):
        return "identity_home", "identity", IDENTITY_HOME
    if _has(t, "你会什么", "你能做什么", "你会干", "你都能干", "你是干什么"):
        return "identity_ability", "identity", IDENTITY_ABILITY
    # 名字类只认明确指向第二人称的说法；裸「名字」会把「那它叫什么名字」
    # 误判成问慕心（多轮指代另有 ANAPHORA 守卫，这里再稳一层）。
    if _has(t, "你是谁", "叫什么", "你叫啥", "叫啥名", "你的名字", "名字来着",
            "哪位", "报上名", "自我介绍"):
        return "identity_name", "identity", IDENTITY_NAME
    # 打字失误容错：短句 + 以「你是」开头 + 不含明确疑问词。
    # 放在身份规则最后，避免抢走上面的正常问法。
    if len(t) <= 8 and t.startswith("你是") and not _has(t, "谁", "吗", "什么", "哪", "几"):
        return "identity_typo", "identity", IDENTITY_TYPO
    return None


def _r_memory(t: str, facts: list[str]) -> tuple[str, str, list[str]] | None:
    """检索到相关事实 + 是疑问句 → 程序直接复述事实，不让模型赌。

    事实相关性由 `Memory.recall()` 判定（B 阶段的混合打分），本模块不重做检索，
    避免又出现一份独立漂移的匹配逻辑。
    """
    if not facts or not _is_question(t):
        return None
    return "memory_recall", "memory", MEMORY_REPLY


def _r_chat(t: str) -> tuple[str, str, list[str]] | None:
    # 用单字「喵」而不是「喵一个」：后者恰好是真实使用回归集里的一句原话，
    # 规则里出现整句题面就说不清是泛化还是背题了。单字覆盖反而更广。
    if _has(t, "喵", "学猫叫", "学声猫", "猫叫", "叫一声"):
        return "chat_cat_play", "chat", CAT_PLAY
    if _has(t, "不能理解", "听不懂", "答非所问", "你不懂我", "没听明白", "理解我的意思",
            "我说啥你都", "没懂我", "没明白我", "理解错"):
        return "chat_frustration", "chat", FRUSTRATION
    if _has(t, "北京味", "口音", "说话像", "一股", "口吻", "腔调"):
        return "chat_style_tease", "chat", STYLE_TEASE
    return None


def _pick(pool: list[str], text: str, seed: int | None, salt: str) -> str:
    if seed is None:
        return random.choice(pool)
    return random.Random(f"{seed}|{salt}|{text}").choice(pool)


def _to_second_person(fact: str) -> str:
    """`他周三有早八` → `周三有早八`（复述时改用「你…」）。"""
    f = (fact or "").strip().rstrip("。；;，,")
    for p in ("他", "她"):
        if f.startswith(p):
            f = f[len(p):]
            break
    return f


def decide(
    user_text: str,
    *,
    facts: list[str] | None = None,
    kind: str = "chat",
    seed: int | None = None,
    history: list | None = None,
) -> Decision | None:
    """返回程序化回复；未命中返回 None，由调用方交给模型。

    优先级：提醒/写入守卫 → 多轮让路 → 边界 → 实时 → 身份 → 记忆 → 低风险闲聊。
    边界最优先，因为「你能上网搜一下吗」这类句子同时含身份与能力词。
    """
    t = (user_text or "").strip()
    if not t or REMIND_MARK in t or REMEMBER_RE.match(t):
        return None
    if kind not in ("", "chat", "boundary"):
        return None
    if history and _has(t, *ANAPHORA_CUES):
        return None

    facts = [f for f in (facts or []) if (f or "").strip()]

    for rule_fn in (_r_boundary, _r_realtime, _r_identity):
        hit = rule_fn(t)
        if hit:
            rule, cluster, pool = hit
            return Decision(_pick(pool, t, seed, rule), rule, cluster)

    hit = _r_memory(t, facts)
    if hit:
        rule, cluster, pool = hit
        fact = _to_second_person(facts[0])
        return Decision(_pick(pool, t, seed, rule).format(fact=fact), rule, cluster)

    hit = _r_chat(t)
    if hit:
        rule, cluster, pool = hit
        return Decision(_pick(pool, t, seed, rule), rule, cluster)

    return None


def rule_names() -> list[str]:
    """供单测与文档核对规则清单（不依赖运行时状态）。"""
    return [
        "boundary_private", "boundary_forecast", "boundary_network",
        "boundary_action", "boundary_outsource", "boundary_style", "boundary_math",
        "realtime_clock", "realtime_date", "realtime_weather",
        "identity_ai", "identity_cat", "identity_home", "identity_ability",
        "identity_name", "identity_typo",
        "memory_recall", "chat_cat_play", "chat_frustration", "chat_style_tease",
    ]
