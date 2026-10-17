# 10 天版本化执行计划：Raw-First Coding Memory

## 0. 先说清楚：当前版本到底是什么

当前 master 是 **V0：原始文本存储 + 单路 BM25**，不是此前报告里出现的“三段卡片版”。`card.py` 不存在于当前代码，旧 `eval_v2_card.json`、`eval_v3_card.json`、`eval_v4_card30k.json` 只能证明旧实验做过什么，不能代表现在版本。

### 当前真实行为

| 项目 | 当前情况 | 影响 |
|---|---|---|
| 存储 | `messages[].content` 用换行拼成一条 `content`，原样写入 `data_store/memory.jsonl` | 符合 Raw-First，原文没有被摘要替换 |
| 检索 | 每个 `user_id` 一个 `BM25Okapi` 索引，首次查询时懒加载 | 能工作，但只有一条检索通道 |
| 排序 | 按 BM25 原始分数从高到低排，然后无条件返回 `top_k` | 没有相关性阈值，低分噪声也会占满 Top-K |
| 响应 | `/search` 返回完整原始 `content` | 大 patch/长轨迹会严重膨胀；当前默认 `top_k=100` 风险尤其大 |
| 时间戳 | `/add` 接收 `timestamp`，但写入时丢失，只记录服务接收时间 | 无法按消息原始时间做排序或会话平滑（**待办**） |
| 幂等 | 重复 `request_id` 会跳过写入，但 HTTP 仍返回 `success=true` | 客户端无法区分“新写入”和“重复请求”（**待办**） |
| 索引更新 | 每次新增一条都令整个用户索引失效，下一次查询重建 | add/search 交错时延迟风险高 |
| 评测映射 | `/search` 返回项的 `id` = `sha256(content)` 前 12 位，`evaluate.py` 按该 id 反查经验 ID | ✅ 已改为显式 ID 映射；lite/noisy 990 次命中 `unmapped=0`，99 个任务的排序与旧的 content 全等口径逐项一致（指标未变） |

### 当前数据集情况

SWEContextBench 的形态也已核实：

- Lite：99 个待解任务，300 条经验；`relevant` 空间 71 条，`noisy` 空间 300 条。
- Full：357 个唯一待解任务，1007 条经验；`relevant` 空间 229 条。
- 每条经验不是对话轨迹，而是 `problem_statement + gold patch`。
- 每条经验独立入库，不是连续 session；不能直接把相邻经验当成“相邻历史消息”。
- 数据中没有可靠的 New Feature / Bug Fix 标签；不能把“出现 bug/add 字样”包装成任务感知能力。

### 当前本地基线

`reports/eval_swe_lite_local_20261011_103757.json` 是旧运行记录，可以作为参考，但还不能直接视为当前 HEAD 的正式基线。该报告实际上同时包含 relevant 与 noisy 两组指标：

| 条件 | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR |
|---|---:|---:|---:|---:|---:|
| relevant | 0.7424 | 0.8384 | 0.8788 | 0.9394 | 0.8066 |
| noisy | 0.5354 | 0.7071 | 0.7980 | 0.8485 | 0.6449 |

这说明当前系统已经有较强 Recall@10，真正短板更像：

1. noisy 下 Top-1 排序不够准，MRR 只有 0.6449。
2. 低分噪声会填满 Top-K。
3. 长文本响应没有预算控制。
4. 文件路径、函数、异常和代码修改位置还没有被当作强信号。
5. 报告没有记录延迟和响应字节数。

### 当前阻塞问题

- `data_store/memory.jsonl` 里目前有 6 条旧的公网 smoke 数据，没有当前 SWEContextBench 的 `swe:*` 数据。现在直接跑评测会静默得到全 0，不能当成模型效果。
- `jieba` 是 `memory_engine.py` 的硬依赖，但没有写进 `requirements.txt`；仅靠 README 里的本地安装命令无法保证服务启动。
- BM25 常见词存在负 IDF 被库函数抬成正权重的现象，导致大量文档会仅因 `the/for/not` 等词被召回。
- `rank_bm25` 的重建代价随记忆规模增长，而每次 `/add` 都令索引失效；在 add/search 交错场景下有风险。

## 1. 榜单经验应如何落到本项目

开源榜经验给出的不是“必须上向量数据库”，而是三条可执行原则：

