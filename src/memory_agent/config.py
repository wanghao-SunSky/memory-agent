"""配置。密钥只从 secrets 文件/环境变量读取，绝不写入仓库、日志或 trace。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

SECRETS_FILE = Path(os.environ.get("MEMORY_SECRETS_FILE", ".env.local"))


def load_secrets() -> None:
    if SECRETS_FILE.exists():
        load_dotenv(SECRETS_FILE, override=False)


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql+psycopg://localhost/memory")


def otlp_endpoint() -> str | None:
    # 设为空字符串则关闭导出
    v = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4318/v1/traces")
    return v or None


def jaeger_ui() -> str:
    return os.environ.get("JAEGER_UI", "http://localhost:16686")


load_secrets()


def model_mode() -> str:
    mode = os.environ.get("MEMORY_MODEL_MODE", "demo")
    if mode not in {"demo", "live"}:
        raise ValueError("MEMORY_MODEL_MODE must be demo or live")
    return mode


DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
EMBED_DIM = 512

# 检索参数
RECALL_TOP_K = int(os.environ.get("RECALL_TOP_K", "8"))
# 余弦距离阈值：超过视为不相关（bge-small-zh-v1.5 上的小样本校准，见 README）
RECALL_MAX_DISTANCE = float(os.environ.get("RECALL_MAX_DISTANCE", "0.62"))
UPDATE_TOP_S = 10
UPDATE_MAX_DISTANCE = float(os.environ.get("UPDATE_MAX_DISTANCE", "0.6"))
