#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""长期记忆：本地 JSON 存用户画像、事实与近期对话。

设计原则（很重要，决定了为什么是代码而不是模型）：

1. **记忆归程序，不归模型。** 1.5B 没有可靠的"记住"能力，让它在上下文里
   自己维持记忆必然丢。所以：写入靠规则（显式"记住…"指令 + 自动存对话），
   读取靠检索（关键词打分），模型只负责把这些事实说进话里。
2. **注入给模型的记忆块必须短。** 1.5B 的注意力很有限，塞长了它会把记忆
   和当前问题搅在一起，出现答非所问。这里硬限制在 160 字以内。
3. **不覆盖、只追加。** 事实带时间戳，去重靠归一化文本比对；
   文件写入用临时文件 + 原子替换，避免断电写坏。
"""

import json
import re
from datetime import datetime
from pathlib import Path

MAX_EPISODES = 40          # 最近对话保留条数
MAX_FACTS = 80             # 事实上限，超出丢最旧的
BLOCK_FACT_LIMIT = 4       # 注入 prompt 时最多带几条事实
BLOCK_BUDGET = 160         # 记忆块字符上限

# 显式记忆指令："记住我周三有早八" / "记一下：他喜欢冰美式"
REMEMBER_PATTERNS = [
    re.compile(r"^(?:请)?(?:你)?(?:记住|记一下|记好|记着|帮我记住|别忘了记)\s*[：:,，]?\s*(.+)"),
    re.compile(r"^(?:以后)?(?:记得|要记得)\s*[：:,，]?\s*(.+)"),
]

# 这些词开头的"记住…"其实是闲聊，不该当事实存
NOISE_PREFIX = ("吗", "什么", "为什么", "怎么", "是不是", "呢")

# 开放式回忆的意图线索：这类问句字面上与任何事实都不重叠
# （「还记得我说过什么吗」），需要兜底给最近一条事实才有得答。
# 其他情况一律不兜底，宁可没有背景，也不要无关背景。
RECALL_CUES = ("记得", "记得我", "还记得", "说过", "聊过", "提过", "之前", "上次", "我一般")


def to_third_person(text: str) -> str:
    """把用户的第一人称改成第三人称。

    用户说「记住我周三有早八」，存下来要变成「他周三有早八」：
    背景串是给模型看的，模型视角里"我"是自己（一只猫），不改会指错人。
    训练样本的记忆背景也统一用「他」，两边要一致。
    """
    return (
        (text or "")
        .replace("我们", "他们")
        .replace("咱们", "他们")
        .replace("我", "他")
    )


def _norm(text: str) -> str:
    """归一化：去空白与标点，用于事实去重。"""
    return re.sub(r"[\s，。！？、,.!?·:：;；\"'“”‘’()（）]", "", text).lower()


class Memory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = {"profile": {}, "facts": [], "episodes": []}
        self.readonly = False
        self.load()

    @classmethod
    def from_data(cls, data: dict) -> "Memory":
        """从内存字典构造一份只读实例，不读盘、不写盘。

        评测器用：每条题需要一份固定、可复现的记忆状态，
        但不能真的落到 `data/memory.json`（那会污染用户真实记忆）。
        """
        obj = cls.__new__(cls)
        obj.path = None
        obj.readonly = True
        obj.data = {
            "profile": dict(data.get("profile") or {}),
            "facts": list(data.get("facts") or []),
            "episodes": list(data.get("episodes") or []),
        }
        return obj

    # ---------------- 存取 ----------------
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            # 文件坏了不阻塞启动，保留一份 .bad 供排查
            try:
                self.path.with_suffix(".bad").write_text(
                    self.path.read_text(encoding="utf-8"), encoding="utf-8"
                )
            except Exception:
                pass
            return
        if isinstance(raw, dict):
            self.data.update({
                "profile": raw.get("profile") or {},
                "facts": raw.get("facts") or [],
                "episodes": raw.get("episodes") or [],
            })

    def save(self) -> None:
        if getattr(self, "readonly", False):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    # ---------------- 写入 ----------------
    def add_fact(self, text: str, source: str = "auto") -> bool:
        text = to_third_person((text or "").strip())
        if not text or len(text) > 60:
            return False
        if any(text.startswith(p) for p in NOISE_PREFIX):
            return False
        key = _norm(text)
        if not key:
            return False
        for f in self.data["facts"]:
            if _norm(f.get("text", "")) == key:
                # 已存在：刷新时间戳，保持它在"新鲜"一侧
                f["ts"] = datetime.now().isoformat(timespec="seconds")
                self.save()
                return False
        self.data["facts"].append({
            "text": text,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "source": source,
        })
        if len(self.data["facts"]) > MAX_FACTS:
            self.data["facts"] = self.data["facts"][-MAX_FACTS:]
        self.save()
        return True

    def parse_remember(self, user_text: str) -> str | None:
        """从一句话里识别显式记忆指令，成功则写入并返回被记住的内容。"""
        text = (user_text or "").strip()
        for pat in REMEMBER_PATTERNS:
            m = pat.match(text)
            if m:
                fact = m.group(1).strip()
                if fact and len(fact) <= 60:
                    self.add_fact(fact, source="explicit")
                    return fact
        return None

    def add_episode(self, user_text: str, bot_text: str, kind: str = "chat") -> None:
        user_text = (user_text or "").strip()
        if not user_text:
            return
        self.data["episodes"].append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "user": user_text[:80],
            "bot": (bot_text or "")[:80],
        })
        if len(self.data["episodes"]) > MAX_EPISODES:
            self.data["episodes"] = self.data["episodes"][-MAX_EPISODES:]
        self.save()

    def set_profile(self, key: str, value: str) -> None:
        self.data["profile"][key] = value
        self.save()

    # ---------------- 读取 ----------------
    @staticmethod
    def _score(query_key: str, fact: str) -> float:
        """相关性打分：2-gram 命中率 与 单字召回率 取较大者。

        B 阶段：只用 2-gram 时，「周三一早我有什么安排」对「他周三有早八」
        只命中「周三」= 1/9 = 0.11，会被判成无关，记忆题因此拿不到背景。
        单字召回能接住同义改写，但太宽松，所以乘 0.5 降权后与 2-gram 取 max：
        既让「周三/一早/有」这类线索生效，又不会让「现在几点了」被
        「他一般十二点左右睡」里的一个「点」字拽跑。
        """
        if not query_key or not fact:
            return 0.0
        fk = _norm(fact)
        grams = {query_key[i:i + 2] for i in range(len(query_key) - 1)}
        bigram = sum(1 for g in grams if g in fk) / len(grams) if grams else 0.0
        chars = set(query_key)
        char = len(chars & set(fk)) / len(chars) * 0.5 if chars else 0.0
        return max(bigram, char)

    def recall(self, query: str, limit: int = BLOCK_FACT_LIMIT) -> list[dict]:
        """只返回**与当前这句相关**的事实，无命中就返回空列表。

        B 阶段改：旧版在无命中时兜底给最近两条事实，A-3 实测这会把
        「他不太爱吃香菜」这类无关信息塞进每一句提问，模型据此编出
        「最近跑了一圈，还喝了点水」。fresh 只用于排序，不参与阈值判断。
        """
        qk = _norm(query)
        scored = []
        for idx, f in enumerate(self.data["facts"]):
            s = self._score(qk, f.get("text", ""))
            # 时间新鲜度：越新越优先，最多加 0.15，避免旧事实长期霸位
            fresh = (idx + 1) / max(1, len(self.data["facts"])) * 0.15
            scored.append((s, s + fresh, f))
        scored.sort(key=lambda x: -x[1])
        picked = [f for s, _, f in scored if s > 0.18][:limit]
        if (not picked and self.data["facts"]
                and any(c in (query or "") for c in RECALL_CUES)):
            # 只给最近 1 条（旧版无条件给 2 条），且只在明确是回忆式提问时。
            # A-3 的主要噪声源「刚才聊到…」已彻底移除，这里再压一层事实噪声。
            picked = self.data["facts"][-1:]
        return picked

    def build_block(self, query: str = "") -> str:
        """返回**纯事实串**，用于拼进 user 消息的「（背景：…）」。

        刻意不做成带标题的多行块：训练样本里的背景就是
        `（背景：他周三有早八）` 这种平铺串，推理侧格式必须与之一致，
        模型才可能真的用上记忆。带标题、换行、加提示语都会造成分布差异。

        B 阶段改（A-3 证据）：
        - **不再无条件追加「刚才聊到…」**。多轮上下文改由 `history` 以真实
          user/assistant 消息传入（见 `app/runtime_input.py`），压成一行噪声
          只会让 1.5B 把上一轮话题误当成当前话题
        - **无相关事实就不注入**：宁可没有背景，也不要无关背景
        """
        if not query:
            return ""
        parts = []
        name = self.data["profile"].get("name")
        if name:
            parts.append(f"他叫{name}")

        for f in self.recall(query):
            text = (f.get("text") or "").strip()
            if text:
                parts.append(text)

        return "；".join(parts)[:BLOCK_BUDGET]

    def stats(self) -> dict:
        return {
            "facts": len(self.data["facts"]),
            "episodes": len(self.data["episodes"]),
            "profile": len(self.data["profile"]),
            "file": str(self.path),
        }


if __name__ == "__main__":
    import sys

    mem = Memory(sys.argv[1] if len(sys.argv) > 1 else "data/memory.json")
    print("stats:", mem.stats())
    for f in mem.data["facts"][-10:]:
        print(f"  [{f['ts']}] {f['text']}")