1. **Raw-First**：不要先让 LLM 把历史改写成摘要。原始信息是可恢复证据，摘要最多是附加视图。
2. **检索信号比抽象层更重要**：BM25、Dense、代码实体、路径、函数、异常都要参与排序。
3. **不同任务可能吃不同信号**：Bug Fix 更吃报错、文件和修复链；New Feature 更吃实现方式和模块关系。但当前 SWEContextBench 无法直接证明这一差异，因此必须先做诊断实验，不能假装已经实现。

后续版本不以“加了多少组件”为成功标准，而以“同一评测口径下是否真实减少排名失败”为标准。

## 2. 实验纪律：每个版本必须可对照

所有实验必须满足以下条件，否则结果不可信：

- 每次运行使用新的 `run_id`，例如 `v0_bm25`、`v1_bm25_plus`、`v2_entity`。
- 每次运行使用独立存储路径，通过 `MEMORY_STORE_PATH` 控制。
- `/add` 后必须验证服务里的实际数量，而不是只看 HTTP 200。
- 评测报告必须记录：
  - Git commit、镜像 tag、服务版本
  - `split`、`top_k`、`INCLUDE_PATCH`
  - relevant/noisy 的 Recall@1/3/5/10、MRR、nDCG@10
  - `n_returned`
  - `/search` 响应字节数或近似 token 数
  - Add、冷启动 Search、稳定 Search 的 P50/P95
- Lite 用于快速迭代；Full 只做版本冻结前验证，不能反复在 Full 上调参。
- 旧报告不能和当前报告直接比，除非 `run_id`、存储、数据和参数完全一致。

### 版本通过门槛

每个新版本相对 V0 必须满足：

1. 所有返回结果都能映射回经验 ID，`unmapped` 必须为 0。
2. relevant Recall@10 不低于 V0 超过 0.02。
3. noisy Recall@10 不低于 V0 超过 0.02。
4. 至少满足一项：
   - noisy MRR 提升 >= 0.05；
   - 或 Recall@1 提升 >= 0.05；
   - 或至少修复 5 个失败任务，且新增失败任务 <= 2，并通过人工检查确认不是过拟合。
5. Search P95 不退化；若引入 Dense/FAISS，稳定查询必须能解释冷启动和内存成本。

未达到门槛的版本只保留为实验分支，不进入 `/search` 默认链路。

## 3. 版本路线：每一版做成什么样子

### V0：可复现 Baseline，而不是“能跑就行”

目标：把当前 Raw + BM25 版本变成可信、可重复、可比较的基线。V0 不负责提分，只负责让后续提分可信。

要做什么：

1. 修复依赖和启动一致性。
   - `requirements.txt` 增加 `jieba`。
   - 补齐数据集下载实际需要的 `huggingface-hub`，不要只留下部分 freeze 包。
   - 镜像和本地都使用同一 `requirements.txt` 安装，避免本地一套、容器一套。
2. 支持实验隔离。
   - `memory_engine.py` 从环境变量 `MEMORY_STORE_PATH` 读取存储路径。
   - 默认仍使用部署路径；实验时指向 `data_store/experiments/<run_id>/memory.jsonl`。
3. 给评测加运行元数据和 store 校验。
   - `evaluate.py` 写入 commit、时间、配置、run_id、每个 condition 的 pool/n_returned 分布。
   - 增加 `verify_store.py`，或在 `evaluate.py` 中检查 `swe:relevant:<run_id>`、`swe:noisy:<run_id>` 中实际记忆数量。
4. 记录性能与响应大小。
   - 每次 `/search` 记录 HTTP 请求耗时和响应原始 JSON 字节数。
   - `/add` 批量导入记录总耗时和平均每条耗时。
5. 更新旧报告解释。
   - `eval_v2_card`、`eval_v3_card` 标注为短响应旧实验，不能用于当前版本回归。
   - `eval_v4_card30k` 内部 run_id 是 `v3`，标注为来源不可靠，不作为后续基线。

V0 做成这样才算完成：

```bash
export MEMORY_STORE_PATH=data_store/experiments/v0_bm25/memory.jsonl
envs/memory/python -m uvicorn app:app --app-dir src --host 127.0.0.1 --port 7878
```

另开终端：

