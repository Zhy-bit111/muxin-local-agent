#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LoRA 监督微调 Qwen2.5-1.5B-Instruct。

用法：
    python scripts/train_lora.py --config configs/lora_qwen25_1.5b.yaml
    python scripts/train_lora.py --config configs/lora_qwen25_1.5b.yaml --max_steps 20   # 冒烟测试

产物：
    output/lora-qwen25-1.5b/   （LoRA adapter + tokenizer）
"""

import argparse
import json
from pathlib import Path

import torch
import yaml
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

ROOT = Path(__file__).resolve().parent.parent

# 人设：训练与推理必须用同一段 system prompt，否则行为会漂移
# v4：改成猫的自我认知（app/persona.py 必须同步，否则评测口径不一致）
SYSTEM_PROMPT = (
    "你叫慕心，是一只住在老周电脑里的猫。你会说人话，平时像个熟人一样聊天。"
    "你偶尔会露出猫的习惯：想晒太阳、喜欢纸箱、被摸头会呼噜、犯困就打哈欠，"
    "但不要每句话都提自己是猫，也不要喵喵叫。"
    "提醒老周喝水、睡觉的时候只说一次，说清楚就行。"
    "不知道的事就说不知道，不要自称人工智能或助手。"
)

IGNORE_INDEX = -100


def load_rows(path: Path):
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def build_user_text(row) -> str:
    instr = row["instruction"].strip()
    ctx = (row.get("input") or "").strip()
    if ctx:
        return f"{instr}\n（背景：{ctx}）"
    return instr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/lora_qwen25_1.5b.yaml")
    ap.add_argument("--max_steps", type=int, default=-1, help=">0 时只跑若干步，用于冒烟测试")
    ap.add_argument("--resume", action="store_true", help="从 output_dir 里最新的 checkpoint 续训")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / args.config).read_text(encoding="utf-8"))

    # ---- 基座：本地优先（兼容 models/Qwen/xxx 与 models/xxx 两种结构），其次 HF id ----
    candidates = [
        ROOT / cfg["model_cache"] / cfg["base_model"],
        ROOT / cfg["model_cache"] / cfg["base_model"].split("/")[-1],
    ]
    model_path = next((str(p) for p in candidates if p.exists()), cfg["base_model"])
    print(f"[model] {model_path}")

    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        torch_dtype=torch.float16,
        attn_implementation="sdpa",
        trust_remote_code=True,
    )
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()

    lora_cfg = LoraConfig(
        r=cfg["lora_r"],
        lora_alpha=cfg["lora_alpha"],
        lora_dropout=cfg["lora_dropout"],
        target_modules=cfg["target_modules"],
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_cfg)
    model.print_trainable_parameters()

    # ---- 数据 ----
    train_rows = load_rows(ROOT / cfg["train_file"])
    valid_rows = load_rows(ROOT / cfg["valid_file"]) if (ROOT / cfg["valid_file"]).exists() else []

    # transformers 5.x 的 apply_chat_template(tokenize=True) 返回 tokenizers.Encoding /
    # BatchEncoding，不是 list[int]，必须显式取 input_ids，否则 Dataset 会报 Arrow 类型溢出
    def _to_ids(messages, add_gen: bool) -> list:
        enc = tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=add_gen,
            return_dict=True,
        )
        return list(enc["input_ids"])

    stat = {"mismatch": 0}

    def encode(row):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_text(row)},
        ]
        prompt_ids = _to_ids(messages, True)
        full_messages = messages + [{"role": "assistant", "content": row["output"].strip()}]
        full_ids = _to_ids(full_messages, False)

        # 只对 assistant 段计算 loss；要求 prompt 是完整序列的严格前缀
        if len(full_ids) > len(prompt_ids) and full_ids[: len(prompt_ids)] == prompt_ids:
            labels = [IGNORE_INDEX] * len(prompt_ids) + full_ids[len(prompt_ids):]
        else:
            # 前缀不一致（BPE 边界合并等）时退化为全长计算，并计数以便发现
            stat["mismatch"] += 1
            labels = list(full_ids)

        max_len = cfg["max_length"]
        input_ids = full_ids[:max_len]
        labels = labels[:max_len]
        return {"input_ids": input_ids, "labels": labels, "attention_mask": [1] * len(input_ids)}

    train_ds = Dataset.from_list([encode(r) for r in train_rows])
    valid_ds = Dataset.from_list([encode(r) for r in valid_rows]) if valid_rows else None
    print(f"[data] train {len(train_ds)} / valid {len(valid_ds) if valid_ds else 0}")
    print(f"[data] prompt 前缀不一致（退化为全长 loss）样本: {stat['mismatch']}")

    pad_id = tokenizer.pad_token_id

    def collate(features):
        max_len = max(len(f["input_ids"]) for f in features)
        input_ids, labels, masks = [], [], []
        for f in features:
            pad = max_len - len(f["input_ids"])
            input_ids.append(f["input_ids"] + [pad_id] * pad)
            labels.append(f["labels"] + [IGNORE_INDEX] * pad)
            masks.append(f["attention_mask"] + [0] * pad)
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
            "attention_mask": torch.tensor(masks, dtype=torch.long),
        }

    out_dir = ROOT / cfg["output_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.resume:
        ckpts = sorted(out_dir.glob("checkpoint-*"), key=lambda p: int(p.name.split("-")[-1]))
        if ckpts:
            print(f"[resume] 将接续 {ckpts[-1].name}（若它来自超参不同的冒烟，lr 会错乱，建议先移走）")
        else:
            print("[resume] 未找到 checkpoint，将从头训练")

    # transformers 5.x 移除了 warmup_ratio，只认 warmup_steps，这里按比例换算
    steps_per_epoch = max(1, len(train_ds) // (cfg["batch_size"] * cfg["grad_accum"]))
    total_steps = args.max_steps if args.max_steps > 0 else steps_per_epoch * cfg["num_epochs"]
    warmup_steps = max(1, int(total_steps * cfg["warmup_ratio"]))
    print(f"[train] 预计总步数 {total_steps}，warmup {warmup_steps} 步")

    training_args = TrainingArguments(
        output_dir=str(out_dir),
        per_device_train_batch_size=cfg["batch_size"],
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=cfg["grad_accum"],
        num_train_epochs=cfg["num_epochs"],
        learning_rate=float(cfg["learning_rate"]),
        lr_scheduler_type=cfg["lr_scheduler"],
        warmup_steps=warmup_steps,
        weight_decay=cfg["weight_decay"],
        max_grad_norm=cfg["max_grad_norm"],
        logging_steps=cfg["logging_steps"],
        save_steps=cfg["save_steps"],
        save_total_limit=2,
        bf16=cfg["bf16"],
        fp16=False,
        optim="adamw_torch",
        gradient_checkpointing=cfg["gradient_checkpointing"],
        dataloader_num_workers=0,
        remove_unused_columns=False,
        report_to="none",
        seed=cfg["seed"],
        max_steps=args.max_steps,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=valid_ds,
        data_collator=collate,
    )

    print("[train] 开始训练，显存监控：")
    if torch.cuda.is_available():
        print(f"  已分配 {torch.cuda.memory_allocated()/1e9:.2f} GB")
    trainer.train(resume_from_checkpoint=True if args.resume else None)
    if torch.cuda.is_available():
        print(f"[显存] 训练峰值 {torch.cuda.max_memory_allocated()/1e9:.2f} GB / 总 {torch.cuda.get_device_properties(0).total_memory/1e9:.2f} GB")

    # 只保存 adapter，体积约几十 MB
    model.save_pretrained(out_dir)
    tokenizer.save_pretrained(out_dir)
    print(f"[done] adapter 已保存到 {out_dir}")


if __name__ == "__main__":
    main()
