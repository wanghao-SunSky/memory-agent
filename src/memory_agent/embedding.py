"""本地中文 embedding（DeepSeek 无 embedding 接口）。BAAI/bge-small-zh-v1.5，512 维，ONNX 推理。"""
from __future__ import annotations

import threading

import numpy as np

from . import config

_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
_model = None
_lock = threading.Lock()


def _get():
    global _model
    with _lock:
        if _model is None:
            from fastembed import TextEmbedding

            _model = TextEmbedding(config.EMBED_MODEL)
    return _model


def embed_passages(texts: list[str]) -> list[np.ndarray]:
    if not texts:
        return []
    return [np.asarray(v, dtype=np.float32) for v in _get().embed(texts)]


def embed_passage(text: str) -> np.ndarray:
    return embed_passages([text])[0]


def embed_query(text: str) -> np.ndarray:
    # bge 系列建议给短查询加检索指令前缀
    return embed_passages([_QUERY_INSTRUCTION + text])[0]