```bash
envs/memory/python src/run_add_client.py --split lite --run-id v0_bm25
envs/memory/python src/verify_store.py --split lite --run-id v0_bm25 --expected-relevant 71 --expected-noisy 300
envs/memory/python src/evaluate.py --split lite --run-id v0_bm25 --top-k 10 --out reports/v0_bm25_lite.json
```

完成后报告必须显示：

- relevant pool 实际为 71，noisy pool 实际为 300。
- 99 个任务全部执行，`n_returned` 平均接近 10。
- `unmapped` 为 0。
- Recall/MRR 与旧参考大致接近；若差距很大，先查数据导入、索引和配置，不查模型。
- 有 Search P50/P95、响应字节数、冷启动耗时。

### V1：让 BM25 排序可信

目标：解决“低分噪声也占满 Top-K”和常见词负 IDF 问题。

做什么：

1. 把 `BM25Okapi` 变成可选变体：
   - `BM25_VARIANT=okapi`：保留原行为，作为对照。
   - `BM25_VARIANT=l`：实验 BM25L。
   - `BM25_VARIANT=plus`：实验 BM25Plus。
2. 同一份数据分别跑三版，不看主观感觉，只看 paired task 差异。
3. 加入结果置信度实验：
   - 返回 top_k 候选。
   - 对 BM25 原始分数做查询内归一化。
   - 实验 `min_score_ratio=0.0/0.05/0.10/0.20` 是否砍掉噪声。
   - 不能为了减少噪声牺牲 Recall@10。

V1 做成这样才算完成：

- 选出一种 BM25 变体，在 noisy MRR 或 Recall@1 上有明确提升。
- `min_score_ratio=0` 时 Recall@10 与 V0 基本一致。
- 若加入低分过滤，必须报告：
  - 平均返回条数；
  - Recall@10；
  - MRR；
  - 被过滤掉但实际是 gold 的任务数。
- 只有少返回噪声但没有少召回 gold 的方案才能默认启用。

### V2：抽取代码实体，仍然不摘要

目标：把函数、文件、异常、patch 修改位置变成检索信号，而不是让 LLM 重写历史。

抽取对象：

| 类型 | 示例 | 用途 |
|---|---|---|
| 文件路径 | `django/db/models/fields.py`、`src/handlers/cache.py` | 匹配之前改过哪里 |
| 函数/类 | `refresh_cache`、`getUserProfile`、`QuerySet` | 匹配代码符号 |
| 异常 | `ValueError`、`TimeoutError`、`NullPointerException` | Bug Fix 场景强信号 |
| Patch 改动 | `diff --git`、`+++ b/...`、hunk header 中的函数名 | 判断经验是否真的改到相关位置 |
| 仓库 | `django/django` | 当前 SWEContextBench 可用，未来平台需确认能否可靠获得 |

做什么：

1. `memory_engine.py` 在保留原始 `content` 的前提下，新增 `metadata` 字段：
   ```json
   {
     "id": "mem_xxx",
     "content": "原始问题与 patch",
     "metadata": {
       "files": ["django/db/models/fields.py"],
       "functions": ["__eq__", "refresh_cache"],
       "errors": ["ValueError"],
       "repository": "django/django"
     }
   }
   ```
2. 旧数据没有 `metadata` 时必须能继续加载，不能因为 schema 升级而启动失败。
3. 第一步只做字段重复/加权进入 BM25 token 的低风险实验：
   - files token 权重低一些；
   - functions 和 errors 权重高一些。
4. 如果低风险实验有效，再考虑自定义字段加权 scorer。
5. 不把 LLM 摘要加入默认链路。摘要只能作为后续 A/B 分支。

V2 做成这样才算完成：

- 对 noisy 失败任务逐条看 Top-10，至少一批原本靠长词重合的任务能因为路径/函数/异常命中而上升。
- Recall@10 不回退。
- noisy MRR 或 Recall@1 通过门槛。
- 同仓库非 gold 的 hard negative 不应明显增多；如果增多，要区分是数据噪声还是实体权重误导。

### V3：Dense 单通道实验，先证明它有没有用

目标：不是急着做 Hybrid，而是先回答 Dense 是否比当前 BM25 更懂自然语言问题。

做什么：

1. 选择本地小模型，不接外部 API。优先考虑 CPU 可跑、英文代码/问题描述表现尚可的模型；HF 缓存必须放在项目目录，不写 C 盘默认路径：
   ```bash
   export HF_HOME=$PWD/data/models/huggingface
   ```
