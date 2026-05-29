"""EGI Secret Store (HashiCorp Vault) client for per-user API key management.

Users authenticate to the Vault via their EGI Check-in access token (JWT auth).
Secrets are stored at: {kv_mount}/[data/]{user_sub}/api-keys/{key_id}
"""

from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import Request
from pydantic import BaseModel

from data_commons_search.auth import UserInfo, require_auth
from data_commons_search.config import settings
from data_commons_search.utils import logger

router = APIRouter(prefix="/auth/keys", tags=["API Keys (EGI Secret Store)"])


class VaultError(Exception):
    pass


# ── Vault client helpers ───────────────────────────────────────────────────────


async def get_vault_token(egi_access_token: str) -> str:
    """Exchange an EGI Check-in access token for a Vault client token via JWT auth."""
    url = f"{settings.vault_url}/v1/auth/{settings.vault_jwt_mount}/login"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={"jwt": egi_access_token, "role": settings.vault_jwt_role})
        if resp.status_code != 200:
            logger.warning(f"Vault JWT auth failed ({resp.status_code}): {resp.text}")
            raise VaultError(f"Vault authentication failed: {resp.status_code}")
        return resp.json()["auth"]["client_token"]


def _secret_path(user_sub: str, key_id: str | None = None) -> str:
    """Build the Vault REST path for a user's API key secret.

    KV v2 uses /data/ for read/write and /metadata/ for list/delete.
    KV v1 uses the path directly.
    """
    sub_enc = quote(user_sub, safe="")
    base = f"{settings.vault_kv_mount}/{sub_enc}/api-keys"
    if key_id:
        base = f"{base}/{quote(key_id, safe='')}"
    return base


def _kv_data_path(user_sub: str, key_id: str) -> str:
    if settings.vault_kv_version == 2:
        sub_enc = quote(user_sub, safe="")
        return f"{settings.vault_kv_mount}/data/{sub_enc}/api-keys/{quote(key_id, safe='')}"
    return _secret_path(user_sub, key_id)


def _kv_list_path(user_sub: str) -> str:
    if settings.vault_kv_version == 2:
        sub_enc = quote(user_sub, safe="")
        return f"{settings.vault_kv_mount}/metadata/{sub_enc}/api-keys"
    sub_enc = quote(user_sub, safe="")
    return f"{settings.vault_kv_mount}/{sub_enc}/api-keys"


def _kv_delete_path(user_sub: str, key_id: str) -> str:
    if settings.vault_kv_version == 2:
        sub_enc = quote(user_sub, safe="")
        return f"{settings.vault_kv_mount}/metadata/{sub_enc}/api-keys/{quote(key_id, safe='')}"
    return _secret_path(user_sub, key_id)


async def vault_save_api_key(egi_access_token: str, user_sub: str, key_id: str, key_value: str) -> None:
    """Write or overwrite a named API key in the user's Vault namespace."""
    vault_token = await get_vault_token(egi_access_token)
    path = _kv_data_path(user_sub, key_id)
    body: dict[str, dict[str, str]] | dict[str, str] = (
        {"data": {"value": key_value}} if settings.vault_kv_version == 2 else {"value": key_value}
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.vault_url}/v1/{path}",
            json=body,
            headers={"X-Vault-Token": vault_token},
        )
        if resp.status_code not in (200, 204):
            raise VaultError(f"Failed to save secret ({resp.status_code}): {resp.text}")


async def vault_get_api_key(egi_access_token: str, user_sub: str, key_id: str) -> str | None:
    """Read a named API key from the user's Vault namespace. Returns None if not found."""
    vault_token = await get_vault_token(egi_access_token)
    path = _kv_data_path(user_sub, key_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.vault_url}/v1/{path}",
            headers={"X-Vault-Token": vault_token},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise VaultError(f"Failed to read secret ({resp.status_code}): {resp.text}")
        payload = resp.json()
        # KV v2 wraps data under data.data; KV v1 puts fields directly under data
        secret_data = payload["data"]["data"] if settings.vault_kv_version == 2 else payload["data"]
        return secret_data.get("value")


async def vault_list_api_key_ids(egi_access_token: str, user_sub: str) -> list[str]:
    """List the IDs of all API keys stored for the user. Returns [] if none exist."""
    vault_token = await get_vault_token(egi_access_token)
    path = _kv_list_path(user_sub)
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            "LIST",
            f"{settings.vault_url}/v1/{path}",
            headers={"X-Vault-Token": vault_token},
        )
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise VaultError(f"Failed to list secrets ({resp.status_code}): {resp.text}")
        return resp.json()["data"]["keys"]


