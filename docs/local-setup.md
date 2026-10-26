# macOS 本机运行说明

2026-10-26 已初始化并验证。项目目录：`/Users/mac/Documents/job-research/memory-agent`。

## 日常使用

```bash
cd /Users/mac/Documents/job-research/memory-agent
scripts/local.sh start      # 启动数据库、API 和后台任务进程
scripts/local.sh test -q    # 全部离线测试，固定使用独立测试库
scripts/local.sh status     # 数据库状态与 API 健康检查
scripts/local.sh stop       # 只停止本项目 API 和后台进程，数据库保留
```

- 记忆列表：http://127.0.0.1:8000/?space=work
- 接口文档：http://127.0.0.1:8000/docs，可在这里提交对话、查看任务和提问。
- 只跑第一课相关测试：`scripts/local.sh test tests/test_recall.py -q`。
- 停止数据库：`.local/pgsql/bin/pg_ctl -D .local/pgdata -m fast -w stop`（先停应用并结束测试）。

不要直接用原 Linux 的 `scripts/start.sh`。本机测试使用上述入口，而不是不带配置的 `uv run pytest`；原配置默认连接旧环境的数据库。

## 已安装内容

| 内容 | 本机配置 |
|---|---|
| Python | 原生 ARM CPython 3.12.13，项目 `.venv` |
| Python 依赖 | 按现有 `uv.lock` 安装，未修改依赖版本 |
| PostgreSQL | 16.15，`.local/pgsql`，数据目录 `.local/pgdata` |
| pgvector | 0.8.7，两个数据库均启用 |
| 数据库地址 | 仅监听 `127.0.0.1:55432`，另有项目内 Unix socket |
| 运行库 / 账号 | `memory_learning` / `memory_local` |
| 测试库 / 账号 | `memory_test_learning` / `memory_test_local` |
| 向量模型 | `BAAI/bge-small-zh-v1.5`，512 维，缓存 `.local/models` |
| 日志及进程编号 | `.local/logs/` |

数据库仅用于本机开发，使用本机 trust 认证，不设密码；不要开放到其他机器。两库撤销了 PUBLIC 的连接权限，测试账号无法连接运行库。测试会清空测试库内相关表，不影响运行库。

`.local/`、`.venv/`、`.env.local` 都已被 Git 忽略。服务后台运行，但没有配置开机自启，重启电脑后重新执行 `scripts/local.sh start`。

## 真实模型需要配置密钥

目前没有配置 DeepSeek 密钥。页面、数据库、无记忆弃答和离线测试可运行；有内容的抽取和模型回答尚未验证。后台进程启动不等于真实模型调用可用。

在项目根目录创建 `.env.local`，填入自己的密钥（不要提交 Git，也不必发到聊天中）：

```dotenv
DEEPSEEK_API_KEY=填入自己的密钥
```

文件权限建议设为 `chmod 600 .env.local`，然后执行 `scripts/local.sh stop` 和 `scripts/local.sh start`。配置完成后，可运行 `scripts/local.sh test -m live tests/regression -q`；这会实际调用 DeepSeek 并产生 API 费用，仍只操作测试库。

本机入口关闭 OTLP 网络导出，未安装 Jaeger UI。离线测试中的内存追踪检查仍然运行；页面返回的 Jaeger 链接当前不可用。

## 验证记录

- 全部离线测试：**58 passed, 20 deselected, 1 warning in 2.53s**。
- 其中单元/集成 38/38，离线回归 20/20；20 条真实模型测试未运行。
- 警告来自依赖 `pydantic_graph` 获取事件循环的旧用法，未影响通过。
- `/healthz`、`/`、`/docs`、`/memories` 均返回 HTTP 200；空记忆 `/recall` 返回弃答和空引用。
- 测试账号连接运行库被数据库拒绝。
- 摘要：`reports/last_offline.json`；完整测试日志：`.local/logs/pytest-offline.log`；接口检查：`.local/logs/setup-smoke.json`。
- 原测试报告在每次测试前备份到 `.local/logs/offline-before-*.json`。

本次只初始化环境和增加本机入口，未修复第一课的无效引用问题。现有测试通过不表示该问题不存在。

## 环境重建依据

默认 Homebrew Python 运行于 Rosetta，无法安装锁定版本的 ARM ONNX Runtime。本次使用：

```bash
uv python install cpython-3.12-macos-aarch64-none
uv sync --locked --python cpython-3.12-macos-aarch64-none
```

数据库按 [PostgreSQL 官方安装说明](https://www.postgresql.org/docs/16/install-make.html) 和 [pgvector 官方说明](https://github.com/pgvector/pgvector) 从源码安装。构建源码保存在 `.local/build/`，构建日志保存在 `.local/logs/`，未修改系统 Homebrew 服务。

- PostgreSQL 源码：`https://ftp.postgresql.org/pub/source/v16.15/postgresql-16.15.tar.bz2`，SHA256：`c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed`。
- pgvector 源码：`https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.7.tar.gz`，SHA256：`cac0b10c360f05b2d521200105ba3697e773d4cd3731f5a915a7e37ebe0bea85`。

已解压源码的重编译命令（从项目根目录执行）：

```bash
PROJECT_DIR="$PWD"
cd "$PROJECT_DIR/.local/build/postgresql-16.15"
./configure --prefix="$PROJECT_DIR/.local/pgsql" --without-icu --without-readline --without-zlib
make -j8
make install
cd "$PROJECT_DIR/.local/build/pgvector-0.8.7"
make -j8 PG_CONFIG="$PROJECT_DIR/.local/pgsql/bin/pg_config"
make install PG_CONFIG="$PROJECT_DIR/.local/pgsql/bin/pg_config"
cd "$PROJECT_DIR"
scripts/local.sh init
```

`init` 可重复执行，不清空数据库。`local.sh` 设置持久模型缓存，并仅在自身进程内移除 SOCKS 兜底代理，保留 HTTP(S) 代理，避免未安装 SOCKS 扩展导致模型下载失败。
