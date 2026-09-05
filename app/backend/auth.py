"""Resolve the caller's identity + access token for OBO.

Precedence:
  1. X-Forwarded-Access-Token  -> browser user via Databricks Apps user-authorization (OBO)
  2. Authorization: Bearer      -> external programmatic caller (App OAuth per-user token / PAT)
  3. Local-dev fallback         -> mint a token from DBX_LOCAL_DEV_PROFILE (never in prod)

Returns an Identity(token, email, source). The token is used verbatim for Statement
Execution and Genie MCP, so all data access is enforced as this user by Unity Catalog.
"""
from __future__ import annotations
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import config

_local_cache: dict[str, str] = {}


@dataclass
class Identity:
    token: str
    email: str
    source: str  # "obo" | "bearer" | "local-dev"


def _local_dev_identity() -> Identity | None:
    if not config.LOCAL_DEV_PROFILE:
        return None
    if "token" not in _local_cache:
        from databricks.sdk.core import Config
        cfg = Config(profile=config.LOCAL_DEV_PROFILE)
        auth_header = cfg.authenticate()["Authorization"]  # "Bearer <token>"
        _local_cache["token"] = auth_header.split(" ", 1)[1]
        # populate host if not already set from env
        if not config.DATABRICKS_HOST and cfg.host:
            config.DATABRICKS_HOST = cfg.host.rstrip("/")
        try:
            from databricks.sdk import WorkspaceClient
            _local_cache["email"] = WorkspaceClient(config=cfg).current_user.me().user_name or "local-dev"
        except Exception:
            _local_cache["email"] = "local-dev"
    return Identity(_local_cache["token"], _local_cache.get("email", "local-dev"), "local-dev")


def get_identity(request: Request) -> Identity:
    fwd = request.headers.get("x-forwarded-access-token")
    if fwd:
        email = (request.headers.get("x-forwarded-email")
                 or request.headers.get("x-forwarded-preferred-username")
                 or request.headers.get("x-forwarded-user") or "unknown")
        return Identity(fwd, email, "obo")

    authz = request.headers.get("authorization")
    if authz and authz.lower().startswith("bearer "):
        return Identity(authz.split(" ", 1)[1], request.headers.get("x-forwarded-email", "api-caller"), "bearer")

    local = _local_dev_identity()
    if local:
        return local

    raise HTTPException(status_code=401, detail="No user token. Expected X-Forwarded-Access-Token "
                        "(browser) or Authorization: Bearer <token> (API caller).")
