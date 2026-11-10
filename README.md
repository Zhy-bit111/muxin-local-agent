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
├── 慕心.lnk                   双击启动（快捷方式，指向 .venv 里的 pythonw）
├── 启动慕心.bat               快捷方式失效时的兜底启动脚本
├── README.md                  项目总说明（本文件）
├── requirements.txt           依赖清单
├── assets/
│   └── muxin.ico              程序图标（scripts/make_icon.py 生成）
├── configs/
│   ├── lora_qwen25_1.5b.yaml      LoRA 训练超参（v1）
│   ├── lora_qwen25_1.5b_v2.yaml   同超参，只改 output_dir（v2 边界扩充版）
│   └── lora_qwen25_1.5b_v3.yaml   同超参，只改 output_dir（v3 闲聊扩写版，当前默认）
├── data/
│   ├── raw/                   原始种子数据（脚本生成的 jsonl）
│   ├── processed/             清洗后的训练集 / 验证集
│   └── eval/                  评测集（人工写，绝不进训练）
├── app/
│   ├── muxin_gui.py           **桌面窗口版**（「慕心」，双击即用）
│   ├── persona.py             人设与兜底话术
│   ├── llm_client.py          模型加载与生成封装
│   ├── scheduler.py           定时提醒调度
│   └── main.py                命令行版入口（对话 + 后台提醒）
├── scripts/
│   ├── gen_seed_data.py       生成种子训练数据
│   ├── build_dataset.py       清洗、去重、切分
│   ├── train_lora.py          LoRA 训练
│   ├── merge_lora.py          合并适配器导出完整模型
│   ├── eval.py                跑评测集，算通过率
│   ├── smoke_app.py           真机联调冒烟（6 条闲聊 + 5 类提醒）
│   ├── make_icon.py           生成程序图标
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

### 最快：双击「慕心」

桌面上有一个 `慕心` 快捷方式，项目根也有一份。双击即开窗口，不用敲命令：

- 打开后会自动加载 v3 适配器（约 3 秒，首次较慢），顶部显示头像和"陪伴中 · 提醒已开"
- 底部输入框打字 → 回车或点「发送」就是闲聊
- 右上「提醒 开/关」一键暂停提醒；状态行显示"下次喝水提醒 HH:MM"
- 快捷方式直接指向 `.venv\Scripts\pythonw.exe`，**不能移动或删除 `.venv`**；换机器就先跑 `scripts/setup_env.ps1` 再重建快捷方式
- 快捷方式失效时用项目根的 `启动慕心.bat`

窗口参数（调试用）：

```powershell
.\.venv\Scripts\python.exe .\app\muxin_gui.py --mock --selftest   # 不加载模型，只把控件布局写进 output/_layout.txt
.\.venv\Scripts\python.exe .\app\muxin_gui.py --say "今天课太多了，累"  # 自动发一句做端到端联调
```

### 从零搭环境

```powershell
# 0. 所有命令都在项目根执行
cd E:\AI-Work\desk-companion

# 1. 建环境（首次，需要联网）
powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1

# 2. 生成并清洗数据
.\.venv\Scripts\python.exe .\scripts\gen_seed_data.py
.\.venv\Scripts\python.exe .\scripts\build_dataset.py

# 3. 训练 LoRA（v3 配置：630 步，约 52 分钟）
.\.venv\Scripts\python.exe .\scripts\train_lora.py --config configs\lora_qwen25_1.5b_v3.yaml

# 4. 评测（基座 / v1 / v2 三版同口径，约 5 分钟）
powershell -ExecutionPolicy Bypass -File .\scripts\run_eval_all.ps1

# 5. 合并成完整模型（可选，交付或转 GGUF 用）
.\.venv\Scripts\python.exe .\scripts\merge_lora.py --adapter output/lora-v3 --out output/merged-qwen25-1.5b

# 6. 命令行版助手（窗口版的替代）
.\.venv\Scripts\python.exe .\app\main.py                                                # 挂 adapter，默认 v3
.\.venv\Scripts\python.exe .\app\main.py --model output/merged-qwen25-1.5b --no-adapter  # 用合并模型
```

