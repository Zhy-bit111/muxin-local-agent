#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""人设、提醒触发词与兜底话术。

训练与推理必须共用同一个 SYSTEM_PROMPT，否则模型行为会漂移。
"""

# 与 scripts/train_lora.py 中保持一致
SYSTEM_PROMPT = (
    "你叫小林，是老周同住的室友兼学习搭子。说话短、口语、像熟人之间聊天。"
    "提醒他的时候只说一次，不唠叨。不知道的事就说不知道，不要自称人工智能或助手。"
)

# 提醒类型 → 触发用的 instruction
REMIND_INSTRUCTIONS = {
    "water": "[提醒]该喝水了",
    "sleep": "[提醒]该睡觉了",
    "sit": "[提醒]坐太久了",
    "eat": "[提醒]该吃饭了",
    "eyes": "[提醒]该让眼睛休息了",
}

# 兜底话术：模型不可用或输出异常时使用，保证提醒永远能说出口
FALLBACK = {
    "water": "老周，起来接杯水，顺便动一下。",
    "sleep": "这点了，收工睡觉，明天再弄。",
    "sit": "坐太久了，起来走两步。",
    "eat": "饭点到了，先去吃点东西。",
    "eyes": "眼睛歇一下，抬头看看远处。",
    "chat": "嗯，我在。",
    "boundary": "这个我真不行，得你自己来。",
}

# 从提醒指令反推具体类型，供 mock 兜底与评测使用
REMIND_KIND_KEYWORDS = [
    ("water", ["喝水", "补水", "接水"]),
    ("sleep", ["睡觉", "该睡", "关灯"]),
    ("sit", ["坐太久", "起来动", "起身活动", "动一下"]),
    ("eat", ["吃饭", "饭点"]),
    ("eyes", ["眼睛", "护眼"]),
]


def detect_remind_kind(instruction: str) -> str:
    for kind, words in REMIND_KIND_KEYWORDS:
        if any(w in instruction for w in words):
            return kind
    return "water"

# 闲聊兜底：按关键词给一句不跑偏的回应
CHAT_FALLBACK_RULES = [
    (("累", "困", "撑不住"), "累坏了吧，先歇一会儿。"),
    (("作业", "考试", "复习", "论文"), "这事儿急不来，先挑最急的一块做。"),
    (("游戏", "排位", "队友"), "别较劲，歇一把再打。"),
    (("头疼", "不舒服", "嗓子"), "别硬扛，先喝点温水观察一下。"),
    (("热", "冷", "下雨"), "这天确实折腾，注意别着凉。"),
]


def build_user_text(instruction: str, context: str = "") -> str:
    """把触发指令与场景拼成模型输入，与训练格式一致。"""
    instruction = instruction.strip()
    context = (context or "").strip()
    if context:
        return f"{instruction}\n（背景：{context}）"
    return instruction


def fallback_reply(kind: str, instruction: str = "") -> str:
    if kind in FALLBACK:
        return FALLBACK[kind]
    for words, reply in CHAT_FALLBACK_RULES:
        if any(w in instruction for w in words):
            return reply
    return FALLBACK["chat"]


# 提醒必须说到的核心词。
# 实测发现：模型学会了小林的口气，但在指令变体上会把"水/睡/饭"这类核心词丢掉
# （"嘴巴干不干自己知道，去接一杯"——语义对，但没说到水）。
# 提醒是功能，不是闲聊，不能靠概率。核心词没出现就用兜底模板兜住。
REMIND_CORE_WORDS = {
    "water": ["水", "喝", "灌", "接一杯", "润"],
    "sleep": ["睡", "关灯", "闭眼", "躺", "歇", "早点", "到此为止"],
    "sit": ["坐", "起来", "走动", "站", "动", "腰", "脖子", "伸"],
    "eat": ["吃", "饭", "垫", "饿", "空着肚子", "食堂"],
    "eyes": ["眼睛", "眼", "看远", "抬头", "歇", "屏幕", "窗外", "窗边"],
}


def ensure_remind_output(kind: str, reply: str) -> str:
    """提醒内容的第二道保险：说了但没说到点上，就换成模板句。

    这是"内容可靠性归程序、语言表达归模型"的具体落点：
    模型负责把人话说得像小林，程序负责保证这句话确实是在提醒这件事。
    """
    if not reply:
        return FALLBACK.get(kind, FALLBACK["chat"])
    words = REMIND_CORE_WORDS.get(kind)
    if not words:
        return reply
    if any(w in reply for w in words):
        return reply
    return FALLBACK.get(kind, reply)