2. 新建实验模块：
   - `embedding_client.py`：模型加载、编码、缓存。
   - `vector_store.py`：FAISS 索引、按 `user_id` 隔离。
3. 文本策略：
   - 查询只使用 `problem_statement`。
   - 经验分别测试：
     - `statement` 单独 embed；
     - `patch` 单独 embed；
     - `statement + patch` 一起 embed；
     - statement 和 patch 分成两个 doc，检索后按经验 ID 合并。
4. Dense 结果必须能映射回原经验 ID。不能因为切片导致召回内容但不知道属于哪条经验。

V3 做成这样才算完成：

- 至少有一版 Dense 单独跑完整 lite。
- 明确给出：
  - Dense-only 指标；
  - 与 V0 的共同失败/共同成功；
  - 冷启动加载模型耗时；
  - 索引大小；
  - 内存占用。
- 如果 Dense-only 在 noisy Recall@10 或 MRR 上没价值，后续 Hybrid 优先级降低，不能因为它听起来先进就进入主链路。

### V4：BM25 + Dense 的 RRF 融合

目标：在两个通道都单独可测之后，才做融合。

做什么：

1. 候选池：
   - BM25 top 50/100；
   - Dense top 50/100。
2. 使用 rank-based RRF：
   ```text
   score(doc) = 1 / (60 + rank_bm25(doc)) + 1 / (60 + rank_dense(doc))
   ```
3. 去重按经验 ID，不按返回文本。
4. 融合后再经过 V2 的代码实体信号或 V5 的 code-aware rerank。

V4 做成这样才算完成：

- 三组对照必须同时报告：
  - 最佳 BM25 版本；
  - 最佳 Dense 版本；
  - Hybrid 版本。
- Hybrid 只有在 noisy Recall@10/MRR 优于两者，且没有牺牲 relevant 上限时才值得默认启用。
- 如果 Hybrid 只是把两个错误信号混在一起，应回退到最佳单通道。

### V5：Code-aware Rerank，而不是泛泛重排

目标：针对 coding memory 的真实信号重排候选。

候选特征：

- query 与 memory 的函数名重合。
- query 与 patch 文件路径重合。
- query 与异常名重合。
- memory 是否包含可执行的修复 patch。
- memory 的 statement 与 query 主题是否接近。
- Dense 与 BM25 rank 是否一致。

做什么：

1. 先做规则重排，不做训练模型：
   ```text
   final_score =
     hybrid_score
     + file_overlap_weight
     + function_overlap_weight
     + error_overlap_weight
   ```
2. 每个权重要单独消融：
   - file only；
   - function only；
   - error only；
   - all。
3. 人工检查 Top-5，尤其看新增进 Top-5 的内容是否真的对解题有帮助。

V5 做成这样才算完成：

- 不是只看指标，而是要有失败任务前后对比。
- 对 5-10 个典型任务列出：
  - V0 排名；
  - 最佳 BM25 排名；
  - V2 排名；
  - Hybrid/Rerank 排名；
  - gold patch 中是否包含关键路径/函数/修复。
- 若某权重只对少数样本有效且造成总体回退，不保留。

### V6：响应视图，解决“召回了但模型用不上”

目标：保持原始记录不丢失，同时控制 `/search` 返回给平台模型的上下文长度。

原则：

- JSONL 永远保存完整原文。
- `/search` 可以返回视图，但视图必须可追溯到原 memory。
- 不先做摘要；先用确定性抽取。
- 不把不同经验的内容合并成一条冒充一条记忆。

视图策略：

1. 默认 raw 视图，用于 V0-V5 公平比较。
2. 只有平均响应超过预算时启用 compact 视图：
   - 问题首段或前 N 行；
   - patch 中与 query 词重合的文件/函数附近片段；
   - `Traceback` / exception 附近片段；
   - 保留 source marker，例如：
   ```text
   [memory_id=mem_xxx;chars=0-3500/206488]
   ```
3. `evaluate.py` 的映射已改为基于返回 `id`（`sha256(content)` 前 12 位），compact 视图不会让指标失真；
   但**切块之后一条经验会对应多个 id**，映射表要升级成 `(memory_id, chunk_index) -> 经验 ID`，否则分块视图仍会 unmapped。

