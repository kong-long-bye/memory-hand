# Memory Hand — Agent Memory Leaderboard 参赛系统

> 赛道：**代码记忆榜 (Coding Memory)** + **文本记忆榜 (Textual Memory)**
> 组别：**学术方法榜**（开源可复现）
> 当前阶段：Day 1 Baseline（JSONL 存储 + BM25 检索 + 官方格式 /add /search）
>
> 提交用**部署信息与原始工作披露**见 [guide.md](./guide.md)（版本、接口地址、鉴权、容量、运行限制、复用声明、个人方法）。

---

## 目录

- [1. 项目结构](#1-项目结构)
- [2. 本地运行](#2-本地运行)
- [3. 官方比赛要求总结（代码/文本赛道）](#3-官方比赛要求总结代码文本赛道)
- [4. 部署教程（镜像 tar + 一键脚本）](#4-部署教程镜像-tar-上传--一键启动)
- [5. 提交材料清单](#5-提交材料清单)
- [6. 常见坑与自检](#6-常见坑与自检)

---

## 1. 项目结构

```
memory-hand/
├── src/                    # 服务代码
│   ├── app.py              # FastAPI 服务：/health  /add  /search
│   ├── memory_engine.py    # JSONL 持久化 + BM25 检索 + 中英混合分词
│   ├── service_log.py      # 服务日志（stdout + data_store/service.log 双写）
│   └── llm_clinet.py       # 离线摘要模块（未被服务导入，服务不调用 LLM）
├── requirements.txt
├── .env                    # API Key（已 gitignore，不提交）
├── Dockerfile              # 生产镜像
├── docker-compose.yml      # 服务器侧 compose（与镜像包一起上传）
├── deploy.sh               # 服务器一键部署脚本
├── dist/                   # docker save 产物（.gitignore已忽略）
├── data/                   # 数据集（已 gitignore）（大模型构建的，根据官方示例的文档构建）
├── evaluate/               # 本地模拟官方评测（Add/Search/Agent/验证，这个是用大模型构建的数据集来测试，一点效果都没有）

```

代码目录不是包：模块之间用平铺导入（`from memory_engine import ...`），
所以启动/评测要么带上 `--app-dir src`，要么直接 `python src/xxx.py`（脚本所在目录会自动进 `sys.path`）。
数据目录固定在仓库根（`data/`、`data_store/`、`reports/`），由 `memory_engine.PROJECT_ROOT` 统一解析。

---

## 2. 本地运行

```bash
# 1) 安装依赖
pip install -r requirements.txt

# 2) 启动服务（默认 7878 端口，与服务器保持一致；在仓库根执行）
uvicorn app:app --app-dir src --host 0.0.0.0 --port 7878

# 3) 冒烟自检
curl http://127.0.0.1:7878/health
# -> {"status":"ok"}
# 特别注意，env配置文件必须有下面内容，否者会自动回退到v2.1 ，效果不如v3.0
RETRIEVAL_MODE=v3_0
```


> **当前部署版本不使用 LLM（同时也没有使用dense，只有BM25）**：服务不读取下列 LLM 配置，`llm_clinet.py` 未被任何服务或评测脚本导入；这些项仅为历史遗留/离线实验保留，不影响 `/add`、`/search` 的返回。

> 因此只需要配置 RETRIEVAL_MODE=v3_0





## 2. 部署教程（镜像 tar 上传 + 一键启动）

前提：服务器**不能访问 Docker Hub / 镜像仓库**，一切依赖本地构建后把镜像打包上传。部署方式固定为一键脚本 `deploy.sh`。

目录约定（服务器上）：

```
/opt/memory-hand/
├── memory-hand-v3_0.tar        # docker save -o 导出的镜像包
├── docker-compose.yml          # 仓库里拷一份
├── .env                        # 环境变量（自行上传，不在仓库）
├── deploy.sh                   # 仓库里拷一份
└── data_store/                 # 持久化目录（deploy.sh 自动创建）
```

### 4.1 四步部署

**Step 1 — 本地构建镜像**

```bash
cd d:\Code\study\memory-hand
# 每次改动必须换一个新 tag，否则 docker load 会命中同名旧镜像缓存，
# 服务器实际跑的还是旧代码（端口、LLM 客户端修复全部失效）
docker build -t memory-hand:v3_0 .
docker run --rm memory-hand:v3_0 ps aux | head -1   # 可选：确认 CMD 已含 --port 7878
```

**Step 2 — 本地导出镜像包**

```bash
mkdir -p dist
# 用 -o 指定输出文件；Windows PowerShell 没有 gzip，不要用 `| gzip`
docker save -o dist/memory-hand-v3_0.tar memory-hand:v3_0
ls -lh dist/                                      # 一般 200–400MB
```

**Step 3 — 上传 4 个文件到服务器**

```bash
IP=<公网IP>
ssh root@$IP "mkdir -p /opt/memory-hand"
scp dist/memory-hand-v3_0.tar     root@$IP:/opt/memory-hand/
scp docker-compose.yml             root@$IP:/opt/memory-hand/
scp deploy.sh                      root@$IP:/opt/memory-hand/
scp .env                           root@$IP:/opt/memory-hand/
ssh root@$IP "chmod 600 /opt/memory-hand/.env"
```

**Step 4 — 服务器上执行 `deploy.sh`**

```bash
ssh root@$IP
cd /opt/memory-hand
bash deploy.sh                    # 默认加载当前目录下的 memory-hand-v3_0.tar
```

脚本依次做 4 件事：
1. 预检（镜像包 / compose / .env / docker 存在）
2. `docker load -i <tar>` 导入镜像
3. `docker compose up -d` 启动（自动挂 `data_store`，`--restart=unless-stopped`）
4. `curl http://127.0.0.1:7878/health` 探测，失败自动 `docker compose logs --tail 50`

验证成功后，服务在服务器本地监听 `7878`。若公网端口与容器端口不同（例如云主机做了 `17187 -> 7878` 映射），平台 API Endpoint 应填映射后的公网地址 `http://<公网IP>:17187`，而不是 7878。

排查顺序（从内向外，定位是哪一层断了）：

```bash
docker logs memory-hand | grep Uvicorn     # 1) 容器内实际监听端口，必须是 0.0.0.0:7878
docker ps  --filter name=memory-hand       # 2) PORTS 必须是 0.0.0.0:7878->7878/tcp，无多余 8080/tcp
curl http://127.0.0.1:7878/health          # 3) 服务器自身可达
firewall-cmd --list-ports                  # 4) 系统防火墙未放行则外部连不上
```
若 1–3 全通但公网 `ERR_CONNECTION_REFUSED`，问题在云厂商端口映射/安全组，与应用无关。

对照官方要求，一份份打包（详细信息在guild.md）：

1. **基本信息**：系统名称、版本 tag、联系人邮箱、机构/团队、参赛赛道（代码 ）、参赛组别（学术方法榜）、提交路径（本仓库 + 部署 IP:端口）。
2. **可复现代码**：本仓库 commit hash（如 `0620fb7`）+ Dockerfile + `requirements.txt`。
3. **运行说明**：本 README §4；关键环境变量说明。
4. **API 契约文档**：本 README §3.2；请求/响应实例附在 `docs/api_examples.md`（可选）。
5. **容量声明**：本 README §4.4 表。
6. **Benchmark 自测报告**：`reports/eval_<run_id>_<ts>.json`，展示 Recall@5/10、MRR、nDCG@10、Noise@10、HN-Rejection。
7. **方法说明**：当前 = BM25 + 中英混合分词 + JSONL 持久化；
8. **部署信息与原始工作披露**：[guide.md](./guide.md)（已部署版本、接口地址、鉴权、容量、运行限制、复用与个人方法说明）。

---
