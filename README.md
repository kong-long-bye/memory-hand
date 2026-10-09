# Memory Hand — Agent Memory Leaderboard 参赛系统

> 赛道：**代码记忆榜 (Coding Memory)** + **文本记忆榜 (Textual Memory)**
> 组别：**学术方法榜**（开源可复现）
> 当前阶段：Day 1 Baseline（JSONL 存储 + BM25 检索 + 官方格式 /add /search）

---

## 目录

- [1. 项目结构](#1-项目结构)
- [2. 本地运行](#2-本地运行)
- [3. 官方比赛要求总结（代码/文本赛道）](#3-官方比赛要求总结代码文本赛道)
- [4. 部署教程（公网 IP + Docker）](#4-部署教程公网-ip--docker一条路径)
- [5. 提交材料清单](#5-提交材料清单)
- [6. 常见坑与自检](#6-常见坑与自检)

---

## 1. 项目结构

```
memory-hand/
├── app.py                # FastAPI 服务：/health  /add  /search
├── memory_engine.py      # JSONL 持久化 + BM25 检索 + 中英混合分词
├── dataset_adapter.py    # 本地数据集接入（评测用）
├── evaluate.py           # 本地评测脚本（Smoke 前的自测）
├── llm_clinet.py         # LLM 摘要客户端（Day 2 Memory Schema 用）
├── requirements.txt
├── .env                  # API Key（已 gitignore，不提交）
├── Dockerfile            # 生产部署镜像
├── data_store/           # 运行时持久化目录（挂载卷）
└── PLAN_10_DAYS.md       # 10 天优化路线图
```

---

## 2. 本地运行

```bash
# 1) 安装依赖
pip install -r requirements.txt

# 2) 启动服务（默认 8000 端口）
uvicorn app:app --host 0.0.0.0 --port 8000

# 3) 冒烟自检
curl http://127.0.0.1:8000/health
# -> {"status":"ok"}

# 4) 导入数据 + 本地评测（需先建一个 run_add_client 或直接用 dataset_adapter 内置的 build_add_requests）
python dataset_adapter.py          # 打印数据集摘要
python evaluate.py                 # 默认跑 validation.jsonl，输出 Recall@5/10、MRR、nDCG@10、Noise@10、HN-Rejection
```

`.env` 关键项（未填 LLM 会自动走离线抽取式回退，不影响 Day 1 baseline）：

```
LLM_MODE=openai          # 关闭改为 none
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini    # 比赛开源榜要求
LLM_TIMEOUT=20
```

---

## 3. 官方比赛要求总结（代码/文本赛道）

> 来源：`https://agentmemories.ai/api-guide` 与 `https://agentmemories.ai/competition/`
> 多模态赛道不在本 README 覆盖范围。

### 3.1 责任边界（最重要）

平台把评测链路拆成 4 段，**参赛方只负责前两段**：

| 环节 | 责任方 | 说明 |
|------|--------|------|
| **Add** | 参赛方 | 接收记忆 → 存储/索引/更新 |
| **Search** | 参赛方 | 根据 query 返回 Top-K 记忆或证据 |
| Answer | 平台 | 统一 LLM 生成最终答案 |
| Eval | 平台 | 统一评分与复核 |

**红线（违反即判废）**：
- ❌ 不允许在 `/search` 里直接生成最终答案
- ❌ 不允许针对公开题硬编码 / 跨样本共享状态
- ✅ 必须绑定并公开：代码 Commit / 镜像 tag / API 版本 / 鉴权方式

### 3.2 接口契约（必须严格对齐）

两个 POST JSON 接口，路径与字段名**不得改动**：

**`POST /add`**
```json
{
  "request_id": "string，用于幂等去重",
  "messages":   [{"role": "user", "content": "...", "timestamp": 1704153601000}],
  "user_id":    "string，用于记忆空间隔离",
  "session_id": "string"
}
```
Response：`{"success": true, "request_id": "...", "user_id": "...", "session_id": "..."}`

**`POST /search`**
```json
{
  "query":   "string",
  "user_id": "string",
  "top_k":   100,
  "options": ["..."]        // 可选
}
```
Response：`{"data": [{"id": "...", "content": "...", "score": 0.0, "created_at": "..."}]}`

**`GET /health`** 必须存在，用于平台探活。

### 3.3 评测流程

1. **Smoke**：平台用小样本预检接口 + 鉴权 + 端到端链路；有官方限流，可自助反复跑。
2. **Full**：正式评测；单个 Leaderboard Key 当期只能提交 **1 次**，赛事周期内最多 **2 次**。
   - Full 受理即冻结版本（代码、镜像、API、鉴权须与申报一致）。
   - 失败或取消可重跑；成功或部分完成进入 **30 天冷却期**。
3. **复核**：材料 / Smoke / Full / 版本 / 合规 五项全通过才上榜。

### 3.4 参评方义务（时间窗口）

- **公网可访问**：自行部署 HTTP(S) API。
- **保持期**：提交后至 **2026-11-04** 期间不得下线、不得改契约、不得改鉴权与容量。
- **申报容量**：QPS、单次请求超时、并发上限、rate limit 都要在提交文档里写清。

### 3.5 榜单分类

- **文本记忆榜**：整合 PersonaMem、LongMemEval 等公开集，1500+ 对话 / ~5000 题 / ~150M 字符，覆盖长上下文、跨会话个性化、时序事件、剧本交互等 7 项能力。
- **代码记忆榜**：仓库级工程记忆，本次我们接入的 `coding_memory_benchmark_rich` 就是本地镜像版；官方集 2 项核心能力（召回 + 适应）。

### 3.6 组别材料差异

| 组别 | 是否开源 | 材料 |
|------|----------|------|
| **学术方法榜**（本项目） | 是 | 可复现代码或托管 API + 完整开源披露 + Commit 固定 + 环境配置 + 复现说明 |
| **商业产品榜** | 否 | 稳定 API + 鉴权凭证 + 方案简述（不披露机密） |

### 3.7 激励

- 开源方法榜 TOP 10：GPT Pro / Plus 账号
- 社区贡献计划：Kimi Token 额度等

---

## 4. 部署教程（公网 IP + Docker，一条路径）

比赛只要求：**公网可访问 + Add/Search 契约正确 + 版本可追溯 + data_store 不丢**。部署方案就一条：一台带公网 IPv4 的云主机，Docker 直接暴露端口，安全组放行。

### 4.1 部署四步

**Step 1 — 买一台带公网 IPv4 的云主机**（阿里云 ECS / 腾讯云 CVM / AWS Lightsail 都行，最低 2c2g 够用），安全组放开 `8000/tcp`（或你选的任意端口）。

**Step 2 — 服务器上装 Docker**（官方 `get.docker.com` 一键脚本即可）。

**Step 3 — 构建镜像并跑起来**（本地或服务器 build 均可）：

```bash
# 本地 build 后推到镜像仓库（阿里云 ACR / Docker Hub）
git rev-parse --short HEAD                       # 得到 sha，例如 0620fb7
docker build -t memory-hand:day1-0620fb7 .
docker tag  memory-hand:day1-0620fb7 registry.cn-beijing.aliyuncs.com/<ns>/memory-hand:day1-0620fb7
docker push registry.cn-beijing.aliyuncs.com/<ns>/memory-hand:day1-0620fb7

# 服务器上直接跑，把 data_store 挂到宿主机避免重启丢数据
mkdir -p /opt/memory-hand/data_store
docker run -d --name memory-hand \
  -p 8000:8000 \
  -v /opt/memory-hand/data_store:/app/data_store \
  --env-file /opt/memory-hand/.env \
  --restart unless-stopped \
  registry.cn-beijing.aliyuncs.com/<ns>/memory-hand:day1-0620fb7
```

**Step 4 — 本地直接验证**：

```bash
curl http://<公网IP>:8000/health
# -> {"status":"ok"}
```

提交给平台的 API Endpoint 就填 `http://<公网IP>:8000`。

> 当前 BM25 索引和幂等 set 都是**进程内**状态，镜像 CMD 已经固定 `--workers 1`；要横向扩展需先把存储后端换成 SQLite/Redis。

### 4.2 部署自检清单（提交前逐条打勾）

- [ ] `curl http://<公网IP>:8000/health` 返回 `{"status":"ok"}`
- [ ] `POST /add` 官方样例 payload 返回 200 且 `success=true`
- [ ] 同一 `request_id` 连发两次：第二次仍成功，但 `data_store/memory.jsonl` 只有一条（幂等）
- [ ] `POST /search` 对刚写入的 `user_id` 能召回，`top_k` 生效
- [ ] `user_id=A` 的查询召不回 `user_id=B` 的记忆（**跨用户隔离**，红线）
- [ ] 安全组已放开对应端口；`ss -lntp` 看到 uvicorn 监听 `0.0.0.0:8000`
- [ ] `docker image inspect` 或 `git rev-parse HEAD` 得到的版本 tag 与提交材料一致
- [ ] `data_store/` 是宿主机挂载目录，重启不丢数据
- [ ] `.env` 权限 `600`，`git status` 确认 `.env` 未被追踪

### 4.3 容量、超时、限流申报模板（写进提交材料，直接抄）

```
API Endpoint        : http://<公网IP>:8000
Auth                : None
Concurrency         : 16 RPS sustained（单 worker，够用）
Timeout             : Search P95 <= 3s；Add P95 <= 2s；平台侧建议设 30s
Rate Limit          : 无（如需可加 uvicorn --limit-concurrency 64）
Payload Size        : Add 单请求 <= 32MB
Data Persistence    : JSONL 挂载卷 /opt/memory-hand/data_store
Version             : Docker image memory-hand:day1-0620fb7
Downtime SLA        : 保持期 提交日 ~ 2026-11-04，7x24 可访问
```

---

## 5. 提交材料清单（学术方法榜）

对照官方要求，一份份打包：

1. **基本信息**：系统名称、版本 tag、联系人邮箱、机构/团队、参赛赛道（代码 + 文本）、参赛组别（学术方法榜）、提交路径（本仓库 + 部署 IP:端口）。
2. **可复现代码**：本仓库 commit hash（如 `0620fb7`）+ Dockerfile + `requirements.txt`。
3. **运行说明**：本 README §4；关键环境变量说明。
4. **API 契约文档**：本 README §3.2；请求/响应实例附在 `docs/api_examples.md`（可选）。
5. **容量声明**：本 README §4.3 表。
6. **Benchmark 自测报告**：`reports/eval_<run_id>_<ts>.json`，展示 Recall@5/10、MRR、nDCG@10、Noise@10、HN-Rejection。
7. **方法说明**：当前 = BM25 + 中英混合分词 + JSONL 持久化；10 天路线见 `PLAN_10_DAYS.md`（Day 2 LLM 摘要、Day 3 向量、Day 4 RRF、Day 5 Code-aware Rerank、Day 7 Query Rewrite…）。

---

## 6. 常见坑与自检

| 现象 | 原因 | 处理 |
|------|------|------|
| Smoke 超时 | Search 冷启 BM25 索引重建 | 索引改懒加载 + 预热；把大用户加进启动 warm-up |
| Full 阶段召回 0 | `user_id` 空间被污染（跨样本共享状态） | 确认 `MemoryStore.search` 严格按 `user_id` 隔离，不合并索引 |
| data_store 数据丢失 | 容器重启但没挂 volume | `-v /host/data_store:/app/data_store` |
| 中文查询效果差 | 未装 `jieba` 走了字符级回退 | `pip install jieba` 并打进镜像 |
| LLM 摘要挂 | API Key 失效 / 网络问题 | `llm_clinet.summarize` 已内置抽取式回退，不会阻塞主流程 |
| 被平台判"跨样本共享状态" | 在 add/search 里用了全局 cache 未按 user_id 分桶 | 一切按 `user_id` 分片，索引/缓存都按桶隔离 |

---

## 附：10 天优化路线速览

| Day | 目标 | 状态 |
|-----|------|------|
| 1 | Dataset + Add Client + BM25 Store + Search + Eval 闭环 | ✅ |
| 2 | Memory Schema（summary / problem / solution / files / functions / errors / keywords） | 🟡 LLM 客户端已就绪 |
| 3 | 加入 Embedding + FAISS | ⏳ |
| 4 | Hybrid Search + RRF | ⏳ |
| 5 | Code-aware Rerank（file/function/error/repo match） | ⏳ |
| 6 | Memory 压缩格式 A/B | ⏳ |
| 7 | Query Rewrite | ⏳ |
| 8 | Noise / Hard-negative Filtering | ⏳ |
| 9 | Ablation 表 | ⏳ |
| 10 | 工程化 & 提交 | ⏳ |

详见 [PLAN_10_DAYS.md](./PLAN_10_DAYS.md)。

---

**核心心法**（引自 10 天计划）：
> 比赛胜负点不是 Vector Database，而是 **Memory Ranking**。
