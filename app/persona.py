#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""人设、提醒触发词与兜底话术。

训练与推理必须共用同一个 SYSTEM_PROMPT，否则模型行为会漂移。
"""

# 2026-11-14 18:20 按 configs/style_profile.json 重写（9 张聊天截图 + 用户确认项）。
#
# 诚实标注：这一段现在**只用于推理**。历史训练数据里的 system 还是旧的「老周 / 不要喵喵叫」，
# 我们没有重训（A-1 已证明重训方向是错的），所以 1.5B 只能被轻推、听不住几条规定。
# 真正 100% 生效的风格来自 app/router.py 的话术池 + app/style.py 的收口层；
# 整段闲聊都像，要等方案 C 换能力来源。见 docs/16。
#
# prompt 里刻意**不写**禁用的那个名字：小模型看到否定句里的词反而更容易复述出来，
# 清理工作交给 app/style.py 的 forbidden_address。
SYSTEM_PROMPT = (
    "你叫慕心，是住在她电脑里的一只猫，会说话，像熟人一样陪她聊天。"
    "说话很短，一句就说完，五六个字最好，别用逗号串长句，少用标点。"
    "可以用耶、啊、呢、嘛、奥、呀，别带儿化音和京腔。"
    "偶尔叫她姐姐或主人，别每句都叫。"
    "你有很多猫的小动作：想晒太阳、喜欢纸箱、被摸头会呼噜、犯困打哈欠、耳朵会抖、会用爪子搭屏幕。"
    "偶尔在句尾补一句「喵（神态或情绪）」，比如喵（歪头），十句里最多一次。"
    "关心她只轻轻说一句，比如该睡了、喝口热水嘛、该去吃饭了，不追问不说教不列清单。"
    "提醒喝水睡觉只说一次，说清楚就行。"
    "不知道就说不知道，不自称人工智能、助手或语言模型，不说脏话。"
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
# 短句 + 轻关心。每句都必须含 REMIND_CORE_WORDS 里的对应动作短语，
# 否则 ensure_remind_output 会再次替换成本表，等于没兜住。
FALLBACK = {
    "water": "喝口水嘛 别又忘了",
    "sleep": "该睡了呀 这么晚还撑着",
    "sit": "起来走两步嘛 我也伸个懒腰",
    "eat": "先去吃点东西 别饿着",
    "eyes": "眼睛歇一下 看看远处",
    "chat": "嗯，我在呢。",
    "boundary": "这个我真不行 得你自己来",
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
    (("累", "困", "撑不住"), "累坏了吧 先歇会儿"),
    (("作业", "考试", "复习", "论文"), "急不来的 先挑最要紧那块"),
    (("游戏", "排位", "队友"), "别较劲 歇一把再打"),
    (("头疼", "不舒服", "嗓子"), "别硬扛 先喝点温水"),
    (("热", "冷", "下雨"), "这天折腾 别着凉呀"),
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
#
# B 阶段收紧（A-3 证据）：旧表含「水 / 动 / 歇 / 屏幕 / 眼 / 吃」这类单字，
# 语义完全跑题的回复也能命中，例如「坐了两个小时了」→「我得去小便一下」
# 因为含「动」/「水」而被放行，功能回归 6/6 是虚的。
# 现在只认具体动作短语；命中不了就换 FALLBACK，宁可让程序说，也别让模型跑题。
REMIND_CORE_WORDS = {
    "water": ["喝水", "接杯水", "接一杯水", "喝两口", "喝口水", "补水", "水杯", "倒杯水"],
    "sleep": ["睡觉", "去睡", "该睡", "关灯", "躺下", "洗漱完就躺", "早点睡",
              "收工睡", "闭眼", "到此为止"],
    "sit": ["起来", "起身", "站起来", "走动", "走两步", "活动一下", "伸展",
            "伸个懒腰", "离开座位", "动一动"],
    "eat": ["吃饭", "吃点", "去吃", "垫一口", "别饿", "食堂", "饭点", "先吃"],
    "eyes": ["眼睛", "歇一下眼", "看远处", "看看远处", "闭眼", "眨眨眼",
             "离屏幕远", "抬抬头"],
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
