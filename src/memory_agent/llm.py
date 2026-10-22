"""模型选择。生产用 DeepSeek（OpenAI 兼容）；测试通过 use_models() 注入 TestModel/FunctionModel。"""
from __future__ import annotations

import contextlib
import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
from typing import Iterator

from pydantic_ai.models import Model

from . import config

_overrides: dict[str, Model] = {}


def get_model(role: str) -> Model:
    """role: extract / update / answer"""
    if role in _overrides:
        return _overrides[role]
    if "*" in _overrides:
        return _overrides["*"]
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.deepseek import DeepSeekProvider

    config.load_secrets()
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置（应在 secrets 文件中）")
    # 每次新建 client：run_sync 每次可能在不同事件循环里跑。
    # OpenAI SDK 自带对 429/5xx/连接错误的指数退避重试（max_retries=3）。
    client = AsyncOpenAI(base_url=config.DEEPSEEK_BASE_URL, api_key=key, max_retries=3, timeout=60.0)
    return OpenAIChatModel(config.DEEPSEEK_MODEL, provider=DeepSeekProvider(openai_client=client))


def model_name(role: str) -> str:
    m = _overrides.get(role) or _overrides.get("*")
    return m.model_name if m is not None else config.DEEPSEEK_MODEL


@contextlib.contextmanager
def use_models(**models: Model) -> Iterator[None]:
    old = dict(_overrides)
    _overrides.update(models)
    try:
        yield
    finally:
        _overrides.clear()
        _overrides.update(old)


MODEL_SETTINGS = {"temperature": 0.0}
