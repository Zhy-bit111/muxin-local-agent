# desk-companion · 本地陪伴型小模型助手

一个跑在自己笔记本上的日常陪伴助手：会闲聊，会在该喝水、该睡觉的时候提醒你。

- **基座**：Qwen2.5-1.5B-Instruct
- **训练方式**：LoRA 监督微调（SFT），fp16 不量化
- **硬件**：RTX 4060 Laptop 8GB / Ryzen 7 8845HS / 24GB RAM
- **项目根目录**：`E:\AI-Work\desk-companion`

## 一、这个项目在解决什么

目标不是做一个"通用大模型"，而是做一个**能在本地跑、说话像室友、能按时提醒**的小助手。

能力边界（重要，决定了架构）：

| 需求 | 由谁负责 | 为什么 |
|---|---|---|
| 几点该喝水、该睡觉 | **程序**（`app/scheduler.py` 的定时器） | 模型没有时间感，让模型"记得"提醒是不可靠的 |
| 提醒时说什么话 | **模型**（LoRA 微调后的语气与话术） | 这部分才是语言模型该干的 |
| 闲聊回应 | **模型** | 基座本身就有能力，微调主要调风格 |
| 记录你几点睡的 | **程序 + 本地文件** | 属于状态管理，不属于语言模型 |

一句话：**时间与状态归程序，语言表达归模型**。这条分工是这个项目最重要的设计决策，也是微调能不能成功的前提。

## 二、目录结构

```
desk-companion/
├── README.md                  项目总说明（本文件）
├── requirements.txt           依赖清单
├── configs/
│   ├── lora_qwen25_1.5b.yaml      LoRA 训练超参（v1）
│   └── lora_qwen25_1.5b_v2.yaml   同样的超参，只改 output_dir（v2 边界扩充版）
├── data/
│   ├── raw/                   原始种子数据（脚本生成的 jsonl）
│   ├── processed/             清洗后的训练集 / 验证集
│   └── eval/                  评测集（人工写，绝不进训练）
├── app/
│   ├── persona.py             人设与兜底话术
│   ├── llm_client.py          模型加载与生成封装
│   ├── scheduler.py           定时提醒调度
│   └── main.py                主入口（对话 + 后台提醒）
├── scripts/
│   ├── gen_seed_data.py       生成种子训练数据
│   ├── build_dataset.py       清洗、去重、切分
│   ├── train_lora.py          LoRA 训练
│   ├── merge_lora.py          合并适配器导出完整模型
│   ├── eval.py                跑评测集，算通过率
│   ├── smoke_app.py           真机联调冒烟（6 条闲聊 + 5 类提醒）
│   ├── run_eval_all.ps1       用统一口径串行跑三版评测
│   └── setup_env.ps1          一键建虚拟环境
├── docs/
│   ├── 01-项目设计.md          需求、架构、技术选型与理由
│   ├── 02-开发日志.md          逐次开发记录（每做一步就追加）
│   ├── 03-数据集规范.md        数据格式、分类、质量标准
│   └── 04-环境与训练手册.md    装环境、训练、排错
├── logs/                      运行日志（程序输出）
├── models/                    基座与适配器缓存
└── output/                    合并后的模型、评测报告
```

## 三、快速开始

```powershell
# 0. 所有命令都在项目根执行
cd E:\AI-Work\desk-companion

# 1. 建环境（首次，需要联网）
powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1

# 2. 生成并清洗数据
.\.venv\Scripts\python.exe .\scripts\gen_seed_data.py
.\.venv\Scripts\python.exe .\scripts\build_dataset.py

# 3. 训练 LoRA（v2 配置，438 步，约 27 分钟）
.\.venv\Scripts\python.exe .\scripts\train_lora.py --config configs\lora_qwen25_1.5b_v2.yaml

# 4. 评测（基座 / v1 / v2 三版同口径，约 5 分钟）
powershell -ExecutionPolicy Bypass -File .\scripts\run_eval_all.ps1

# 5. 合并成完整模型（可选，交付或转 GGUF 用）
.\.venv\Scripts\python.exe .\scripts\merge_lora.py --adapter output/lora-v2 --out output/merged-qwen25-1.5b

# 6. 启动助手（两种方式任选）
.\.venv\Scripts\python.exe .\app\main.py                                                # 挂 adapter，默认 v2
.\.venv\Scripts\python.exe .\app\main.py --model output/merged-qwen25-1.5b --no-adapter  # 用合并模型
```