async def vault_get_api_keys(
    egi_access_token: str,
    user_sub: str,
    key_ids: list[str] | None = None,
) -> dict[str, str]:
    """Fetch multiple API keys at once. Pass key_ids=None to retrieve all keys."""
    if key_ids is None:
        key_ids = await vault_list_api_key_ids(egi_access_token, user_sub)
    result: dict[str, str] = {}
    for kid in key_ids:
        value = await vault_get_api_key(egi_access_token, user_sub, kid)
        if value is not None:
            result[kid] = value
    return result


async def vault_delete_api_key(egi_access_token: str, user_sub: str, key_id: str) -> None:
    """Permanently delete a named API key from the user's Vault namespace."""
    vault_token = await get_vault_token(egi_access_token)
    path = _kv_delete_path(user_sub, key_id)
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{settings.vault_url}/v1/{path}",
            headers={"X-Vault-Token": vault_token},
        )
        if resp.status_code not in (200, 204, 404):
            raise VaultError(f"Failed to delete secret ({resp.status_code}): {resp.text}")


# ── FastAPI dependency ─────────────────────────────────────────────────────────


async def require_vault_auth(
    request: Request,
    user: UserInfo = Depends(require_auth),
) -> tuple[UserInfo, str]:
    """Dependency: authenticated user + their EGI access token (needed for Vault auth)."""
    access_token: str | None = getattr(request.state, "access_token", None)
    if not access_token:
        raise HTTPException(status_code=401, detail="EGI access token not available for Vault authentication")
    return user, access_token


def _vault_http_error(exc: VaultError) -> HTTPException:
    msg = str(exc)
    if "401" in msg or "403" in msg:
        return HTTPException(status_code=403, detail=f"Vault access denied: {msg}")
    if "404" in msg:
        return HTTPException(status_code=404, detail=msg)
    return HTTPException(status_code=502, detail=f"EGI Secret Store error: {msg}")


# ── API models ─────────────────────────────────────────────────────────────────


class ApiKeyIn(BaseModel):
    key_value: str


class ApiKeyOut(BaseModel):
    key_id: str
    key_value: str


class ApiKeyListOut(BaseModel):
    key_ids: list[str]


class ApiKeysOut(BaseModel):
    keys: dict[str, str]


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.put("/{key_id}", summary="Save or update an API key in EGI Secret Store")
async def put_api_key(
    key_id: str,
    body: ApiKeyIn,
    auth: tuple[UserInfo, str] = Depends(require_vault_auth),
) -> dict[str, str]:
    user, access_token = auth
    try:
        await vault_save_api_key(access_token, user.sub, key_id, body.key_value)
    except VaultError as exc:
        raise _vault_http_error(exc) from exc
    return {"status": "saved", "key_id": key_id}


@router.get("", summary="List API key IDs stored in EGI Secret Store")
async def list_api_keys(
    auth: tuple[UserInfo, str] = Depends(require_vault_auth),
) -> ApiKeyListOut:
    user, access_token = auth
    try:
        key_ids = await vault_list_api_key_ids(access_token, user.sub)
    except VaultError as exc:
        raise _vault_http_error(exc) from exc
    return ApiKeyListOut(key_ids=key_ids)


@router.get("/all", summary="Retrieve all API key values from EGI Secret Store")
async def get_all_api_keys(
    auth: tuple[UserInfo, str] = Depends(require_vault_auth),
) -> ApiKeysOut:
    user, access_token = auth
    try:
        keys = await vault_get_api_keys(access_token, user.sub)
    except VaultError as exc:
        raise _vault_http_error(exc) from exc
    return ApiKeysOut(keys=keys)


@router.get("/{key_id}", summary="Retrieve a specific API key value from EGI Secret Store")
async def get_api_key(
    key_id: str,
    auth: tuple[UserInfo, str] = Depends(require_vault_auth),
) -> ApiKeyOut:
    user, access_token = auth
    try:
        value = await vault_get_api_key(access_token, user.sub, key_id)
    except VaultError as exc:
        raise _vault_http_error(exc) from exc
    if value is None:
        raise HTTPException(status_code=404, detail=f"API key '{key_id}' not found")
    return ApiKeyOut(key_id=key_id, key_value=value)


@router.delete("/{key_id}", summary="Delete an API key from EGI Secret Store")
async def delete_api_key(
    key_id: str,
    auth: tuple[UserInfo, str] = Depends(require_vault_auth),
) -> dict[str, str]:
    user, access_token = auth
    try:
        await vault_delete_api_key(access_token, user.sub, key_id)
    except VaultError as exc:
        raise _vault_http_error(exc) from exc
    return {"status": "deleted", "key_id": key_id}
