# Memory Agent

[![Tests](https://github.com/wanghao-SunSky/memory-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/wanghao-SunSky/memory-agent/actions/workflows/tests.yml)

可运行的个人记忆 Agent：从对话提取事实，保留变更历史，按用户和空间检索；回答带引用，证据不足时弃答。附浏览器工作台、自动测试和固定样例评测。

## 能做什么

- **记住与更新**：对话脱敏、去重 → 后台提取 → 新增、确认或替换事实，保留版本链。
- **两种问答**：普通 RAG 一次检索后回答；Agent 自主选择查记忆、查旧版本、查来源，受调用与时间预算约束。
- **历史与删除**：按事件有效时间查询；删除整条版本链及关联来源，并检查检索残留。
- **完整演示**：页面写入、任务进度与重试、提问、证据、历史、删除；服务端身份校验，工作/生活空间隔离。

技术栈：Python 3.12、FastAPI、Pydantic AI、PostgreSQL + pgvector、DeepSeek、本地中文向量模型、OpenTelemetry。

## 启动

### Docker（跨平台）

安装并启动 Docker（Compose 2.24+）：

```bash
git clone https://github.com/wanghao-SunSky/memory-agent.git
cd memory-agent
scripts/docker.sh start
scripts/docker.sh token   # 在页面中填入本机生成的访问令牌
```

打开 [工作台](http://127.0.0.1:8000/) 或 [API 文档](http://127.0.0.1:8000/docs)。首次会下载向量模型。`scripts/docker.sh stop` 停止服务并保留数据。

### Apple Silicon macOS（原生）

需要 Apple Command Line Tools（`xcode-select --install`）：

```bash
scripts/bootstrap.sh       # 安装 uv、Python、项目内 PostgreSQL/pgvector，创建独立运行库和测试库
scripts/local.sh start
scripts/local.sh status
scripts/local.sh stop       # 停 API/worker；db-stop 同时停止数据库
```

数据库与日志位于 `.local/`，不修改系统数据库；API 监听 `127.0.0.1:8000`，数据库端口 `55432`。本地模式固定为 `demo` 用户，不接受请求头伪造身份。

### 使用真实模型

默认 `demo` 使用有限规则，**不是大语言模型**；可以用页面内置的面试安排/改期示例走通流程。真实抽取和问答请创建 `.env.local`：

```dotenv
MEMORY_MODEL_MODE=live
DEEPSEEK_API_KEY=填写你自己的密钥
```

然后 `scripts/local.sh restart`，或重新执行 `scripts/docker.sh start`。文件已被 Git 和 Docker 构建忽略。其余配置见 [.env.example](.env.example)。容器强制使用令牌认证，默认也只监听本机；公开部署需另行配置 HTTPS、限流及独立凭证。

## 验证

```bash
scripts/local.sh test -q                              # 离线：阻止外部模型请求
scripts/local.sh test -m live tests/regression -q      # 真实 DeepSeek，产生 API 用量
scripts/local.sh evaluate --mode demo                 # 两种问答的固定样例比较
scripts/local.sh evaluate --mode live                 # 真实模型效果、延迟与 token
```

测试仅允许 `memory_test` / `memory_test_learning`，会清空其中的项目表。评测使用独立样例用户，不清空表；不要与测试同时运行。

自备数据库也可 `uv sync --locked`，设置 `DATABASE_URL` 后运行 `uv run pytest -q` 或 `uv run python scripts/evaluate.py --mode demo`。需先在测试库启用 `vector` 扩展。CI 自动运行离线测试、Docker 构建和 HTTP 全流程验收。

[测试报告](reports/)区分离线行为验证和真实模型回归；[评测集](evals/cases.json)覆盖更新、历史、弃答、隔离和删除。样例规模小，不能作为泛化准确率或生产性能承诺。

## 代码导航

| 入口 | 职责 |
|---|---|
| [`api.py`](src/memory_agent/api.py)、[`auth.py`](src/memory_agent/auth.py)、[`ui.py`](src/memory_agent/ui.py) | 接口、身份与页面 |
| [`store.py`](src/memory_agent/store.py)、[`jobs.py`](src/memory_agent/jobs.py)、[`worker.py`](src/memory_agent/worker.py) | 对话入库、队列与失败恢复 |
| [`engine.py`](src/memory_agent/engine.py) | 结构化提取、更新决策、事务与版本链 |
| [`recall.py`](src/memory_agent/recall.py)、[`investigation.py`](src/memory_agent/investigation.py) | 检索回答与有界工具循环 |
| [`deletion.py`](src/memory_agent/deletion.py)、[`tests/`](tests/) | 删除验证与行为证据 |

## 边界

`as_of` 查询事实的**事件有效时间**，不能重现“系统当时知道什么”。有效引用检查只保证引用来自本次检索，不能证明每句话都被证据支持。Agent 最多 6 次模型请求、8 次工具调用、16,000 token；45 秒取消等待，SQL 有剩余时间限制，已经开始的本地模型加载线程无法强杀。

写入仍在数据库事务内调用模型，适合单机演示，未做生产负载验证。删除会排除共享来源，但同一对话提取出的其他事实会保留并明确列出；未实现备份副本、模型提供商日志的删除。`/readyz` 检查数据库、worker 心跳和配置，不代表模型提供商实时可用。
