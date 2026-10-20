# Agent 本地评测问答

> 整理时间：2026-10-19
> 对应代码：`evaluate/`；对应报告：`evaluate/reports/`
> 结论先行：本次检索排序是健康的；真正的瓶颈是**记忆里没有本题答案**，其次是**隐藏测试要求精确的输出措辞**。

---

## 问题 1：你的 Agent 是如何测评的？

**本次的答案：**

不是让模型自由发挥，而是把官方链路拆成“记忆重建 → 解题 → 判分”三段，每段都可观测、可复现。

```
官方 /add 原始请求
        │
        ▼
MemoryStore（与 src/memory_engine.py 同一份代码）
        │
        ├── replay 模式：只回放官方 /search，量检索命中
        │
        └── agent 模式
                │
                ▼
        search_memory / 读文件 / grep / 改代码 / 跑命令
                │
                ▼
        git diff（模型补丁）
                │
                ▼
  重置工作区 → 应用模型补丁 → 应用官方 test patch → pytest
                │
                ▼
         FAIL_TO_PASS + PASS_TO_PASS 全过 = resolved
```

### 1）记忆重建

- 直接读取 `data_store/official_requests.jsonl` 里的 6 条真实 `/add` 请求，在进程内调用 `MemoryStore.add()` 重建记忆库。
- 不经过 HTTP，但用的就是服务端同一份 `memory_engine.py`，`/add` 的 messages、role、timestamp 都是官方原始数据。
- 自动识别两种记忆环境：
  - `relevant`：只含 astropy timeseries 轨迹的 user，2 个 Parent；
  - `noisy`：同一批 timeseries 轨迹 + progressbar / furlong 两条无关轨迹的 user，4 个 Parent。
- 识别依据是轨迹里有没有 timeseries 关键词，不写死 user_id；也可以用 `--user-id` 指定。

### 2）解题 Agent

- 工作区：每次从 `evaluate/work/.../template` 复制一份干净的 astropy 5.0 未修复版，Agent 的改动不会污染基准。
- 输入：任务的 `problem_statement` + 仓库根路径。
- 工具：`search_memory`、`list_dir`、`read_file`、`grep`、`write_file`、`edit_file`、`run_command`、`finish`。
- 约束：
  - 最多 3 次记忆检索，防止反复搜同一批内容；
  - 前 8 个工具调用内必须开始改源码；
  - 禁止联网（关键词拦截 + 不可达代理），否则模型会直接抓 GitHub 上游修复；
  - 上下文超预算时，把较早的大块工具结果替换成占位符；
  - 默认最多 20~40 步，测试文件不允许改（判分时也会从模型补丁里剔除）。
- Agent 每一步的 assistant 内容、工具参数、工具结果都会实时写入 `evaluate/reports/<run_id>.transcript.jsonl`，中途可以查看。

### 3）判分

- 取出 Agent 工作区的 `git diff` 作为模型补丁；新文件也会被纳入。
- 从模型补丁中剔除官方 test patch 会覆盖的测试文件，避免“改测试骗过验证”。
- 重置工作区，依次应用模型补丁、官方 test patch。
- 运行官方 `FAIL_TO_PASS`（1 个）和 `PASS_TO_PASS`（20 个）；全部通过才记 `resolved=True`。
- 报告记录：是否解决、通过/失败数量、失败节点、pytest 输出尾部、模型补丁、transcript、token 用量。

### 三种模式

| 模式 | 做什么 | 用途 |
| --- | --- | --- |
| `oracle` | 直接应用官方 gold patch + test patch | 证明评测链路本身没坏 |
| `replay` | 只回放官方 `/search`，不跑 Agent | 看记忆排序和命中 |
| `agent` | 完整解题 + 判分 | 本地模拟官方解题率 |

---

## 问题 2：本次的测评结果是什么？

**本次的答案：**

