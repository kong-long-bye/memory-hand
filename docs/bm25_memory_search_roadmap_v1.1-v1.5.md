# BM25-only 记忆检索改进路线（V1.1–V1.5）

> **范围锁定：本路线只优化 BM25，不引入 Dense、Embedding、向量数据库或 RRF。**  
> 核心方向是：保留 Add 的原始消息结构，以 message/chunk 作为检索单位，再将命中证据组织成合规的 Search 响应。每个版本尽量只改变一类变量，方便通过本地评测判断收益来自哪里。

- 当前基线：V1.0（扁平化 `messages`、按整条 memory/字符块建立 BM25 索引、命中块 + 邻居 + 无条件尾部、软预算）
- 目标版本：V1.1 → V1.2 → V1.3 → V1.4 → V1.5
- 官方接入参考：[Agent Memory Leaderboard API Guide](https://agentmemoryleaderboard.ai/api-guide)

---

## 1. 先统一几个概念

### 1.1 Parent Memory、Retrieval Unit 和 Search 返回项不是同一个概念

V1.1 起明确区分三个层级：

```text
一次 Add 请求
└── Parent Memory（完整保存本次 Add 的来源记录）
    ├── Message 0（role=user）
    │   ├── Retrieval Unit 0.0
    │   └── Retrieval Unit 0.1（仅当这条 message 太长时才切块）
    ├── Message 1（role=assistant）
    │   └── Retrieval Unit 1.0
    └── Message 2（role=user）
        └── Retrieval Unit 2.0
```

- **Parent Memory**：存储和来源追溯单位。当前服务中，一次 Add 请求原则上对应一个 Parent Memory；同一次 Add 的多个 `messages` 是它的子消息。
- **Message**：Add 请求中原始的消息，保留 `role`、`content`、`timestamp`（若提供）和原始顺序。
- **Retrieval Unit**：真正进入 BM25 的 document。通常一条 message 对应一个 unit；只有单条 message 太长时，才在该 message 内进一步切 chunk。
- **Search 返回项**：最终 `data` 数组中的一项。建议以 Parent Memory 为结果身份（`id` 使用稳定的 parent ID），但 `content` 只放从该 Parent 中检索出来的证据片段，不必把整个 Add 全文返回。

因此，**“一个 Add 是一个 Parent”不等于“整个 Add 是一个 BM25 document”**。BM25 应搜索更细的 Retrieval Units，再把命中证据归并到 Parent。

### 1.2 `top_k` 是上限，不是目标数量

官方契约要求 Search 返回的 `data` 数组长度**不得超过请求中的 `top_k`**；正式外部评测的 `top_k` 为 100。它不要求一定返回 100 项，没有结果时可以返回 `{"data": []}`。

最终返回多少项，应同时受以下因素约束：

1. `top_k` 上限；
2. 当前 `user_id` 下真正有检索证据的 Parent 数量；
3. 全局响应预算；
4. 每个候选能否在剩余预算内形成非空、有效的 evidence。

可以理解为：

```text
最终 data 条数
≤ min(top_k, 有相关证据的 Parent 数量, 预算容纳得下的候选数量)
```

这是一个上限关系，不是要求凑满的公式。内部 BM25 可以评估远多于 100 个 Retrieval Units，但最终 `data` 列表不能超过 `top_k`，也不应该为了凑数返回无关候选。

示例（假设 `top_k=100`）：

| 实际情况 | 合理的 `data` 条数 |
|---|---:|
| 只有 4 个 Parent 有相关证据，预算充足 | 4 |
| 有 80 个相关 Parent，预算都容纳得下 | 最多 80 |
| 有 150 个相关 Parent，但预算只能容纳 18 项 | 最多 18 |
| 没有任何相关证据 | 0 |
| 有很多 BM25 document，但它们只属于 6 个 Parent | 最多 6 项（Parent 去重后） |

**不要把 `top_k=100` 理解成“必须返回 100 条记忆”。**

### 1.3 全局预算是内部工程约束，不是 `top_k`

当前 `MAX_RESPONSE_CHARS = 60000` 并没有成为真正的全局上限：代码使用 `max(MIN_HIT_CHARS, MAX_RESPONSE_CHARS // len(ranked))`，短 memory 直接全文返回，而必带的命中块和尾部块也可能突破 `cap`。

V1.1 应把它改成真正的**全局内容字符预算**，例如：

```python
MAX_RESPONSE_CONTENT_CHARS = 60000
```

这里的 60,000 是一个可配置的本地工程起点，不是 AML 官方要求，也不等同于 token 数。它必须通过实际响应大小和评测结果校准，不能用未经确认的模型上下文窗口数字反推。

预算的计算口径应明确：

- 统计所有最终 `data[i].content` 的渲染后字符数总和；
- `[user]`、`[assistant]` 标签和证据片段之间的分隔符也计入；
- 不允许某个候选的“必带块”绕过总预算；
- 不允许每条 memory 都有一个会使总预算失效的最低字符额度；
- 可另外记录序列化 JSON 的字节数，作为传输层监控指标。

建议 Search 的最终执行顺序：

1. 在对应 `user_id` 的记忆范围内计算 BM25；
2. 排除没有词面证据的候选，并按相关性排序；
3. 将 Retrieval Units 归并为 Parent 候选；
4. 为每个 Parent 生成有界的 evidence 内容；
5. 按排名依次尝试加入结果，只有在剩余全局预算内能放入有效内容时才加入；
6. 满足 `len(data) == top_k` 或没有更多可容纳的候选时停止。

预算影响的是**实际返回的数量和内容长度**；它不能改变已返回结果的相关性排序，也不能成为塞入低相关结果的理由。

### 1.4 必须守住的官方边界

依据当前 [API Guide](https://agentmemoryleaderboard.ai/api-guide)：

- Search 只能在请求的 `user_id` 对应的记忆范围内检索；
- Add 的 `messages` 是有序数组，`role` 是消息字段；应保留消息顺序和角色；
- Search 的 `data` 必须按相关性排序，条数不得超过 `top_k`；没有结果时返回空数组；
- Search 返回已有记忆证据，不得根据当前 query 现场生成答案、伪装成历史记忆，或添加历史中不存在的事实和未来信息；
- `session_id` 用于标识来源会话，可用于组织数据，但不是 Search 的过滤条件。

本路线不增加跨 Add 的 session 邻居扩展，也不在 Search 阶段调用 LLM 生成答案或摘要。默认优先使用可追溯的抽取式证据。

---

## 2. V1.1 — 结构化 Add、细粒度 BM25、真正的全局预算

### 本版本目标

先把数据表示、检索粒度和返回纪律做正确。V1.1 的目标不是立即发明更复杂的评分公式，而是保证 BM25 能命中单条 message 或其内部 chunk，并且 Search 不会因为 `top_k` 或预算设计而返回大量无关内容。

### 2.1 Add：保留原始 messages 结构

当前实现：

```python
content = "\n".join(m["content"] for m in messages)
```

会把 `role` 和消息边界丢掉。V1.1 改为保存结构化 Parent Memory，概念结构如下：

```json
{
  "id": "stable_parent_id",
  "user_id": "...",
  "session_id": "...",
  "request_id": "...",
  "messages": [
    {
      "role": "user",
      "timestamp": 1704067200000,
      "content": "原始消息文本"
    },
    {
      "role": "assistant",
      "content": "原始回复文本"
    }
  ],
  "created_at": "..."
}
```

实施要求：

- 保留每条消息的 `role`、原始 `content`、可用的 `timestamp` 和数组顺序；
- 不把一个 Add 的所有消息拼成一个无法拆分的字符串；
- 不把每条 message 另存成独立顶层 Parent；它们仍属于同一个 Add；
- Parent ID 必须稳定，且能映射回来源 Add；
- `request_id` 重试仍需幂等；
- 重新检查当前基于 content hash 的去重行为，避免不同角色结构或不同来源的有效记忆被意外吞掉；如要改变去重政策，应单独测试和记录。

### 2.2 建立 Retrieval Units

BM25 的 document 改为 Retrieval Unit：

- 普通长度的 message：一条 message 对应一个 unit；
- 超长 message：只在这条 message 内切 chunk；
- 绝不跨 message 边界切块；
- 每个 unit 保存 `parent_id`、`message_index`、`chunk_index`、`role`、文本偏移和原始文本；
- chunk 尽量按换行或段落边界切分；单条超长行无法按自然边界切分时才按字符上限切；
- chunk 大小应有上限，避免一个 unit 自身大到无法放入响应预算；
- 索引重建后 `unit_id` 必须确定且稳定。

建议的内部结构：

```json
{
  "unit_id": "parent_id:m3:c0",
  "parent_id": "stable_parent_id",
  "message_index": 3,
  "chunk_index": 0,
  "role": "assistant",
  "content": "历史原文片段"
}
```

这里的 `unit_id` 是内部检索标识。最终 API 的 `data[i].id` 建议仍标识 Parent Memory；不要因为一条 Add 切成了很多 unit，就把同一 Parent 重复返回很多次。

### 2.3 BM25：先搜 unit，再做最小 Parent 聚合

V1.1 采用简单、可解释的基线：

1. 对当前 `user_id` 的全部 Retrieval Units 建 BM25；
2. 对 query 评分；
3. 没有 query token、没有任何词面重叠或没有有效检索证据时，不返回候选；
4. 暂时用该 Parent 最高的 unit 分数作为 `parent_score`；
5. 每个 Parent 只保留一次；
6. 在该 Parent 中选择最高分的 unit，作为本版本返回的 evidence；
7. 按 `parent_score` 降序排列。

**注意：** V1.1 的 `max(unit_score)` 只是建立可测量的基线。它仍可能让消息较多的 Parent 更容易出现一个高分 unit，这个偏差留到 V1.3 专门解决。不要在 V1.1 同时混入多种 parent 聚合公式。

不要仅凭一个硬编码的 `score <= 0` 就决定所有候选都不相关；应同时检查 query 是否有可用 token、候选是否存在词面匹配，并用本地开发集校准最低相关性门槛。BM25 分数分布会随语料变化。

### 2.4 Search：content 返回证据，不返回整条长 Add

V1.1 的返回项：

- `id`：稳定的 Parent ID；
- `content`：命中的 message/chunk，加上从原始 role 得到的 `[user]` 或 `[assistant]` 标签；
- `score`：该 Parent 的基线相关性分数；
- `created_at`：如有则保留。

例如，命中 assistant 的第三条消息时，返回内容可以是：

```text
[assistant]
历史原文中的相关修改与测试内容……
```

不应为了“看起来完整”而默认把整个 Parent 全文返回。也不应加入现场生成的诊断、解决方案、摘要或原始记录中不存在的事实。

当前 `[memory=...;spans=...]` 这类内部调试 marker 不应混入最终 evidence 内容。需要审计的 span、message index 和 chunk index 留在内部日志或检索对象里即可。

### 2.5 真正落实全局预算

- 将 `MAX_RESPONSE_CHARS` 改为语义明确的 `MAX_RESPONSE_CONTENT_CHARS`；
- 对最终所有 `content` 共享一个 `remaining_budget`；
- 不设置会突破总预算的每条最低返回额度；
- 不设置不受预算约束的必带 chunk；
- 一个候选放不进剩余预算时，不得超预算硬塞；可跳过它并继续检查后续更短的候选，但最终保留项仍按相关性降序；
- 预算用完就停止，不用低分或零匹配候选填充；
- `data` 长度始终 `<= req.top_k`。

V1.1 不做复杂的 rank-aware 分配；先用“按排名顺序、每个 Parent 返回一个有界的最佳 evidence”建立正确基线。

### 2.6 V1.1 验收测试

- Add 中的 `user/assistant/user/assistant` 顺序、角色和正文可原样恢复；
- 一条超长 message 被切成多个 unit，但任何 unit 都不会混入另一条 message；
- 一个 Parent 有多个命中 unit，最终 `data` 中仍只出现一个 Parent；
- 不同 `user_id` 之间不能互相检索；
- 空 query、没有词面重叠的 query 返回 `data: []` 或仅返回真正有证据的结果；
- `top_k=1` 时最多一项；`top_k=100` 时最多 100 项；不要求凑满；
- `sum(len(item["content"]) for item in data) <= MAX_RESPONSE_CONTENT_CHARS`；
- 测试预算恰好相等、剩余预算差 1 个字符、没有预算、单个候选比剩余预算长等边界；
- Search 的 `content` 不含生成答案、未来信息或无法追溯的内容。

**V1.1 完成标准：** 结构化数据正确、检索单位正确、输出上限严格有效、契约测试全部通过。不要用“是否恰好返回 100 条”作为验收标准。

---

## 3. V1.2 — 上下文窗口重建与预算分配

### 本版本目标

V1.1 能找出最相关的 message/chunk；V1.2 解决的是：**命中的证据是否需要同一 Add 内的少量上下文才能被理解。**

### 3.1 从 anchor evidence 扩展局部上下文

对每个 Parent，先找到最相关的 anchor unit，再考虑：

- 同一条超长 message 中紧邻的 chunk；
- 同一 Parent 内前后相邻的 message；
- 这些邻居是否有助于理解 anchor，而不是机械地全部加入。

默认可以从命中 message 前后各一条开始做实验，参数化配置，而不是写死为不可调整的规则。必须处理第一条/最后一条消息的边界。

上下文只在**同一个 Parent / Add 内**扩展。V1.2 不通过 `session_id` 跳到其他 Add，也不把同一个 session 的所有记忆都塞进结果。

### 3.2 合并重叠窗口

如果同一个 Parent 的多个 unit 都命中：

```text
命中 M3 → M2 + M3 + M4
命中 M4 → M3 + M4 + M5
```

应合并为不重复的连续窗口，而不是把 M3、M4 重复返回两遍。输出消息按原始 source order 排列，并保留 role 标签。

### 3.3 删除无条件尾部注入

移除 `TAIL_CHUNKS = 2` 的无条件保底逻辑。

- 尾部如果确实是高相关证据，可以由 BM25 检索命中并被选中；
- 尾部如果只是清理日志、无关命令或环境输出，不应因为它“在最后”就获得特权；
- 不能假设每条历史的解决方案都位于最后几个 chunk；
- 不为“完整性”牺牲更相关的证据。

### 3.4 预算分成 anchor 和 optional context

V1.2 在 V1.1 的硬全局预算之上，改进预算使用顺序：

1. 对排名靠前的 Parent，优先尝试放入最相关的 anchor evidence；
2. 只有 anchor 成功放入后，才使用剩余预算添加相邻上下文；
3. 上下文过长时先减少可选邻居，不要为了带齐邻居而突破总预算；
4. 可选的第二个命中片段排在邻居之后还是之前，应通过开发集消融测试决定；
5. 预算耗尽后不继续追加低相关结果。

这才是 rank-aware budget 的第一版：**排名决定优先级，预算决定完整程度；两者都不能让总输出超限。**

### 3.5 V1.2 验收测试

- 命中第一条或最后一条 message 时，不会越界；
- 相邻窗口按原顺序输出，role 不丢；
- 重叠窗口合并后没有重复片段；
- 无关 tail 不再被强制返回；
- anchor 不能放入预算时不会返回空壳 Parent；
- 任何上下文扩展都不突破全局预算；
- 在同一数据集上比较 V1.1 与 V1.2 的 Recall/MRR、返回字符数和空结果率。

**V1.2 完成标准：** 上下文能帮助理解命中证据，但不引入无关尾部、重复窗口或预算超限。

---

## 4. V1.3 — Parent 排名聚合与长轨迹偏差控制

### 本版本目标

V1.1 的 `parent_score = max(unit_scores)` 是简单基线，但存在潜在偏差：一个包含很多 messages/chunks 的 Parent 有更多机会出现一个偶然高分 unit。

V1.3 专门解决这个问题，不改 Add 格式，也不引入 Dense。

### 4.1 比较 Parent 聚合策略

在相同的 unit 分数上，离线比较以下方案：

- **Max baseline**：最高 unit 分数；
- **Top-2/Top-3 衰减聚合**：最佳 unit 为主，其他独立的高分 unit 提供有限补充；
- **带长度/Unit 数量控制的聚合**：避免 Parent 只因为切出了更多 unit 就占优；
- 必要时使用 query 内的分数归一化，再比较聚合策略。

不要直接把同一 Parent 所有 unit 的分数相加。unit 越多，总和天然越大；也不要未经实验就指定一个固定的长度惩罚系数。

### 4.2 区分“找哪条历史”和“返回哪段证据”

V1.3 的流程：

```text
Query
  ↓
BM25 对 Retrieval Units 评分
  ↓
按 parent_id 聚合 unit 分数
  ↓
Parent Ranking（每个 Parent 只排一次）
  ↓
在每个 Parent 内选择最佳 evidence window
  ↓
全局预算
  ↓
Search data
```

- Parent 排名决定哪个来源优先；
- evidence selection 决定该来源中返回哪些原文；
- 不能用“返回内容更长”或“尾部必须出现”改变 Parent 的原始相关性顺序；
- 最终 `data` 中同一 Parent 只能出现一次；
- `top_k` 统计最终 `data` 的项数，而不是内部 BM25 unit 数量。

### 4.3 V1.3 验收测试

构造不同长度的 Parent：例如一个有 30 个 unit，另一个只有 3 个 unit，但后者包含更直接的匹配。验证长 Parent 不会仅因 unit 多而稳定占据前列。

比较：

- Recall@K；
- MRR 或 nDCG（若有相关性标签）；
- 同一 Parent 的重复返回数（必须为 0）；
- Parent 长度与排名之间的异常相关性；
- 全局预算、返回条数和 Search 延迟。

**V1.3 完成标准：** 选出的 Parent 更可靠，长轨迹不因 unit 数量获得不合理优势；如果新聚合方案没有稳定收益，就保留 Max baseline。

---

## 5. V1.4 — Coding-aware BM25

### 本版本目标

在检索结构和预算稳定后，再提升 BM25 对代码词汇的处理。仍然只使用 BM25，不使用 embedding 或语义向量。

当前 tokenizer 已有一定的 `snake_case` / `CamelCase` 拆分能力；V1.4 是在这个基础上补强，而不是从零开始。

### 5.1 保留完整代码标识符和子词

示例：

| 原始文本 | 检索时同时保留的信号（示意） |
|---|---|
| `refresh_token` | 完整标识符、`refresh`、`token` |
| `TokenExpiredError` | 完整标识符、`token`、`expired`、`error` |
| `src/auth/token.py` | 完整路径、文件名、目录名、扩展名及有用子词 |
| `validateToken()` | 完整函数标识符、`validate`、`token` |
| `HTTP 500` | `HTTP`、`500` |
| `test_auth_refresh` | 完整测试标识符、`test`、`auth`、`refresh` |

当前 tokenizer 对路径中的 `/` 和 `.` 不会完整保留为路径特征，V1.4 应增加路径感知提取，并避免只剩下过于宽泛的单词。

### 5.2 可实验的 BM25 特征

- 完整路径与 basename；
- 函数名、类名、模块名；
- 异常类型、错误码、测试名；
- 命令行 flag、包名和配置键；
- 原始标识符与拆分子词；
- `role` 作为保留字段，是否按 role 加权必须通过实验决定。

不要预设 assistant 消息总是比 user 消息重要。用户消息可能包含精确错误、文件路径和复现条件；assistant 消息可能包含修复和测试结果。角色权重应通过数据验证。

### 5.3 只做确定性的词法扩展

允许从 query 中提取路径、标识符和子词；不调用 LLM 生成 query 答案，不把推测出来的解决方案加进索引或返回内容。

### 5.4 V1.4 验收测试

- 精确文件路径能检索到对应历史；
- `snake_case`、`CamelCase`、异常名、测试名和数字错误码均有测试；
- 普通自然语言查询没有明显退化；
- 代码 tokenizer 的变化有独立开关，可与 V1.3 做消融比较；
- 仍然遵守 `user_id` 隔离、`top_k` 上限和全局预算。

**V1.4 完成标准：** Coding 特征查询的 lexical retrieval 有稳定收益，且没有以大幅增加噪声为代价。

---

## 6. V1.5 — BM25 参数校准、消融和发布门禁

### 本版本目标

不再增加新的检索范式，而是用可复现的实验确定哪些 BM25 改动真正有效，冻结一个稳定版本。

### 6.1 固定对比实验

至少保留以下对照：

1. V1.0 当前实现；
2. V1.1（结构化 messages + unit BM25 + 硬预算）；
3. V1.2（上下文窗口）；
4. V1.3（Parent 聚合策略）；
5. V1.4（Coding-aware tokenizer）。

每次只切换一个功能开关；使用同一份获准使用的开发数据、相同 query 和相同评测流程。不要只根据某一次 smoke 的单题结果决定整体算法，也不要针对单个隐藏题写特判。

### 6.2 需要记录的指标

**检索质量**

- Recall@K；
- MRR / nDCG（有标签时）；
- 相关证据是否进入最终返回内容；
- 空结果率；
- 低相关或无词面证据的返回率。

**预算和返回质量**

- `len(data)`；
- `sum(len(item["content"]) for item in data)`；
- 序列化 JSON 的字节数；
- 被预算跳过的候选数；
- 每个 Parent 返回的证据片段数；
- 返回内容中重复片段比例。

**运行质量**

- Search 延迟（冷索引和热索引分开统计）；
- 索引构建时间；
- Add 后索引失效和重建是否正确；
- 不同 `user_id` 之间是否存在任何串读。

### 6.3 发布门禁

只有以下条件全部满足才发布：

- 官方 API contract 测试通过；
- `data` 始终按相关性降序，且长度不超过 `top_k`；
- 全局字符预算与可选 JSON 字节预算没有越界；
- 没有跨 `user_id` 读取；
- 返回内容可追溯到原始 Add 消息；
- role 和消息顺序没有丢失；
- 对比基线的检索指标和返回规模都有报告；
- 固定代码版本、配置和开发集，能复现同一结果。

**V1.5 完成标准：** 形成一个可重复、可解释、BM25-only 的稳定版本，而不是仅在单次 smoke 上看起来更好。

---

## 7. 所有版本都不做的事情

- 不引入 Dense、Embedding、向量数据库或 RRF；
- 不根据当前 query 现场生成最终答案；
- 不把生成的解决方案伪装成历史 memory；
- 不添加历史记忆中不存在的事实或未来信息；
- 不按 `session_id` 自动扩展到其他 Add；
- 不为了填满 `top_k` 返回零匹配或无关候选；
- 不允许必带 chunk、最低每条字符数或尾部保底突破全局预算；
- 不把一个 Add 的所有 messages 永久压平后再试图从文本中猜回 role。

---

## 8. 建议的实际开发顺序

```text
V1.0 当前基线
  ↓
V1.1  先改数据结构、检索单位、过滤和硬预算
  ↓
V1.2  再改同一 Parent 内的上下文重建和预算分配
  ↓
V1.3  再校准 Parent 聚合，控制长轨迹偏差
  ↓
V1.4  再增强 Coding-aware BM25
  ↓
V1.5  做消融、回归和发布门禁
```

### 旧数据处理

当前 `memory.jsonl` 中的 `content` 已经把多条 message 拼在一起，原始 `role` 和消息边界无法可靠恢复。V1.1 不应根据文本猜测谁说了什么。

建议：

1. 保留旧文件作为 V1.0 基线和回滚依据；
2. 为 V1.1 使用新的存储文件或独立数据目录；
3. 让平台重新执行 Add，写入结构化 `messages`；
4. 在新数据上验证后，再切换服务配置；
5. 记录版本号、配置和数据来源，确保对比公平。

---

## 最终决策摘要

**V1.1 是本次最重要的改造：**

- 一次 Add 保存为一个 Parent Memory；
- 保留有序 `messages[]`、`role`、`timestamp`；
- 每条 message 是独立检索单位；只有超长 message 才继续切 chunk；
- BM25 搜索 Retrieval Units，再聚合到 Parent；
- 每个 Parent 最多产生一个最终 `data` 项，`content` 只返回被选中的历史证据；
- `top_k` 是最终 `data` 长度的上限，不是必须达到的数量；
- 所有返回内容共用一个真正的全局预算；
- 无相关证据时允许返回 `data: []`；
- 不做 Dense，不生成答案，不引入历史之外的信息。

后续每个版本只在这个稳定基础上增加一个可单独验证的改进。
