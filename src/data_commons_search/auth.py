"""Authentication endpoints and utilities for OIDC integration."""

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Response
from fastapi.exceptions import HTTPException
from fastapi.requests import Request
from fastapi.responses import RedirectResponse
from fastapi.security import OpenIdConnect

from data_commons_search.config import settings
from data_commons_search.db import ensure_user_exists
from data_commons_search.models import UserInfo
from data_commons_search.utils import logger

# OIDC Configuration cache
_oidc_config_cache: dict[str, dict[str, Any]] = {}

router = APIRouter(tags=["Authentication"])

# OpenID Connect scheme for OpenAPI docs
oidc_scheme = OpenIdConnect(
    openIdConnectUrl=settings.oidc_config_url,
    auto_error=False,
)

DEFAULT_EXPIRY = 3600  # 1 hour
DEFAULT_REFRESH_EXPIRY = 2592000  # 30 * 24 * 3600 = 30 days


async def get_oidc_config() -> dict[str, Any]:
    """Fetch and cache OIDC configuration from the provider."""
    if "config" not in _oidc_config_cache:
        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.oidc_config_url)
            resp.raise_for_status()
            _oidc_config_cache["config"] = resp.json()
    return _oidc_config_cache["config"]


def _set_auth_cookies(
    response: Response,
    access_token: str,
    expires_in: int = DEFAULT_EXPIRY,
    refresh_token: str | None = None,
    refresh_expires_in: int = DEFAULT_REFRESH_EXPIRY,
) -> None:
    """Set auth cookies for access and optionally refresh tokens."""
    response.set_cookie(
        key="access_token",
        value=access_token,
        max_age=expires_in,
        httponly=True,
        secure=True,
        samesite="lax",
        # domain=".eosc-data-commons.eu",
    )
    if refresh_token:
        # Prefer explicit refresh token expiry from token response when available.
        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=refresh_expires_in,
        )


async def _fetch_userinfo(access_token: str) -> tuple[UserInfo | None, int | None]:
    """Fetch userinfo for the given access token.

    Returns:
        Tuple of (userinfo, status_code). userinfo is None when request fails.
    """
    oidc_config = await get_oidc_config()
    userinfo_endpoint = oidc_config["userinfo_endpoint"]
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code == 200:
            return UserInfo(**resp.json()), resp.status_code
        return None, resp.status_code


async def _refresh_access_token(refresh_token: str) -> dict[str, Any] | None:
    """Exchange refresh token for a new access token."""
    if not settings.oidc_client_id:
        return None

    oidc_config = await get_oidc_config()
    token_endpoint = oidc_config["token_endpoint"]
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": settings.oidc_client_id,
        "refresh_token": refresh_token,
    }
    if settings.oidc_client_secret:
        data["client_secret"] = settings.oidc_client_secret

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            logger.warning(f"Refresh token exchange failed: {resp.status_code} - {resp.text}")
            return None
        return resp.json()