V6 做成这样才算完成：

- raw 与 compact 都有完整 lite 对照。
- compact 不破坏 Recall/MRR：unmapped 仍为 0。
- 响应大小显著下降，例如 P95 总响应从数百 KB 降到可控预算。
- 人工检查确认 compact 里仍能看到问题、相关文件、关键函数、修复方向。
- 没有 LLM 参与默认链路。

### V7：性能与稳定性，把实验版变成可提交版

目标：确保平台 Full 阶段不会因为索引重建、锁竞争、响应过大而失败。

做什么：

1. 索引增量更新或至少缓存重建：
   - 避免每加一条就完全丢失 BM25 索引。
   - 可以按批次 dirty 后重建，但必须证明延迟可接受。
2. 搜索缓存按 `user_id + query_hash + top_k` 设置短 TTL，仅用于本地压力实验，不能跨 `user_id`。
3. `/search` 不要在写锁内做全量重排；拆分读锁，或维护无锁只读快照。
4. 为 `/add` 增加容量检查：
   - 单请求最大 messages；
   - 单条 content 最大字符数；
   - 超限时返回明确错误，不能静默截断。
5. 部署检查：
   - compose、Dockerfile、git tag、镜像 tar 版本统一。
   - 镜像必须包含 `src/run_add_client.py`、`src/dataset_adapter.py`、`src/download_dataset.py`，或至少提供可复现导入入口。
   - Dense/FAISS 若进入主链路，模型和索引体积必须离线打包或挂载持久化目录。

V7 做成这样才算完成：

- Full 或接近 Full 压力下 Search P95 明确满足申报 SLA。
- add/search 交错不再触发每次全量重建。
- 健康检查、幂等、跨用户隔离、重启持久化全部通过。
- 报告里有容量声明依据，而不是照抄 README。

## 4. 10 天执行安排

| Day | 版本 | 当天必须产出 |
|---|---|---|
| 1 | V0 环境 | `requirements` 修复、`MEMORY_STORE_PATH`、实验目录、启动 smoke |
| 2 | V0 基线 | 当前 master 的 lite 基线报告；store 数量校验；延迟和响应大小记录 |
| 3 | V1 BM25 | okapi/l/plus 三组对照，选出排序变体 |
| 4 | V2 抽取 | 文件、函数、异常、patch metadata 抽取；旧数据兼容 |
| 5 | V2 排序 | 字段加权进 BM25；失败任务 paired 分析 |
| 6 | V3 Dense | Dense-only lite 实验；模型冷启动和索引成本 |
| 7 | V4 Hybrid | BM25 top-N + Dense top-N + RRF；三通道对照 |
| 8 | V5 Rerank | code-aware 规则重排与权重消融；人工检查 Top-5 |
| 9 | V6/V7 | compact 响应视图；ID 映射稳定；性能和 Full smoke |
| 10 | 冻结 | 选定最佳版本，重跑 lite + full 验证，更新 README 和提交材料 |

如果某天没有达到门槛，不要硬推进下一阶段。先做 paired 失败分析、缩小改动范围、回退该候选。

## 5. 最终应提交哪一个版本

最终提交不是组件最多的版本，而是按以下顺序选择第一个达标版本：

1. 如果 V0 + V1 已经稳定且满足 SLA：提交 Raw BM25Plus/L 基线。
2. 如果 V2 通过：提交 Raw + BM25 + code entities。
3. 如果 V3 不通过但 V2 通过：不要上 Dense。
4. 如果 V4 Hybrid 通过：提交 Hybrid + RRF。
5. 如果 V5 Rerank 通过：提交 Hybrid + code-aware rerank。
6. 如果 V6 compact 通过且解决响应问题：启用 compact；否则保持 raw。
7. 如果 V7 不通过：不能申请 Full，即使指标好看。

### 最可能的合理路线

以当前证据看，第一优先不是大改架构，而是：

```text
V0 可复现
 -> V1 修 BM25 噪声/负 IDF
 -> V2 加代码实体
 -> V3 Dense 单测
 -> V4 只有证明 Hybrid 有效才融合
 -> V6 才处理 compact 视图
```

这条路线符合 Raw-First + 检索增强的开源榜经验，也避免在当前数据上引入无法验证的 LLM 摘要和任务分类。
