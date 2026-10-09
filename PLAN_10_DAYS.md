# Agent Memory Leaderboard - 10天优化开发计划

## 项目目标

本比赛的核心目标：

> 在固定模型基础上，通过 Memory Retrieval（记忆检索）提升 Coding Agent 最终任务得分。

核心链路：

```
Memory
  |
  v
Retrieval
  |
  v
Ranking
  |
  v
Useful Context
  |
  v
Agent Success
```

重点不是开发 Agent，而是优化：

- Memory 存储
- Memory 检索
- Memory 排序
- 返回给模型的上下文质量

---

# 最终系统架构

```
                  Add API
                     |
             Memory Processing
                     |
        +------------+-------------+
        |                          |
 Structured Store            Search Index
        |                          |
 SQLite/JSONL              +-------------+
                           |             |
                         BM25          Vector
                           |             |
                           +------+------+
                                  |
                                RRF
                                  |
                         Code-aware Rerank
                                  |
                              Top-K Memory
                                  |
                              Search API
```

推荐技术栈：

```
Python

FastAPI
SQLite / JSONL
FAISS
rank-bm25
sentence-transformers
numpy
```

不建议使用 LangChain 作为核心框架。

原因：

- 增加抽象层
- 不利于 Retrieval 实验
- 不方便进行 Ranking 调优

---

# Day 1：构建本地测试集 + 跑通完整流程

## 目标

第一天完成本地 Benchmark 闭环，并获得第一个 baseline score。

---

## Task 1：接入本地测试数据集

已有：

```
coding_memory_benchmark_rich
```

目标：

模拟官方评测流程。

流程：

```
Local Dataset

↓

Add API Client

↓

Memory Service

↓

Search API

↓

Evaluation Script

↓

Score
```

---

## Task 2：实现 Add API Client

单独编写数据发送程序。

不要直接读取数据进入数据库，而是模拟官方调用方式：

```
Benchmark Dataset

        |

        v

POST /add

        |

        v

Memory System
```

请求格式：

```json
{
  "request_id": "...",
  "messages": [],
  "user_id": "...",
  "session_id": "..."
}
```

要求：

- 支持批量导入
- 保持官方字段格式
- request_id 可追踪
- 支持失败重试

---

## Task 3：实现 Search API

完成：

```
POST /search
```

第一版：

```
BM25 Retrieval
```

---

## Task 4：Memory Storage

初期使用：

```
memory.jsonl
```

结构：

```json
{
  "id": "001",
  "user_id": "xxx",
  "content": "memory text"
}
```

---

## Day 1 输出

必须完成：

```
Dataset
 |
Add Client
 |
Memory Store
 |
Search
 |
Evaluation
```

得到：

```
Baseline Score
```

---

# Day 2：优化 Memory Schema（记忆结构）

不要只保存 raw text。

推荐结构：

```json
{
  "summary": "",
  "problem": "",
  "solution": "",
  "files": [],
  "functions": [],
  "errors": [],
  "keywords": []
}
```

目标：

提升 Memory 的可检索性。

---

# Day 3：加入向量检索

加入：

```
Embedding
+
FAISS
```

比较：

| 方法            | Score |
| --------------- | ----- |
| BM25            |       |
| Dense Retrieval |       |

---

# Day 4：Hybrid Search 混合检索

不要简单平均：

```
BM25 + Vector
```

采用：

## RRF Fusion

公式：

```
score =
1/(60 + rank_bm25)
+
1/(60 + rank_vector)
```

流程：

```
BM25 Top100

+

Vector Top100

↓

RRF

↓

Top100
```

---

# Day 5：Code-aware Ranking 代码感知排序

这是核心优化方向。

增加：

- File Match
- Function Match
- Error Match
- Repo Match

示例：

```
query:

refresh_cache bug


memory:

refresh_cache()

cache.py

stale data
```

提高排序权重。

---

# Day 6：Memory 压缩与表达优化

实验不同 Memory 格式：

```
Raw Conversation

vs

Summary

vs

Structured Memory
```

比较效果。

---

# Day 7：Query Enhancement 查询增强

加入：

```
LLM Query Rewrite
```

用途：

- Query Expansion
- 技术关键词提取

不要让 LLM 直接负责搜索。

---

# Day 8：Noise 优化

优化：

- False Positive
- 相似但无用的 Memory
- 重复 Memory

增加：

Negative Filtering。

---

# Day 9：Benchmark 与 Ablation 实验

记录：

| Version         | Score |
| --------------- | ----- |
| BM25            |       |
| Dense           |       |
| Hybrid          |       |
| + Memory Schema |       |
| + Rerank        |       |
| + Query Rewrite |       |

找到真正有效的优化。

---

# Day 10：工程化与最终调优

## 性能优化

- embedding cache
- batch search
- index loading


## 稳定性测试

- timeout
- empty result
- error handling


## 文档整理

输出：

```
README.md

Architecture

Benchmark Result

API Documentation
```

---

# 最终实现方案

```
FastAPI

+

SQLite / JSONL

+

FAISS

+

BM25

+

RRF

+

Code-aware Reranker

+

Optional Query Rewrite
```

核心：

> 比赛胜负点不是 Vector Database，而是 Memory Ranking。