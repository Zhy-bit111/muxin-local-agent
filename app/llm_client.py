#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模型加载与生成封装。

- mock 模式：不加载模型，用 persona 里的兜底话术，用于先验证调度链路
- adapter 模式：基座 + LoRA adapter
- 纯基座模式：--no-adapter
"""

import re
from pathlib import Path

from app import persona


class LLMClient:
    def __init__(
        self,
        model_path: str,
        adapter_path: str | None = None,
        device: str | None = None,
        mock: bool = False,
    ):
        self.mock = mock
        self.model = None
        self.tokenizer = None
        self.device = device or "cuda"

        if mock:
            return

        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if self.device == "cuda" and not torch.cuda.is_available():
            print("[llm] CUDA 不可用，退回 CPU（会很慢）")
            self.device = "cpu"

        dtype = torch.float16 if self.device == "cuda" else torch.float32
        print(f"[llm] 加载基座: {model_path} ({self.device})")
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=dtype,
            attn_implementation="sdpa",
            trust_remote_code=True,
        ).to(self.device)
        self.model.eval()

        if adapter_path and Path(adapter_path).exists():
            from peft import PeftModel

            print(f"[llm] 挂载 LoRA: {adapter_path}")
            self.model = PeftModel.from_pretrained(self.model, adapter_path).to(self.device)
            self.model.eval()

    @staticmethod
    def _clean(text: str) -> str:
        text = text.replace("<|im_end|>", "").replace("<|endoftext|>", "").strip()
        # 去掉模型可能模仿出的说话人前缀（v4 起人设叫慕心）
        text = re.sub(r"^(小林|慕心|助手|AI)[:：]\s*", "", text)
        # 取第一段，避免输出分点或长篇
        for sep in ("\n", "  "):
            if sep in text:
                text = text.split(sep)[0]
        return text.strip()[:120]

    def chat(self, user_text: str, kind: str = "chat", memory_block: str = "",
             seed: int | None = None,
             history: list[tuple[str, str]] | None = None) -> str:
        if self.mock or self.model is None:
            return persona.fallback_reply(kind, user_text)

        import torch

        # 记忆拼进 user 消息的「（背景：…）」，与训练样本的格式完全一致。
        # 不要放进 system prompt：训练数据里 system 只有人设，多一段模型没见过的东西，
        # 1.5B 只会当噪声忽略（v3 实测过，加了记忆回复几乎没变化）。
        user_content = user_text
        if memory_block:
            user_content = f"{user_text}\n（背景：{memory_block}）"

        messages = [{"role": "system", "content": persona.SYSTEM_PROMPT}]
        # B 阶段：多轮上下文以真实消息传入，不再压成「刚才聊到…」的背景噪声。
        # bot 为空（评测集只给了上一轮用户话）时只加 user 消息，不编造助手回复。
        for u, b in (history or []):
            if u:
                messages.append({"role": "user", "content": u})
            if b:
                messages.append({"role": "assistant", "content": b})
        messages.append({"role": "user", "content": user_content})
        # 先渲染成纯文本再 tokenize：transformers 5.x 的 apply_chat_template(tokenize=True)
        # 返回的是 BatchEncoding，不能直接喂给 generate
        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        enc = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        input_len = enc["input_ids"].shape[1]

        # 评测要求可复现：每条样本生成前重置种子，结果与题目顺序无关
        if seed is not None:
            torch.manual_seed(seed)

        with torch.no_grad():
            out = self.model.generate(
                **enc,
                max_new_tokens=96,
                do_sample=True,
                temperature=0.7,
                top_p=0.9,
                repetition_penalty=1.1,
                pad_token_id=self.tokenizer.pad_token_id,
            )
        reply = self.tokenizer.decode(out[0][input_len:], skip_special_tokens=True)
        reply = self._clean(reply)
        return reply or persona.fallback_reply(kind, user_text)
