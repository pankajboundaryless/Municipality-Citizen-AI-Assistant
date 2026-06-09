import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from authlib.integrations.starlette_client import OAuth, OAuthError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth")

oauth = OAuth()

# Register Microsoft if credentials are present
_ms_client_id = os.getenv("MICROSOFT_CLIENT_ID")
_ms_tenant = os.getenv("MICROSOFT_TENANT_ID", "common")
if _ms_client_id:
    oauth.register(
        name="microsoft",
        client_id=_ms_client_id,
        client_secret=os.getenv("MICROSOFT_CLIENT_SECRET"),
        server_metadata_url=(
            f"https://login.microsoftonline.com/{_ms_tenant}/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )

# Register Google if credentials are present
_google_client_id = os.getenv("GOOGLE_CLIENT_ID")
if _google_client_id:
    oauth.register(
        name="google",
        client_id=_google_client_id,
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def _enabled_providers() -> list[str]:
    providers = []
    if os.getenv("MICROSOFT_CLIENT_ID"):
        providers.append("microsoft")
    if os.getenv("GOOGLE_CLIENT_ID"):
        providers.append("google")
    return providers


def _oauth_enabled() -> bool:
    return bool(_enabled_providers())


def get_current_user(request: Request) -> dict | None:
    if not _oauth_enabled():
        return None
    return request.session.get("user")


def require_auth(request: Request) -> dict | None:
    user = get_current_user(request)
    if _oauth_enabled() and not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


@router.get("/login/{provider}")
async def login(provider: str, request: Request):
    if provider not in ("microsoft", "google"):
        return RedirectResponse(url="/?auth_error=1")
    if provider not in _enabled_providers():
        return RedirectResponse(url="/?auth_error=1")
    redirect_uri = str(request.url_for("auth_callback", provider=provider))
    # Azure terminates SSL so internal requests appear as http — force https in prod
    if os.getenv("SESSION_HTTPS_ONLY", "false").lower() == "true":
        redirect_uri = redirect_uri.replace("http://", "https://", 1)
    client = getattr(oauth, provider)
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/callback/{provider}", name="auth_callback")
async def auth_callback(provider: str, request: Request):
    if provider not in _enabled_providers():
        return RedirectResponse(url="/?auth_error=1")
    client = getattr(oauth, provider)
    # Microsoft multi-tenant /common endpoint returns a tenant-specific iss —
    # skip issuer validation since it won't match the generic discovery doc.
    claims_opts = {"iss": {"essential": False}} if provider == "microsoft" else {}
    try:
        token = await client.authorize_access_token(request, claims_options=claims_opts)
    except OAuthError as exc:
        logger.error(f"OAuth callback error ({provider}): {exc}")
        return RedirectResponse(url="/?auth_error=1")

    user_info = token.get("userinfo")
    if not user_info:
        user_info = await client.userinfo(token=token)

    request.session["user"] = {
        "name": user_info.get("name") or user_info.get("displayName", ""),
        "email": (
            user_info.get("email")
            or user_info.get("mail")
            or user_info.get("preferred_username", "")
        ),
        "sub": str(user_info.get("sub") or user_info.get("oid", "")),
        "picture": user_info.get("picture", ""),
        "provider": provider,
    }
    logger.info(f"User authenticated: {request.session['user']['email']} via {provider}")
    return RedirectResponse(url="/")


@router.get("/me")
async def me(request: Request):
    user = request.session.get("user")
    enabled = _oauth_enabled()
    return JSONResponse({
        "authenticated": bool(user) or not enabled,
        "oauth_enabled": enabled,
        "providers": _enabled_providers(),
        "user": user,
    })


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
