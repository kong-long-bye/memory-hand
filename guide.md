# Memory Hand — 部署信息与原始工作披露

> 本文档对应比赛提交清单中的两项：「部署信息完整」与「已披露原始工作」。
> 部署操作步骤见 [README.md](./README.md) §4；本文只写提交方需要申报的事实。

> **关键声明：本系统未使用大语言模型（LLM）。**
> `/add`、`/search`、`/health` 全链路不调用任何 LLM、Embedding 模型或外部模型 API；
> 检索与内容组装全部在本地完成（BM25 + 规则引擎）。
> `.env` 中保留的 `LLM_MODE` / `OPENAI_API_KEY` / `OPENAI_BASE_URL`（其中模型名为
> deepseek）属于历史遗留配置，**服务代码不读取**，不影响任何接口的返回值；
> 仓库中的 `src/llm_clinet.py` 也**未被任何服务或评测脚本导入**，不参与运行。

---

## 一、部署信息

### 1.1 已部署版本

| 项目 | 值 |
| --- | --- |
| 系统名称 | Memory Hand |
| 参赛赛道 | 代码记忆榜（Coding Memory） |
| 参赛组别 | 学术方法榜（开源可复现） |
| 服务实现版本 | **3.0**（V3.0-lite 实现） |
| 镜像 tag | **`memory-hand:v3_0`** |
| 代码 Commit | **`61e61df`**（master，含 `feat/v3.0-lite` 全部提交；该 commit 之后仅有文档改动） |
| API 契约 | 官方 `/add`、`/search`、`/health`，字段与路径未做任何改动 |
| 检索渲染模式 | `RETRIEVAL_MODE=v3_0`（启动日志可核；`v2_1` 为原始基线，可环境变量一键回滚） |

> 版本一致性要求：构建镜像的工作区必须干净（`git status` 为空），且 commit 与本表一致。
> 当前发布基线为 `61e61df`；若其后仍有代码提交，构建后请同步更新本行。

### 1.2 接口地址

公网 Base URL：**`http://183.66.27.21:17187`**

| 方法 | 路径 | 完整地址 | 用途 |
| --- | --- | --- | --- |
| GET | `/health` | `http://183.66.27.21:17187/health` | 平台探活，返回 `{"status":"ok"}` |
| POST | `/add` | `http://183.66.27.21:17187/add` | 写入一段会话记忆 |
| POST | `/search` | `http://183.66.27.21:17187/search` | 按 query 返回 Top-K 记忆/证据 |

说明：`17187` 是云主机公网端口映射，容器内服务监听 `7878`；两个端口同属本服务。
协议为 **HTTP**（非 HTTPS）。

### 1.3 鉴权

**无鉴权（Auth: None）**。三个接口均可直接访问，不校验 API Key / Token。
请求体与响应字段严格遵循官方契约，不做额外包装。

### 1.4 容量声明

| 项目 | 申报值 |
| --- | --- |
| 并发 | 16 RPS sustained（单 worker 单进程；检索/写入共享一把全局锁，实际串行） |
| 超时 | Search P95 ≤ 3s；Add P95 ≤ 2s；平台侧建议请求超时 30s |
| Rate Limit | 无 |
| `top_k` | 默认 100，允许范围 1–1000；实际返回条数还受字符预算约束 |
| 单 Parent content | 上限 16,000 字符；V3 按状态另有 8,000 / 8,000 / 4,000 / 4,000 / 1,000 预算 |
| 单次 `/search` 总 content | 硬上限 60,000 字符（按排名顺序花费，放不下的候选跳过） |
| `/add` 请求体 | 建议 ≤ 32 MB |
| 数据持久化 | JSONL 挂载卷 `/opt/memory-hand/data_store`，容器重启不丢 |
| 存储文件 | `data_store/v3.0/memory.jsonl`（结构化 messages）；V1.1 旧目录 `v1.1/` 不读取、不自动迁移 |
| 可用性 | 提交后至 **2026-11-04** 7×24 在线，不降级、不改契约、不改鉴权 |
| 日志 | `data_store/service.log` + 容器 stdout；Docker 日志轮转 20 MB × 5 |

性能参考：本地回放稳态检索 2–6 ms/次；某 `user_id` 首次检索需要现场构建该用户的
BM25 索引（本地约 0.4–0.9 s），随后复用内存索引。

### 1.5 运行限制

1. **单实例、单 worker**：索引与记忆常驻进程内存，不支持多副本水平扩展；多实例会各持一份索引且 JSONL 追加无跨实例协调。
2. **内存随记忆量线性增长**：启动时加载 JSONL 全量，数据量增大需相应提高容器内存。
3. **检索串行**：`/add`、`/search` 共用全局锁，单次请求毫秒级，高并发时排队而非并行。
4. **模式切换需重启**：`RETRIEVAL_MODE` 在进程启动时读取一次；改 `.env` 后需要 `docker compose up -d --force-recreate` 或重跑 `deploy.sh` 才生效。
5. **数据目录随版本固定**：本版本读写 `data_store/v3.0/memory.jsonl`；V1.1 的 `data_store/v1.1/` 不读取、不自动迁移，升级或回滚若需保留历史数据须手动复制该 JSONL。
6. **全链路无外部模型调用**：不调用 LLM、不调用 Embedding 服务、不访问外网；`.env` 中的 deepseek 等模型配置不被服务读取，不存在第三方额度、限流或超时导致检索失败的风险。
7. **负分过滤**：BM25 得分 `< 0` 的命中不返回；无任何词面证据时返回空列表（不是错误）。
8. **时区**：服务与日志时间均为 UTC。

---

## 二、已披露原始工作

