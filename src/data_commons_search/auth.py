"""Authentication endpoints and utilities for OIDC integration."""

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends
from fastapi.exceptions import HTTPException
from fastapi.requests import Request
from fastapi.responses import RedirectResponse
from fastapi.security import OpenIdConnect

from data_commons_search.config import settings
from data_commons_search.utils import logger

# OIDC Configuration cache
_oidc_config_cache: dict[str, dict[str, Any]] = {}

router = APIRouter(tags=["Authentication"])

# OpenID Connect scheme for OpenAPI docs
oidc_scheme = OpenIdConnect(
    openIdConnectUrl=settings.oidc_config_url,
    auto_error=False,
)


async def get_oidc_config() -> dict[str, Any]:
    """Fetch and cache OIDC configuration from the provider."""
    if "config" not in _oidc_config_cache:
        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.oidc_config_url)
            resp.raise_for_status()
            _oidc_config_cache["config"] = resp.json()
    return _oidc_config_cache["config"]


async def get_current_user(request: Request) -> dict[str, Any] | None:
    """Extract current user from access token cookie.

    Returns user info dict if authenticated, None otherwise.
    """
    access_token = request.cookies.get("access_token")
    if not access_token:
        return None
    try:
        oidc_config = await get_oidc_config()
        userinfo_endpoint = oidc_config["userinfo_endpoint"]
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 200:
                return resp.json()
            return None
    except Exception as e:
        logger.warning(f"Failed to get user info: {e}")
        return None


async def require_auth(
    request: Request,
    token: str | None = Depends(oidc_scheme),
) -> dict[str, Any]:
    """Dependency that requires authentication.

    Raises HTTPException if not authenticated.
    This will show a padlock icon in Swagger UI for protected endpoints.
    """
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def optional_auth(
    request: Request,
    token: str | None = Depends(oidc_scheme),
) -> dict[str, Any] | None:
    """Dependency that optionally extracts user info.

    Returns user info if authenticated, None otherwise.
    This will show a padlock icon in Swagger UI for endpoints with optional auth.
    """
    return await get_current_user(request)


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
        "scope": "openid email profile",
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

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)

    # Redirect to home page with tokens stored in HttpOnly cookies
    response = RedirectResponse(url="/")
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=expires_in,
    )

    if refresh_token:
        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=30 * 24 * 3600,  # 30 days
        )

    # Clear the state and PKCE cookies
    response.delete_cookie(key="oauth_state")
    response.delete_cookie(key="pkce_verifier")
    logger.info(
        f"User authenticated successfully via OIDC. Access token: {access_token} \nRefresh token: {refresh_token}"
    )
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
async def auth_user(user: dict[str, Any] | None = Depends(optional_auth)) -> dict[str, Any]:
    """Get current authenticated user info."""
    if user is None:
        return {"authenticated": False}
    return {"authenticated": True, "user": user}