| 项目 | 结果 |
| --- | --- |
| oracle | **21/21 通过，resolved=True** |
| replay relevant | 15/15 查询命中相关 Parent，首个相关排名全是 1 |
| replay noisy | 15/15 命中，首个相关排名仍是 1，噪声没有挤掉相关记忆 |
| agent relevant（禁网） | **20 个 P2P 通过，1 个 F2P 失败，resolved=False** |
| agent 用量 | 20 步，约 24 万 token，模型 deepseek-v4-flash |

失败的具体原因不是“找不到代码”，而是**措辞不一致**：

```text
隐藏测试要求：
  TimeSeries object is invalid - expected ['time', 'a']
  as the first columns but found ['time', 'b']

Agent 实际写成：
  TimeSeries object is invalid - expected 'time', 'a'
  as the first columns but found 'time', 'b'
  and the following required columns are missing: 'a'
```

两者语义一致，但 pytest 断言的是精确字符串，所以失败。平台侧这题也没有解决，本地结果和平台一致。

---

## 问题 3：如果当前的瓶颈不是检索，那可能是什么？

**本次的答案：**

按可能性从高到低，分为五层。**本次的主因是第 1 层和第 2 层叠加**，不是排序算法。

### 第 1 层：记忆里根本没有本题答案（本次主因）

这是最容易被“命中率 100%”掩盖的一点。

- 两条 timeseries 轨迹的最终总结分别是：
  - 第 1 条：创建 `astropy/timeseries/` 子包；
  - 第 2 条：修复 `BinnedTimeSeries.to_pandas()` 的 `KeyError`。
- 本题要修的是 `_check_required_columns()` 的错误消息格式，**两条轨迹都没有给出这个修复**。
- 全库搜索 `as_scalar_or_list_str`、`expected ['time', 'a']`、`as the first columns but found` 等关键字符串：**0 命中**。
- 轨迹里唯一相关的内容是探索阶段读到的**修复前** `_check_required_columns` 代码，它只能告诉 Agent“问题在这里”，不能告诉 Agent“最终怎么写”。

所以 `replay` 的“命中相关 Parent”只能证明**主题相关**，不能证明**答案存在**。这是两个不同的指标：

| 指标 | 含义 | 本次结果 |
| --- | --- | --- |
| retrieval_hit@k | Top-K 里有没有同主题记忆 | 15/15，排名 1 |
| answer_hit@k | Top-K 内容里有没有 gold patch 的关键改法 | **0/15** |

检索系统无法返回语料里不存在的东西。这个题在当前记忆环境下，任何 BM25 调参、Top-K 调整、预算调整都不会改变结果。

### 第 2 层：输出契约不明确 + 隐藏测试精确匹配

- Issue 只说“异常应该告诉用户缺少必需列”，没有规定消息的精确格式。
- 隐藏测试却要求：多列用列表表示 `['time', 'a']`，单列保持 `'time'`。
- 这是一个**信息不足的规格**：语义正确的实现有很多种，只有一种能通过。
- Agent 读了 `core.py` 和现有测试，知道单列要保持 `'time'`，但多列选择了“用引号拼接”的自然写法，和上游的列表 `repr` 不同。
- 在官方环境里，解题 Agent 同样看不到 test patch；这类题本质上是在赌上游的措辞习惯。

### 第 3 层：Agent 策略与预算

- 本次 20 步，前 3 次记忆检索用完后才被迫改代码；第 10 步完成第一版修复，后面主要在做边界验证。
- 没有隐藏测试作为反馈，Agent 只能靠自写复现脚本验证“消息是否更清楚”，无法验证“精确字符串是否匹配”。
- 更多步数可以让 Agent 多试几种措辞，但它没有依据判断哪一种是对的；步数不是决定性问题。
- 禁网是必要的：不禁网时 Agent 直接抓到了 GitHub v5.1.1 的上游修复，21/21 通过。这反而证明**模型有能力应用正确修复，缺的是正确信息**。

### 第 4 层：模型能力差异

- 本地用 deepseek-v4-flash，官方用 gpt-4o-mini。
- 不同模型对“从 issue 和现有测试推断隐藏测试措辞”的能力不同。
- 这一层值得复测：用 `--model` 换成官方模型或更强模型，看失败是否仍是同样的措辞问题。
- 但即使换模型，第 1 层的信息缺口仍然存在，不能指望换模型就稳定解决。

