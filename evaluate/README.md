# 本地评测（模拟官方 Add / Search / 解题验证）

> Agent 的评测流程、本次结果和“为什么检索不是瓶颈”的分析，见 [agent-eval-qa.md](agent-eval-qa.md)。

用真实 smoke 数据在本地复现官方的评测链路，不消耗平台上传次数：

```
official_requests.jsonl          dataset 里的任务
        |                              |
        v                              v
  重建记忆库（/add）            准备 astropy 工作区
        |                              |
        +--------> 解题 Agent <--------+
                      |
              search_memory / 读写代码 / 跑测试
                      |
                      v
        模型补丁 + 官方 test patch -> pytest -> 是否解决
```

## 与官方评测的对应关系

| 官方 | 本地 |
| --- | --- |
| 平台向 `/add` 灌入 relevant / noisy 轨迹 | 用 `data_store/official_requests.jsonl` 里的 `/add` 请求重建内存库 |
| 解题 Agent 调 `/search` | Agent 的 `search_memory` 工具直接调用同一个 `MemoryStore.search` |
| Agent 在隔离仓库里改代码 | 每次运行从 `work/.../template` 复制一份干净工作区 |
| 验证程序跑 FAIL_TO_PASS / PASS_TO_PASS | 官方 test patch + pytest，默认跑全部 1+20 个测试 |
| 平台评分 | 报告里的 `verification.resolved` |

记忆条件按数据自动识别，不写死 user_id：

- `relevant`：只包含 astropy timeseries 轨迹的 user（smoke 里是 2 个 Parent）；
- `noisy`：同一批相关轨迹 + progressbar / furlong 无关轨迹的 user（4 个 Parent）。

## 目录

```
evaluate/
├── run_eval.py          # 入口：oracle / replay / agent
├── agent.py             # OpenAI 兼容 tool-calling 解题 Agent
├── memory_env.py        # 官方 /add 回放 + relevant/noisy 选择
├── sandbox.py           # 工作区复制、补丁提取/过滤、pytest 验证
├── env.py               # 路径、.env、任务数据
├── setup_workspace.py   # 从 venv 里的 astropy 5.0 构建模板仓库
├── requirements-eval.txt
├── work/                # 模板与每次运行的副本（gitignore）
└── reports/             # JSON 报告、补丁、transcript（gitignore）
```

## 准备环境

```bash
# 建议 Python 3.10
python -m venv evaluate/.venv
evaluate/.venv/Scripts/python.exe -m pip install -r evaluate/requirements-eval.txt

# 构建基准工作区（只需一次；已存在会跳过）
evaluate/.venv/Scripts/python.exe evaluate/setup_workspace.py
```

模型配置沿用仓库根 `.env`：`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`LLM_MODE`、
`LLM_TIMEOUT`（可选 `EVAL_MAX_TOKENS`，默认 8192）。

## 运行

```bash
# 1) 先验证评测链路本身：应用官方 gold patch，应该 resolved=True
evaluate/.venv/Scripts/python.exe evaluate/run_eval.py --mode oracle

# 2) 只看记忆检索：回放官方 /search（按 user 过滤，每个条件 15 条），统计命中相关记忆的比例
evaluate/.venv/Scripts/python.exe evaluate/run_eval.py --mode replay --memory relevant
evaluate/.venv/Scripts/python.exe evaluate/run_eval.py --mode replay --memory noisy

# 3) 完整本地评测：Agent 检索记忆 -> 改代码 -> 应用 test patch -> pytest
evaluate/.venv/Scripts/python.exe evaluate/run_eval.py --mode agent --memory relevant
evaluate/.venv/Scripts/python.exe evaluate/run_eval.py --mode agent --memory noisy

# 换模型 / 限制步数 / 指定 user
evaluate/.venv/Scripts/python.exe evaluate/run_eval.py --mode agent \
    --model deepseek-v4-flash-0731 --steps 40 --memory noisy --user-id u_xxx
```

`--mode all` 依次跑 oracle、replay、agent。`--p2p none` 只跑 FAIL_TO_PASS。

## 报告

`evaluate/reports/<时间戳>_<instance>_<mode>_<memory>.json`：

- `memory`：选中的 user、Parent 数量、各 user 的相关/无关轨迹统计；
- `agent`：停止原因、步数、token 用量、每次 `search_memory` 的 query/结果 id；
- `patch`：模型补丁字符数，补丁与逐步 transcript 的文件路径；
- `verification`：是否 resolved、通过/失败数量、失败节点、pytest 输出尾部。

Agent 模式的产物：

- `<run_id>.model.patch`：Agent 的 `git diff`（验证时会剔掉 test patch 覆盖的文件）；
- `<run_id>.transcript.jsonl`：每一步的 assistant 内容和工具调用结果。

## 与官方评测的差异

1. **代码替身**：官方 base_commit 是 astropy 4.3 附近，本地用 astropy 5.0 未修复版
   作为替身。官方 test patch 能直接应用，oracle 模式下 21 个测试全部通过，说明修复
   前后的行为一致；但不等同于在精确 base_commit 上运行。
2. **记忆检索走进程内调用**：不经过 HTTP，但用的是 `src/memory_engine.py` 同一份
   代码；`/add` 请求就是官方原始请求。
3. **模型不同**：官方用 gpt-4o-mini，本地默认读 `.env` 的模型。不同模型的结果不可
   直接对比平台分数。
4. **工具集是近似**：官方 Agent 的完整工具链未公开，本地提供检索、列目录、读文件、
   grep、写文件、精确替换、执行命令和结束工具。
5. **网络隔离**：`run_command` 拒绝 `curl`/`wget`/`pip download`/`git clone`/
   `urllib` 等联网命令，并给子进程设置不可达代理。否则 Agent 会直接抓取上游修复，
   本地结果就不再反映“只能依赖记忆和代码”的官方条件。
5. **单题**：smoke 数据只有 `astropy__astropy-13033`；300 个评测单元的结构已在
   `run_eval.py` 中按单题实现，扩题需要补对应的数据集记录和基准工作区。

## 注意

- `run_command` 会在本地执行模型生成的 shell 命令，工作目录是 `evaluate/work/` 下的
  副本；不要把它指向含敏感文件或未提交改动的仓库。
- Agent 被要求不要改测试文件；即使改了，验证阶段也会从模型补丁中剔除 test patch
  覆盖的文件，再应用官方 test patch。