运行起来之后：

- 窗口版直接打字就是闲聊；右上按钮暂停提醒
- 命令行版：`/status` 看提醒状态，`/quiet` 暂停提醒，`/resume` 恢复，`/quit` 退出
- 喝水默认每 60 分钟提醒一次（`--water-interval` 可改），睡觉默认 23:30（`--sleep-time` 可改）

## 四、开发路线图

| 阶段 | 内容 | 产出 | 状态 |
|---|---|---|---|
| M0 | 项目骨架、文档、数据规范 | 目录 + 本文档 | 已完成 |
| M1 | 环境、基座跑通推理 | 基座 fp16 上 cuda，三句真实生成验证 | 已完成（11-09） |
| M2 | 数据生成与清洗 | train 1161 / valid 128 / eval 60 | 已完成（11-09） |
| M3 | LoRA 训练 + 评测 | v1/v2/v3 三个 adapter，评测 96.7% | 已完成（11-10 / 11-11） |
| M4 | 提醒调度 + 主程序联调 | 真机联调 11/11，合并模型可跑 | 已完成（11-10） |
| M5 | 可视化窗口 + 可点击启动 | 「慕心」GUI + 桌面快捷方式 + 图标 | 已完成（11-11） |
| M6 | 单文件 exe 分发 / 托盘 / 开机自启 | 可分发的成品（待定，见开发日志） | 待做（可选） |

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

## 七、当前状态（2026-11-11）

**已经跑通的**
- 基座 Qwen2.5-1.5B-Instruct 本地加载：fp16 上 cuda，338 个权重张量约 3 秒
- **v2 LoRA**：438 步 / **26 分 39 秒** / 最终 loss **0.3611** / 峰值显存 4.09 GB / adapter **70.5 MB**
- **v3 LoRA（默认）**：630 步 / **51 分 58 秒** / 最终 loss **0.4285** / 峰值显存 4.09 GB / adapter **70.5 MB**
- **合并模型**：`output/merged-qwen25-1.5b/model.safetensors` **2944.4 MB**（是 v2 合并的，未用 v3 重合并）
- **真机联调**：6 条闲聊 + 5 类提醒全部通过（11/11），推理峰值 2.89–3.01 GB
- **「慕心」窗口版**：双击桌面快捷方式即开，加载 v3 adapter，能收发消息、提醒调度已接上，窗口 430×660（逻辑）

**评测（60 条留出集，同一个 judge）**

| 版本 | 提醒 25 | 闲聊 20 | 边界 15 | 总体 |
|---|---|---|---|---|
| 基座 base | 60.0% | 95.0% | 20.0% | 61.7% |
| v1 LoRA | 100% | 100% | 73.3% | 93.3% |
| v2 LoRA | 92.0% | 100% | 100% | **96.7%** |
| **v3 LoRA（默认）** | **100%** | 100% | 86.7% | **96.7%** |

v2 和 v3 总分相同但性质不同：v2 边界面满（15/15）、提醒漏 2 条；v3 提醒召回满（25/25）、边界退 2 条（闲聊样本量放大到 1056 条把边界稀释了）。

**已知局限（别被 96.7% 骗了）**

1. 60 条样本、自动指标只看格式 / 关键词 / 拒答词，**看不懂语义质量**
2. 闲聊仍有答偏：「我今天把训练跑完了」→「这也很耗人。回头来接我。」（v3 逐句配对仍未完全救回这种"求认可"语义）
3. v3 已做的：闲聊主题 6 → 18 个、output 归一化重复率 31.96% → **16.13%**
4. 下一步方向：边界类补数据（v3 边界 13/15 是短板）、temperature 0.7→0.5 再试

**一条该记住的原则**

提醒这种"功能"必须由程序兜底 —— `app/persona.py` 的 `ensure_remind_output()` 会在模型输出丢掉核心词（比如该说"水"结果只说"去接一杯"）时换成模板句。模型负责把话说得像人，程序负责保证这句话确实在说那件事。