### 2.1 数据集与轨迹来源

- 参赛与评测数据来自比赛平台提供的官方请求/轨迹（代码记忆榜对应
  `coding_memory_benchmark_rich` 类任务，如 astropy 仓库任务），以及平台
  实际发来的 `/add`、`/search` 请求。
- 这些数据**不随本仓库分发**：`data_store/` 已加入 `.gitignore`；仓库中的
  `evaluate/` 只包含回放脚本与评测产物，不含原始轨迹正文。
- 本项目未把任何本地构造的“标准答案补丁”或隐藏测试文本写入可提交的记忆库。

### 2.2 复用的论文与算法

| 来源 | 用途 | 使用方式 |
| --- | --- | --- |
| Robertson & Zaragoza, *The Probabilistic Relevance Framework: BM25 and Beyond* (2009) | BM25 词项打分 | 使用开源库 `rank-bm25` 的现成实现，未复制论文代码 |
| Cormack, Clarke & Büttcher, *Reciprocal Rank Fusion* (2009) | RRF 融合 | **仅实验**（V2.2 Dense+RRF），未进入提交镜像；结论记录在 `evaluate/v2.2-dense-rrf.md` |
| 官方 Agent Memory API Guide（agentmemories.ai） | `/add`、`/search`、`/health` 接口契约 | 按公开契约实现，未复用其代码 |

代码记忆榜的“Parent/证据”组织方式、状态判定与渲染规则均为本项目自行设计（见 2.5）。

### 2.3 复用的开源仓库与依赖

运行时实际导入的第三方组件（均未修改源码）：

| 组件 | 版本约束 | 许可证 | 用途 |
| --- | --- | --- | --- |
| `rank-bm25` | ≥ 0.2.2 | Apache-2.0 | BM25Okapi 打分 |
| `jieba` | 镜像内安装 | MIT | 中英混合分词 |
| `fastapi` | ≥ 0.115 | MIT | HTTP 服务框架 |
| `uvicorn` | ≥ 0.30 | BSD-3-Clause | ASGI 服务器（单 worker） |
| `pydantic` | ≥ 2.7 | MIT | 请求/响应模型校验 |
| `openai` | 3.26.1 | Apache-2.0 | **服务未使用**：随依赖安装，但服务代码不导入；仅仓库保留的 `llm_clinet.py` 引用，而该模块未被任何服务或评测脚本导入 |

完整依赖清单见 [`requirements.txt`](./requirements.txt)。上述库均按原样使用，
未复制、未改写其内部实现。实验脚本中出现的 `text-embedding-v4` 调用仅用于
V2.2 消融实验，生产镜像的检索链路不包含任何 Embedding 或 LLM 调用。

### 2.4 改动内容声明

本仓库为自研实现，**没有 fork 或改写任何第三方仓库**。如果后续引入，将在此
登记：原仓库地址、原作者、许可证、改动文件与改动要点。

### 2.5 个人方法说明（在生产中的实际形态）

检索链路只做「记忆读取与证据组装」，最终答案由平台统一生成（符合比赛红线）：

1. **写入**：`/add` 按 `request_id` 幂等去重、按 `user_id` 隔离空间，按
   `(role, content)` 哈希生成内容寻址 Parent ID，结构化写入
   `data_store/v3.0/memory.jsonl`。
2. **检索（V2.1 基线）**：消息级 BM25 + 中英混合分词；同一 Parent 取最高分
   unit 作为分数、前 3 个高分 unit 作为证据锚点；负分命中丢弃；单 Parent
   16k、单响应 60k 字符硬预算。
3. **证据组装（V3.0-lite，部署版本）**：在 V2.1 选择结果之上做三件事：
   - **问题焦点**：query 实体 ∪ 最高分锚点实体（排除次锚点，防同 Parent 多子任务交叉）；
     `summary` 锚点只贡献符号不贡献路径，`thinking/note` 锚点贡献路径。
   - **二元绑定 + 解答状态**：patch/test/summary 必须与焦点强关联（完整路径相交、
     或 ≥2 共享强符号、或 query 点名 1 + 共享 1）才渲染；据此判
     `verified_solution / patch_available / summary_only / source_only / related_only`。
   - **答案优先 + 诚实声明**：按状态决定分节顺序与预算，补丁/测试置顶并保留最终
     状态；每条返回带 `[solution_status=...]` 头与英文声明，明确“记忆里是否
     存在已验证修复”。绑不上的补丁/测试/总结一律不渲染（False Solution 门禁）。
4. **可回滚**：`RETRIEVAL_MODE=v2_1` 恢复 V2.1 原输出，不需要数据迁移。

上述方法的实现、单测（68 项）与本地回放证据均在仓库 `src/`、`tests/`、`evaluate/` 中，
可直接复现；方法评估只使用本地回放指标，端到端得分以平台官方评测为准。

### 2.6 未使用项声明

本提交**不使用**以下组件或能力：

- **大语言模型（LLM）**：无 OpenAI / DeepSeek / 通义等任何模型调用；环境变量中的 deepseek 配置未被读取。
- **Embedding / 向量检索**：生产链路无 Dense 召回、无向量数据库（FAISS/Milvus 等）。
- **外部检索或 RAG 框架**：无 LangChain / LlamaIndex，无第三方 rerank 服务。
- **自训练、微调模型或私有模型权重**：无。
- **跨样本共享状态**：无；索引与缓存全部按 `user_id` 隔离。

`/search` 的输出完全由 `src/memory_engine.py` 与 `src/evidence_builder.py` 的本地
BM25 打分和规则化证据组装决定；最终答案由平台统一生成，本服务不生成答案。
