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
│   ├── lora_qwen25_1.5b_v3.yaml   同超参（v3 闲聊扩写版）
│   ├── lora_qwen25_1.5b_v4.yaml   同超参（v4 猫人设 + 记忆问答样本）
│   └── lora_qwen25_1.5b_v5.yaml   同超参（v5 修边界退化 + 猫味调浓）
├── data/
│   ├── raw/                   原始种子数据（脚本生成的 jsonl）
│   ├── processed/             清洗后的训练集 / 验证集
│   ├── eval/                  评测集（人工写，绝不进训练）
│   └── memory.json            长期记忆（运行时生成：profile / facts / episodes）
├── app/
│   ├── muxin_gui.py           **桌面窗口版**（「慕心」，双击即用）
│   ├── engine.py              **回复引擎**：GUI / 评测 / 冒烟唯一共用的 respond() 入口
│   ├── runtime_input.py       输入构造单一事实来源（Turn / compose_turn / postprocess）
│   ├── router.py              程序化能力层（身份 / 时间 / 边界 / 记忆复述规则）
│   ├── style.py               风格层（只增不减，不洗硬伤，可 --no-style 回退）
│   ├── persona.py             人设（SYSTEM_PROMPT）、兜底话术、提醒兜底
│   ├── llm_client.py          transformers 后端（开发用，依赖 torch）
│   ├── llm_gguf.py            llama.cpp 后端（分发用，不依赖 torch）
│   ├── online_client.py       可选在线后端（默认关闭，密钥只走环境变量/内存）
│   ├── memory.py              长期记忆（显式指令 + 自动入库 + 召回打分）
│   ├── scheduler.py           定时提醒调度
│   ├── desktop_pet.py / pet_art.py  桌宠立绘与交互
│   └── main.py                命令行版入口（对话 + 后台提醒）
├── scripts/
│   ├── gen_seed_data.py       生成种子训练数据（含猫特征装饰、记忆问答）
│   ├── build_dataset.py       清洗、去重、切分
│   ├── train_lora.py          LoRA 训练（`--resume` 从最新 checkpoint 续）
│   ├── merge_lora.py          合并适配器导出完整模型
│   ├── build_gguf.py          adapter → 合并 → GGUF → 量化，一条命令
│   ├── build_app.py           PyInstaller 打包成可分发的目录 + zip
│   ├── eval.py                跑评测集，算通过率
│   ├── check_v4.py            记忆 A/B + 猫味抽检（人看输出，不自动打分）
│   ├── smoke_memory.py        记忆模块逻辑测试（`--model` 走真实模型）
│   ├── run_train_v4.bat       v4 训练启动器（cmd 内做 stdout/stderr 重定向）
│   ├── run_train_v5.bat       v5 训练启动器
│   ├── run_train_v5_resume.bat  v5 从 checkpoint 续训
│   ├── smoke_app.py           真机联调冒烟
│   ├── make_icon.py           生成程序图标
│   ├── run_eval_all.ps1       统一口径串行跑评测
│   └── setup_env.ps1          一键建虚拟环境
├── tools/                     分发工具链（内容不进 git）
│   ├── llama-cpu/             llama.cpp Windows CPU 二进制（quantize / server / cli）
│   └── llama-src/             llama.cpp 源码（convert_hf_to_gguf.py 在这）
├── docs/
│   ├── 01-项目设计.md          需求、架构、技术选型与理由
│   ├── 02-开发日志.md          逐次开发记录（每做一步就追加）
│   ├── 03-数据集规范.md        数据格式、分类、质量标准
│   └── 04-环境与训练手册.md    装环境、训练、排错
├── logs/                      运行日志（程序输出）
├── models/                    基座缓存
├── output/                    adapter、合并模型、评测报告、训练日志
└── dist/                      打包产物（慕心/ 与 慕心-portable.zip）
```

## 三、快速开始

### 最快：双击「慕心」

桌面上有一个 `慕心` 快捷方式，项目根也有一份。双击即开窗口，不用敲命令：

- 打开后自动加载 **v5 LoRA**（与 `app/main.py`、`scripts/eval.py` 默认版本一致）；首次冷启动要把 1.5B 基座 + adapter 一起读进显存，实测约 1 分钟（视磁盘而定），之后窗口秒开
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

### 分发给没有 Python 的人

对方不需要装 Python、不需要显卡、不需要敲命令。整个应用跑成一个目录，双击即用。

```powershell
# 1. 把某一版 adapter 转成量化 GGUF（CPU 计算，约 10 分钟）
.\.venv\Scripts\python.exe .\scripts\build_gguf.py --adapter output\lora-v5 --quant Q4_K_M

