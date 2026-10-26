# memory-agent

个人长期记忆后端：从对话中提取事实，保存变更历史，按工作/生活空间检索，并支持删除验证。

## 工作流程

- 写入：对话 → 脱敏、去重 → 后台提取事实 → 新增或作废旧记忆。
- 提问：检索有效记忆 → 模型生成带引用的回答；没有相关记忆时直接弃答。
- 删除：删除记忆版本链及相关数据 → 检查是否仍能被检索到。

技术栈：Python 3.12+、FastAPI、Pydantic AI、PostgreSQL + pgvector、DeepSeek、本地中文向量模型。

## 运行

需要 `uv`、PostgreSQL 16+ 和 pgvector。先创建运行库 `memory`、独立测试库 `memory_test`，并在两个库中启用 `vector` 扩展。

```bash
uv sync --locked
export DATABASE_URL='postgresql+psycopg://用户名:密码@127.0.0.1:5432/memory'
export MEMORY_SECRETS_FILE="$PWD/.env.local"
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=''
uv run python -m memory_agent.migrate
uv run uvicorn --factory memory_agent.api:build --host 127.0.0.1 --port 8000
```

在 `.env.local` 中设置 `DEEPSEEK_API_KEY`。另开终端，设置相同环境变量后启动后台任务：

```bash
uv run python -m memory_agent.worker
```

打开 [记忆列表](http://127.0.0.1:8000/) 或 [接口文档](http://127.0.0.1:8000/docs)。首次使用会下载向量模型；真实抽取和回答需要 DeepSeek 密钥。

### 已初始化的本机 macOS 环境

本机已在 `.local/` 安装数据库和模型，直接使用：

```bash
scripts/local.sh start     # 启动数据库、API、后台任务
scripts/local.sh test -q   # 使用独立测试库运行离线测试
scripts/local.sh stop      # 停止 API 和后台任务
```

此入口仅适用于已经初始化的本机环境，不是全新机器的安装器。日志在 `.local/logs/`；本机数据库端口为 55432，默认不启用 Jaeger。

## 测试

```bash
DATABASE_URL='postgresql+psycopg://用户名:密码@127.0.0.1:5432/memory_test' uv run pytest -q
```

测试会清空指定测试库中的项目表。默认使用模型替身，不调用真实模型；运行真实模型回归需配置密钥，并加 `-m live tests/regression`。

2026-10-26 本机离线测试：58/58 通过。该结果验证程序行为，不代表真实模型质量。

## 代码入口

| 路径 | 职责 |
|---|---|
| `src/memory_agent/api.py` | HTTP 接口与记忆列表页面 |
| `store.py`、`jobs.py`、`worker.py` | 写入与后台任务 |
| `engine.py`、`recall.py` | 事实提取、更新、检索与回答 |
| `deletion.py` | 删除及验证 |
| `migrations/`、`tests/` | 数据库结构与测试 |

表中 Python 文件均位于 `src/memory_agent/`。

## 当前限制

尚无用户鉴权；历史时间参数未用于检索过滤；引用全部无效时仍可能返回答案。模型调用占用数据库事务，删除一条记忆不会自动删除同一对话产生的其他事实。
