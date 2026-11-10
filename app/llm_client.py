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
        # 去掉模型可能模仿出的说话人前缀
        text = re.sub(r"^(小林|助手|AI)[:：]\s*", "", text)
        # 取第一段，避免输出分点或长篇
        for sep in ("\n", "  "):
            if sep in text:
                text = text.split(sep)[0]
        return text.strip()[:120]

    def chat(self, user_text: str, kind: str = "chat") -> str:
        if self.mock or self.model is None:
            return persona.fallback_reply(kind, user_text)

        import torch

        messages = [
            {"role": "system", "content": persona.SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]
        # 先渲染成纯文本再 tokenize：transformers 5.x 的 apply_chat_template(tokenize=True)
        # 返回的是 BatchEncoding，不能直接喂给 generate
        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        enc = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        input_len = enc["input_ids"].shape[1]

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
