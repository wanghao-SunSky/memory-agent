"""Small, explicit identity boundary for a local demo or a token-protected service."""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from . import config


def auth_mode() -> str:
    config.load_secrets()
    return os.environ.get("MEMORY_AUTH_MODE", "local")


def _tokens() -> dict[str, str]:
    try:
        data = json.loads(os.environ.get("MEMORY_API_TOKENS", "{}"))
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict) or any(not isinstance(k, str) or len(k) < 16 or not isinstance(v, str) or not v for k, v in data.items()):
        return {}
    return data


def auth_configured(*, allow_test_auth: bool = False) -> bool:
    mode = auth_mode()
    return mode == "local" or (mode == "token" and bool(_tokens())) or (mode == "test" and allow_test_auth)


def identity(request: Request, *, allow_test_auth: bool = False) -> str:
    mode = auth_mode()
    if mode == "test" and allow_test_auth:
        return request.headers.get("X-Owner-Id", "me")
    if mode == "local":
        try:
            local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            local = False
        if not local or request.url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise HTTPException(403, "本机模式仅允许本机访问；远程访问请配置 token 模式")
        # Browsers can reach localhost from an unrelated site. Do not accept its writes.
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc != request.headers.get("host"):
                raise HTTPException(403, "不接受来自其他站点的写入")
        return os.environ.get("MEMORY_LOCAL_OWNER", "demo")
    if mode != "token" or not _tokens():
        raise HTTPException(503, "身份验证配置不可用")
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        for token, owner in _tokens().items():
            if secrets.compare_digest(credential, token):
                return owner
    raise HTTPException(401, "访问令牌缺失或无效", headers={"WWW-Authenticate": "Bearer"})