### 第 5 层：评测环境与单题过拟合

- 本地基准是 astropy 5.0 未修复版，不是官方精确 base_commit；官方 test patch 能应用，oracle 21/21 通过，行为一致，但仍不是逐字节相同。
- smoke 只有 1 道题，官方说这题较难；单题结果不能代表 300 个评测单元。
- 如果针对这一题去改检索或加“尾部方案段保底”，很可能过拟合，而且在本题上也没用——因为尾部方案讲的是 `to_pandas`，不是 required-columns。

### 不是瓶颈的部分

- **不是排序**：15/15 首个相关排名 1，噪声没有挤掉相关。
- **不是 Top-K/预算**：Agent 直接读取了 `core.py`，也看到了修复前的函数。
- **不是响应延迟或存储**：链路本身 oracle 21/21 通过。
- **不是 Parent/unit 结构**：命中内容就是相关代码所在的消息。

---

## 问题 4：为什么“检索命中率 100%”仍然没解决？

**本次的答案：**

因为“相关”是**主题层面**的，不是**答案层面**的。

- 官方 smoke 的两条 timeseries 轨迹，对本题来说只是“同一模块的历史经验”，不是“同一 issue 的修复记录”。
- 官方 Agent 的 30 条查询也集中在 required columns，但它拿到的同样是这两条轨迹，最后平台也没解决。
- 这说明官方 `relevant` 条件的定义更接近“领域相关”，不是“答案一定在池子里”。
- 判断一个记忆环境是否有用，不能只看 hit rate，要看返回内容里有没有**可执行的改法**（符号、函数、最终 diff、明确的输出格式）。

本次 Agent 的真实路径是：

```
检索到“相关”轨迹（排名 1）
        ↓
轨迹讲的是 to_pandas / timeseries 子包
        ↓
没有 required-columns 的最终写法
        ↓
Agent 只能自己猜消息格式
        ↓
猜的格式和隐藏测试不一致 → 失败
```

所以失败点在第 2 步之后，而不是第 1 步。

---

## 问题 5：下一步建议做什么？

**本次的答案：**

1. **先加“answer_hit”指标，不要再用 hit rate 判断记忆是否有效。**
   在 replay 里检查返回内容是否包含 gold patch 的关键符号/字符串（例如 `as_scalar_or_list_str`、精确消息片段）。本题预期是 0/15；这个 0 比 15/15 的 hit rate 更能解释失败。
2. **用官方模型复测一次。**
   `--model` 换成 gpt-4o-mini 或更强模型，保持禁网、同样步数，看它是否能从 issue + 现有测试推断出列表 `repr`。这能区分“模型能力”与“信息缺失”。
3. **换一道“记忆里确实有答案”的题验证链路。**
   当前 smoke 的 relevant 记忆是主题相关但无答案；要验证检索改动是否真的有用，需要找一条轨迹末尾包含目标修复的任务。否则所有改动都会被这道题的错误信号带偏。
4. **不要为这一题改 BM25 或回退 V1.1 结构。**
   排序已经是第 1，问题不在排序；加“尾部方案段保底”也救不了本题（尾部是 to_pandas 总结）。这类改动应先在多题、answer-bearing 的数据上证明收益。
5. **把“输出契约推断”作为 Agent 的独立能力来评估。**
   可以观察它在有多条现有测试时，是否优先保持既有消息风格、是否尝试最小改动。本题它做了部分推理，但最终选了非上游写法；这是后续 prompt 或模型选择的优化点，不是检索问题。
6. **扩题。**
   单题只能做链路自检，不能做策略对比。官方是 150×2 个单元；本地至少要到十几道不同任务，结论才有统计意义。

---

## 一句话总结

**检索没有拖后腿：相关记忆排在第 1。真正拖后腿的是“相关 ≠ 有答案”，以及隐藏测试对错误消息措辞的精确要求；Agent 在没有 test patch、没有网络、记忆也不含修复的情况下，只能猜格式，猜错就失败。**