运行起来之后：

- 直接打字就是闲聊；`/status` 看提醒状态，`/quiet` 暂停提醒，`/resume` 恢复，`/quit` 退出
- 喝水默认每 60 分钟提醒一次（`--water-interval` 可改），睡觉默认 23:30（`--sleep-time` 可改）

## 四、开发路线图

| 阶段 | 内容 | 产出 | 状态 |
|---|---|---|---|
| M0 | 项目骨架、文档、数据规范 | 目录 + 本文档 | 已完成 |
| M1 | 环境、基座跑通推理 | 基座 fp16 上 cuda，三句真实生成验证 | 已完成（11-09） |
| M2 | 数据生成与清洗 | train 1161 / valid 128 / eval 60 | 已完成（11-09） |
| M3 | LoRA 训练 + 评测 | v2 adapter，评测 96.7% | 已完成（11-10） |
| M4 | 提醒调度 + 主程序联调 | 真机联调 11/11，合并模型可跑 | 已完成（11-10） |
| M5 | 打包成开机自启 / 托盘程序 | 日常可用的成品 | 待做（可选） |

## 五、日志规范（这个项目要求）

每完成一次实质开发，就在 `docs/02-开发日志.md` **追加**一节，格式固定：

```
### YYYY-MM-DD HH:MM · 做了什么
- 改动：具体文件与内容
- 结果：跑出来的数字或现象
- 问题：遇到什么坑，怎么解决
- 下一步：紧接着要做什么
```

规则三条：
1. 只追加，不改写历史（防止后期美化过程）
2. 记录**数字**，不写"效果不错"这种话
3. 遇到坑必须写清"怎么解决的"，这是日志最值钱的部分

## 六、硬件约束与已知坑

| 约束 | 影响 | 应对 |
|---|---|---|
| 显存 8GB | 7B 全参/QLoRA 不可行 | 只用 1.5B，fp16 LoRA，`batch=1` + 梯度检查点 |
| Windows + bitsandbytes 4bit 不稳 | QLoRA 路径容易崩 | **不用 4bit**，直接 fp16 LoRA |
| 笔记本散热 | 长时间训练降频、可能过热 | 训练时限功耗、单次不超过 1.5 小时、中断续训 |
| Python 3.13 | 部分包轮子滞后 | 若某包装不上，退到 3.12 建 venv |

## 七、当前状态（2026-11-10）

**已经跑通的**
- 基座 Qwen2.5-1.5B-Instruct 本地加载：fp16 上 cuda，338 个权重张量约 3 秒
- **v2 LoRA**：438 步 / **26 分 39 秒** / 最终 loss **0.3611** / 峰值显存 **4.09 GB**（8.59 GB 卡）/ adapter **70.5 MB**
- **合并模型**：`output/merged-qwen25-1.5b/model.safetensors` **2944.4 MB**，不挂 adapter 可直接加载
- **真机联调**：6 条闲聊 + 5 类提醒全部通过（11/11），推理峰值 2.89–3.01 GB

**评测（60 条留出集，同一个 judge）**

| 版本 | 提醒 25 | 闲聊 20 | 边界 15 | 总体 |
|---|---|---|---|---|
| 基座 base | 60.0% | 95.0% | 20.0% | 61.7% |
| v1 LoRA | 100% | 100% | 73.3% | 93.3% |
| **v2 LoRA（默认）** | 92.0% | 100% | 100% | **96.7%** |

**已知局限（别被 96.7% 骗了）**

1. 60 条样本、自动指标只看格式 / 关键词 / 拒答词，**看不懂语义质量**
2. 闲聊有明显答非所问：「我今天把训练跑完了」→「哈，这也很正常，今天就到这儿吧」；「晚安」→「诶，在呢，今天挺稳的」
3. 根因：闲聊只有 6 个主题 540 条、output 重复率 32%，模型学到的是高频短语的分布，不是语义对齐
4. v3 该做的：闲聊主题扩到 15–20 个、重复率压到 20% 以下、temperature 0.7→0.5

**一条该记住的原则**

提醒这种"功能"必须由程序兜底 —— `app/persona.py` 的 `ensure_remind_output()` 会在模型输出丢掉核心词（比如该说"水"结果只说"去接一杯"）时换成模板句。模型负责把话说得像人，程序负责保证这句话确实在说那件事。
