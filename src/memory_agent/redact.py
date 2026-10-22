"""写入前的敏感信息正则打码（手机号、验证码、证件号、银行卡、密钥）。"""
from __future__ import annotations

import re

# 顺序有意义：先打长串（密钥/证件），再打短串
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("secret", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b"), "[密钥]"),
    ("secret", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[密钥]"),
    ("secret", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[密钥]"),
    ("secret", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\b"), "[密钥]"),
    ("secret", re.compile(r"(?i)((?:api[_\- ]?key|secret|token|password|passwd|密钥|密码|口令)\s*(?:(?:是|为)\s*[:：=]?|[:：=])\s*)\S+"), r"\1[密钥]"),
    ("id_card", re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![\dA-Za-z])"), "[证件号]"),
    ("bank_card", re.compile(r"(?<!\d)(?:\d[ \-]?){15,18}\d(?!\d)"), "[卡号]"),
    ("phone", re.compile(r"(?<!\d)(?:\+?86[\- ]?)?1[3-9]\d[\- ]?\d{4}[\- ]?\d{4}(?!\d)"), "[手机号]"),
    ("otp", re.compile(r"((?:验证码|校验码|动态码|verification code|code)\s*(?:是|为)?\s*[:：]?\s*)\d{4,8}(?!\d)", re.I), r"\1[验证码]"),
    ("otp", re.compile(r"(?<!\d)\d{4,8}(?!\d)(?=\s*(?:是|为)?(?:你的|您的)?(?:验证码|校验码|动态码))"), "[验证码]"),
]


def redact(text: str) -> tuple[str, list[str]]:
    """返回 (打码后文本, 命中的类别列表)。"""
    kinds: list[str] = []
    out = text
    for kind, pat, repl in _RULES:
        out, n = pat.subn(repl, out)
        if n:
            kinds.append(kind)
    return out, sorted(set(kinds))