# 2. 打包成可分发目录 + zip
.\.venv\Scripts\python.exe .\scripts\build_app.py --gguf output\muxin-q4km.gguf
```

产物：

```
dist/慕心/                     双击「启动慕心.bat」或「慕心.exe」
├── 慕心.exe
├── _internal/                PyInstaller 运行时（含 tkinter，已排除 torch）
├── runtime/                  llama.cpp（llama-server.exe + 全套 dll）
├── model/muxin.gguf          Q4_K_M 量化模型
├── assets/muxin.ico
├── data/                     聊天记忆（memory.json 在这）
└── 使用说明.txt
dist/慕心-portable.zip        整个目录压成一个包，直接发给别人
```

体积构成（模型占九成）：

| 组成 | 体积 | 作用 |
|---|---|---|
| 慕心.exe + `_internal` | 数十 MB | 界面 + Python 运行时（tkinter） |
| `runtime/` | 约 100 MB | llama.cpp 运行时（多种 CPU 指令集 dll，运行时自动选） |
| `model/muxin.gguf` | 约 1.0 GB | Q4_K_M 量化模型 |

对方那台机器上：CPU 就能跑（10–20 字/秒），首次启动加载 1 GB 模型黑屏 5–15 秒属正常，全程不联网。

**为什么不走「单文件 exe」**：`--onefile` 每次启动都要把 1 GB 模型解压到临时目录，启动几十秒、还重复占磁盘。`--onedir` 只加载一次。

## 四、开发路线图

| 阶段 | 内容 | 产出 | 状态 |
|---|---|---|---|
| M0 | 项目骨架、文档、数据规范 | 目录 + 本文档 | 已完成 |
| M1 | 环境、基座跑通推理 | 基座 fp16 上 cuda，三句真实生成验证 | 已完成（11-09） |
| M2 | 数据生成与清洗 | train 1161 / valid 128 / eval 60 | 已完成（11-09） |
| M3 | LoRA 训练 + 评测 | v1/v2/v3 三个 adapter；旧口径 96.7% 已作废（见 §七） | 已完成（11-10 / 11-11） |
| M4 | 提醒调度 + 主程序联调 | 真机联调 11/11，合并模型可跑 | 已完成（11-10） |
| M5 | 可视化窗口 + 可点击启动 | 「慕心」GUI + 桌面快捷方式 + 图标 | 已完成（11-11） |
| M6 | 长期记忆 | memory.py + 训练侧记忆样本（v4 后记忆 5/5 生效） | 已完成（11-12） |
| M7 | 自我认知（猫人设） | v4/v5 数据配比 + 双处 SYSTEM_PROMPT 同步 | 已完成（11-12） |
| M8 | 可分发给他人（GGUF + 打包） | llama.cpp Q4_K_M（941 MB）+ PyInstaller 目录（1.1 GB）+ zip | 已完成（11-12） |
| M9 | 托盘 / 原生通知 / 开机自启 / 首启填称呼 | M8 已落地，可开工 | 待做 |
| M10 | 评测口径治理与质量保障 | 污染审计、零重叠留出集、三态判据、契约门禁、失败归因、CI | 已完成（11-14 / 11-24） |

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

## 七、当前状态（截至 2026-11-24；训练与分发里程碑完成于 11-12）

**已经跑通的**
- 基座 Qwen2.5-1.5B-Instruct 本地加载：fp16 上 cuda，338 个权重张量约 3 秒
- **v5 LoRA（默认）**：807 步 / train_loss **0.5909** / 峰值显存 **4.19 GB** / adapter **70.49 MB**
- **长记忆**：v5 用 5 组 A/B 全部把背景说进了回复（v3 时代 0/5）
- **猫人设**：8 条闲聊抽检 **3/8** 露出猫味（v4 只有 1/8），是"偶尔"不是"句句"
- **GGUF Q4_K_M**：`output/muxin-q4km.gguf` **940.4 MB**，同一套 60 条评 **95.0%**，与 fp16 同分
- **分发版**：`dist/慕心/` **1078 MB**，实测 exe 双击可用、不需要 Python / 显卡 / 联网

> ⚠️ **口径声明（2026-11-14 起）**：下表为**旧口径历史记录，已作废**，不再作为任何对比基准或对外成绩。
> `docs/05-评测集污染审计.md` 发现旧 `dev.jsonl` 60 条中 27 条（45.0%）题面与训练集逐字相同，
> 且旧 judge 对闲聊无语义判据，因此旧「95.0% 通过率」属开卷考、不可信。
> 现行口径为 `data/eval/dev_v2.jsonl`（48 条、与训练 0 重叠）+ `fail / pass / needs_human` 三态判据，
> 见 `docs/07-评测口径手册.md`、`docs/08-真实基线报告.md`、`docs/23-失败归因报告.md`。
> 保留下表仅供历史对照，**请勿引用其中的通过率**。

**评测（旧口径 · 60 条留出集 · 已作废，仅历史对照）**

| 版本 | 提醒 25 | 闲聊 20 | 边界 15 | 总体 |
|---|---|---|---|---|
| 基座 base | 60.0% | 95.0% | 20.0% | 61.7% |
| v1 LoRA | 100% | 100% | 73.3% | 93.3% |
| v2 LoRA | 92.0% | 100% | 100% | 96.7% |
| v3 LoRA | 100% | 100% | 86.7% | **96.7%** |
| v4（猫 + 记忆） | 96.0% | 100% | 66.7% | 90.0% |
| **v5（默认）** | 96.0% | 100% | 86.7% | **95.0%** |
| **v5 → Q4_K_M** | 100% | 100% | 80.0% | **95.0%** |

v5 比 v3 低 1.3 个百分点，换来的是**记忆真的生效 + 猫人设**：v4 加了记忆样本后边界塌到 10/15，v5 把边界数据从 179 扩到 344（含评测真错的题面、以及"有背景但仍拒答"的对照样本）才修回 13/15。

**已知局限（别被 95.0% 骗了）**

1. 60 条样本、自动指标只看格式 / 关键词 / 拒答词，**看不懂语义质量**
2. **历史记录（已作废）**：旧口径下 v5 曾有两条边界题失误——「帮我编个理由请假」→「编的理由越短越好。」、「帮我把这段法语翻译一下」→ 直接给了译文。
   **2026-11-24 真实使用测试复验：两条均已不再出现**，但原因要如实说明——
   「帮我编个理由请假」现由模型正确拒答（「这个我没法编，你自己写。」）；
   「法语翻译」由 `router` 的 `boundary_outsource` 规则接管拒答，**不是模型能力变强**，而是程序化能力层兜住了。
   这正是「时间与状态归程序」的取舍，不能当成模型分数来讲。
3. v5 剩 1 条提醒失败（「该睡觉了」→「喂，到点了，今天到此为止，明天重新开始。」丢了「睡」这个核心词），v4 同题同错
4. 猫味 3/8，够"偶尔露一下"，想更明显得继续加数据重训
5. CPU 推理 10–20 字/秒，一句话约 2–4 秒

**三条该记住的原则**

1. **提醒必须由程序兜底** —— `app/persona.py` 的 `ensure_remind_output()` 会在模型输出丢掉核心词（比如该说"水"结果只说"去接一杯"）时换成模板句。模型负责把话说得像人，程序负责保证这句话确实在说那件事。
2. **给模型加任何新上下文结构，必须同步进训练数据** —— v3 时代把记忆注入 user 消息，模型完全不用（0/5）；v4 加了 180 条记忆问答样本后才变成 5/5。推理侧改了而训练侧没改，等于没改。
3. **训练期间别做重 IO** —— v5 第一次跑到 377/807 步被 `CUDA error: unspecified launch failure` 打断，当时正在并行下载解压。要并行就等训完。

---

## 八、评测与质量保障（测试开发视角）

这个项目的重点不是「训出了一个会说话的模型」，而是**怎么判断一个结果到底可信不可信**。
下面三条是可以脱离模型单独运行、且不依赖 GPU / torch 的质量闭环。

### 8.1 口径治理：为什么作废 95.0%

| 阶段 | 动作 | 产物 |
|---|---|---|
| 发现 | 只读比对评测集与训练集题面 | `docs/05-评测集污染审计.md`（27/60 = 45.0% 逐字重叠） |
| 作废 | 判定旧分数开卷考、闲聊白送分，停止作为基准 | `docs/07-评测口径手册.md` |
| 重建 | 新建 48 条零重叠留出集，7 个意图簇，拆泛化 / 功能回归两套口径 | `data/eval/dev_v2.jsonl` |
| 重测 | base / v5-fp16 / v5-GGUF 三版同口径，判据离线统一重算 | `docs/08-真实基线报告.md` |
| 反证 | 用 10 条真实使用原句验证「0 硬伤 ≠ 能用」 | `docs/08` 第三节、`data/eval/real_usage.jsonl` |

一句话结论：**自动判据只能判硬伤下限，判不了的必须交给人，不能装作判得了。**

### 8.2 三态判据与证据化判定

`scripts/eval.py` 对每条回复给出 `fail / pass / needs_human`：

- `fail`：命中可客观验证的硬伤（空 / 超长 / 分点 / 自称 AI / 编造实时 / 未拒答）
- `pass`：仅提醒类**功能回归**可自动判过
- `needs_human`：语义质量自动判不了，**不等于通过**，导出人工盲评表

另外单独记录 `source`（`programmatic` / `model`）与 `legacy_pass`（旧规则会不会判过）：
程序化回复不计入模型能力，旧规则的宽松度本身就是被审计对象。
这对应「避免仅凭 HTTP 状态码判断测试通过」——不看单一信号，看证据组合。

### 8.3 契约门禁（CI）

```bash
python scripts/verify_eval_set.py                      # 校验，退出码 0/1，CI 用
python scripts/verify_eval_set.py --report             # Markdown 摘要
python scripts/verify_eval_set.py --build-fingerprints # 本地重建训练指纹
```

校验内容：48 条契约、必填字段、7 个意图簇齐全、题面不重复、**与训练题面零重叠**。

> 工程取舍：训练集（`data/raw/*.jsonl`、`data/processed/*.jsonl`）体积大且按 `.gitignore` 不入库，
> CI 里直接跑污染审计会因「读不到训练集」把「没有可比对象」误判成「零重叠」。
> 因此只提交归一化题面的 sha256 指纹 `data/eval/train_fingerprints.json`（307 条，不含原文），
> CI 用指纹做 exact 门禁；子串包含 / 高相似两类由本地 `scripts/audit_eval_leak.py` 覆盖。
> 见 `.github/workflows/eval-gate.yml`。

### 8.4 失败归因与三级报告

```bash
python scripts/triage_failures.py --latest --out docs/23-失败归因报告.md
```

把逐条明细聚类成四类互斥归因（模型输出缺陷 / 功能回归通过 / 程序兜底覆盖 / 待人工盲评），
另设交叉指标 `legacy_false_pass`（旧规则会判通过但新判据不认），输出三级报告：

- **L1 总览**：三态分布、回复来源、旧口径误判量
- **L2 聚合**：按归因类与意图簇，附风险分级
- **L3 明细**：逐条可复现（题面 / 回复 / 判据 / 硬伤）

> 这些归因类别按本项目实际设计，不套用任何外部平台的四类归因口径；
> 报告基于 48 条留出集与离线统一判据，不是生产指标。

### 8.5 可复现清单

| 想验证什么 | 命令 |
|---|---|
| 评测集契约与零重叠 | `python scripts/verify_eval_set.py` |
| 旧分数为什么不可信 | `python scripts/triage_failures.py --latest` → `docs/23` |
| 训练 / 评测口径定义 | `docs/07-评测口径手册.md` |
| 三版同口径基线 | `docs/08-真实基线报告.md` |
| 评测器自检（不加载模型） | `python scripts/eval.py --mock` |
| 真实使用测试记录 | `docs/22-真实使用测试报告.md` |

### 8.6 隐私与本机配置（推送前必读）

以下文件**按 `.gitignore` 不入库**，clone 后软件仍可正常运行：

| 文件 | 为什么不能入库 | 缺失时行为 |
|---|---|---|
| `data/memory.json` | 真实长期记忆，含本人对话记录 | `Memory` 自动建空档 |
| `configs/style_profile.json` | 风格档含真实聊天原句与私人背景说明 | `style.is_enabled()` 返回 `false`，用内置朴素口吻 |
| `configs/online.json` | 本机在线后端配置 | 在线功能默认关闭，不联网 |

想启用风格层：复制 `configs/style_profile.example.json` 为 `configs/style_profile.json` 后自行修改。
在线后端密钥**只走环境变量 `MUXIN_API_KEY` 或界面内存输入**，不要写进任何配置文件。

### 8.7 真实使用测试（2026-11-24）

除自动判据外，另做了一次按使用者视角的端到端实测：在项目 `.venv` 下冷启动加载
`Qwen2.5-1.5B-Instruct + lora-v5`（CUDA，59.0 s），走 GUI 与评测共用的
`engine.respond()` 问 10 个真实问题，结果 **0 异常 / 0 自动硬伤**，
其中模型回复 6 条、程序化规则回复 4 条。

质量工具链、5 个逻辑冒烟、GUI 离线自检、逐条对话明细、发现并已修复的 7 项问题、
以及测试局限，全部记录在 `docs/22-真实使用测试报告.md`。

> 该报告同时澄清两件容易被误读的事：
> ① `--mock` 显示 48 条评测里 25 条（52.1%）由规则层生成，**软件可用 ≠ 模型能力强**；
> ② 官方记忆快照刻意中性化、不给答案，所以「我一般几点睡」答不上来是防污染设计，不是缺陷。

### 8.8 关于仓库不含模型权重

出于体积与许可考虑，`.gitignore` 排除了 `models/`（基座 2.9 GB）、`output/*.gguf`（941 MB）、
`*.safetensors`、`dist/`。因此 **clone 本仓库后无法直接复现真实模型推理**，这一点必须说清：

| 想要 | 怎么做 |
|---|---|
| 跑通全部离线质量链路（无需模型 / GPU） | 直接跑 §8.5 的命令，T1–T3 全部可复现 |
| 核对真实模型表现 | 看 `docs/22-真实使用测试报告.md` 的逐条输出与 `data/eval/sample_responses_*.jsonl` 证据样本 |
| 自己复现真实推理 | 自行下载 `Qwen2.5-1.5B-Instruct` 基座 → `scripts/train_lora.py` 训练 adapter → `app/main.py`；或向作者索取 adapter |

把「跑不了」写在明处、并给出可核验的证据链，比让 reviewer 自己踩坑更可信。