async def get_current_user(request: Request, response: Response) -> UserInfo | None:
    """Extract current user from access token cookie.

    Returns user info dict if authenticated, None otherwise.
    """
    # # TODO: support Bearer token in Authorization header?
    # auth_header = request.headers.get("authorization")
    # if auth_header and auth_header.startswith("Bearer "):
    #     try:
    #         user, _ = await _fetch_userinfo(auth_header.split(" ", 1)[1].strip())
    #         return user
    #     except Exception:
    #         # If a header token is present but invalid, do not fall back to cookie refresh.
    #         return None

    # Fall back to cookie-based tokens (used by the browser/OIDC flow)
    access_token = request.cookies.get("access_token")
    refresh_token = request.cookies.get("refresh_token")
    if not access_token and not refresh_token:
        return None
    try:
        if access_token:
            user, status_code = await _fetch_userinfo(access_token)
            if user is not None:
                request.state.access_token = access_token
                return user
            # Token may be expired/invalid, try refresh flow below.
            if status_code not in (401, 403):
                return None
        if not refresh_token:
            return None

        refreshed_tokens = await _refresh_access_token(refresh_token)
        if not refreshed_tokens:
            response.delete_cookie(key="access_token")
            response.delete_cookie(key="refresh_token")
            return None

        new_access_token = refreshed_tokens.get("access_token")
        if not new_access_token:
            logger.warning("Refresh response missing access_token")
            response.delete_cookie(key="access_token")
            response.delete_cookie(key="refresh_token")
            return None

        new_refresh_token = refreshed_tokens.get("refresh_token")
        expires_in = refreshed_tokens.get("expires_in", DEFAULT_EXPIRY)
        refresh_expires_in = refreshed_tokens.get("refresh_expires_in", DEFAULT_REFRESH_EXPIRY)
        _set_auth_cookies(response, new_access_token, expires_in, new_refresh_token, refresh_expires_in)
        # Store new tokens in request state for endpoints that need to apply them to streaming responses
        request.state.pending_auth_tokens = {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "expires_in": expires_in,
            "refresh_expires_in": refresh_expires_in,
        }
        request.state.access_token = new_access_token
        user, _ = await _fetch_userinfo(new_access_token)
        return user
    except Exception as e:
        logger.warning(f"Failed to get user info: {e}")
        return None


def apply_pending_auth_cookies(request: Request, response: Response) -> None:
    """Apply refreshed auth cookies from request state to a custom response.

    This is needed for endpoints returning custom Response objects (e.g. StreamingResponse),
    where cookies set on injected `Response` in dependencies are not propagated automatically.
    """
    # TODO: we could make this a middleware so we don't have to call it manually
    pending_tokens = getattr(request.state, "pending_auth_tokens", None)
    if not pending_tokens:
        return
    _set_auth_cookies(
        response,
        pending_tokens["access_token"],
        pending_tokens["expires_in"],
        pending_tokens.get("refresh_token"),
        pending_tokens.get("refresh_expires_in"),
    )


async def require_auth(
    request: Request,
    response: Response,
    token: str | None = Depends(oidc_scheme),
) -> UserInfo:
    """Dependency that requires authentication.

    Raises HTTPException if not authenticated.
    This will show a padlock icon in Swagger UI for protected endpoints.
    """
    user = await get_current_user(request, response)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def optional_auth(
    request: Request,
    response: Response,
    token: str | None = Depends(oidc_scheme),
) -> UserInfo | None:
    """Dependency that optionally extracts user info.

    Returns user info if authenticated, None otherwise.
    This will show a padlock icon in Swagger UI for endpoints with optional auth.
    """
    return await get_current_user(request, response)


def generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge.

    Returns:
        Tuple of (code_verifier, code_challenge)
    """
    # Generate code_verifier: 43-128 characters using unreserved characters
    # Using 32 bytes gives us 43 characters when base64url encoded
    code_verifier = secrets.token_urlsafe(32)
    # Generate code_challenge: BASE64URL(SHA256(code_verifier))
    code_challenge_digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(code_challenge_digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


@router.get("/auth/login")
async def auth_login(request: Request) -> RedirectResponse:
    """Redirect to OpenID Connect provider for authentication."""
    if not settings.oidc_client_id:
        raise HTTPException(status_code=500, detail="OpenID Connect not configured")

    oidc_config = await get_oidc_config()
    auth_endpoint = oidc_config["authorization_endpoint"]
    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)
    # Generate PKCE code_verifier and code_challenge
    code_verifier, code_challenge = generate_pkce_pair()

    params = {
        "client_id": settings.oidc_client_id,
        "response_type": "code",
        "scope": "openid email profile voperson_id",  # TODO: offline_access
        "redirect_uri": str(request.base_url).rstrip("/") + "/auth/callback",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{auth_endpoint}?{urlencode(params)}"

    response = RedirectResponse(url=auth_url)
    # Store state in cookie for CSRF validation
    response.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=True,
        samesite="lax",
        # samesite="none",
        max_age=600,  # 10min
    )
    # Store code_verifier in cookie for PKCE validation
    response.set_cookie(
        key="pkce_verifier",
        value=code_verifier,
        httponly=True,
        secure=True,
        samesite="lax",
        # samesite="none",
        max_age=600,
    )
    return response


@router.get("/auth/callback")
async def auth_callback(request: Request, state: str, code: str | None = None) -> RedirectResponse:
    """Handle OpenID Connect callback and exchange code for tokens."""
    # Validate state for CSRF protection
    stored_state = request.cookies.get("oauth_state")
    if not stored_state or stored_state != state:
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    # Retrieve PKCE code_verifier
    code_verifier = request.cookies.get("pkce_verifier")
    if not code_verifier:
        raise HTTPException(status_code=400, detail="Missing PKCE code verifier")

    oidc_config = await get_oidc_config()
    token_endpoint = oidc_config["token_endpoint"]

    # Exchange code for tokens with PKCE code_verifier
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
                "code": code,
                "redirect_uri": str(request.base_url) + "auth/callback",
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            logger.error(f"Token exchange failed: {resp.text}")
            raise HTTPException(status_code=400, detail="Token exchange failed")
        tokens = resp.json()

    # Redirect to home page with tokens stored in HttpOnly cookies
    response = RedirectResponse(url="/")
    _set_auth_cookies(
        response,
        tokens.get("access_token"),
        tokens.get("expires_in", DEFAULT_EXPIRY),
        tokens.get("refresh_token"),
        tokens.get("refresh_expires_in", DEFAULT_REFRESH_EXPIRY),
    )

    # Clear the state and PKCE cookies
    response.delete_cookie(key="oauth_state")
    response.delete_cookie(key="pkce_verifier")
    logger.info(tokens)
    logger.info(f"User authenticated successfully via OIDC.\nRefresh token: {tokens.get('refresh_token')}")
    logger.info(f"export ACCESS_TOKEN={tokens.get('access_token')}")
    # logger.info(await _fetch_userinfo(tokens.get("access_token")))
    user, _ = await _fetch_userinfo(tokens.get("access_token"))
    if user is not None:
        ensure_user_exists(user)
    return response


@router.get("/auth/logout")
async def auth_logout(request: Request) -> RedirectResponse:
    """Logout user and clear cookies."""
    response = RedirectResponse(url="/")
    response.delete_cookie(key="access_token")
    response.delete_cookie(key="refresh_token")
    # Optionally redirect to OIDC end session endpoint
    if settings.oidc_client_id:
        try:
            oidc_config = await get_oidc_config()
            end_session_endpoint = oidc_config.get("end_session_endpoint")
            if end_session_endpoint:
                post_logout_redirect = str(request.base_url)
                logout_url = f"{end_session_endpoint}?{urlencode({'post_logout_redirect_uri': post_logout_redirect, 'client_id': settings.oidc_client_id})}"
                response = RedirectResponse(url=logout_url)
                response.delete_cookie(key="access_token")
                response.delete_cookie(key="refresh_token")
        except Exception as e:
            logger.warning(f"Failed to get end session endpoint: {e}")
    return response


@router.get("/auth/user")
async def auth_user(user: UserInfo | None = Depends(optional_auth)) -> UserInfo:
    """Get current authenticated user info."""
    # print(user)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# def _extract_refresh_expires(token_response: dict[str, Any]) -> int | None:
#     """Try common fields for refresh-token expiry in token responses.

#     Returns the expiry in seconds if found, otherwise None.
#     """
#     if not token_response:
#         return None
#     candidates = [
#         "refresh_expires_in",
#         "refresh_token_expires_in",
#         "refresh_expires",
#         "refresh_token_expires",
#     ]
#     for key in candidates:
#         val = token_response.get(key)
#         if val is None:
#             continue
#         try:
#             return int(val)
#         except Exception:
#             try:
#                 return int(float(val))
#             except Exception:
#                 continue
#     return None
