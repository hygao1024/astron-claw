from fastapi import APIRouter, Cookie
from fastapi.responses import JSONResponse

from infra.log import logger
import services.state as state

router = APIRouter()


async def _require_admin(admin_session: str | None):
    if not await state.admin_auth.validate_session(admin_session):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


@router.get("/api/admin/tokens")
async def list_tokens(admin_session: str | None = Cookie(default=None)):
    denied = await _require_admin(admin_session)
    if denied:
        return denied
    tokens = await state.token_manager.list_all()
    connections = await state.bridge.get_connections_summary()
    result = []
    for t in tokens:
        conn = connections.get(t["token"], {})
        result.append({
            "token": t["token"],
            "name": t.get("name", ""),
            "created_at": t["created_at"],
            "expires_at": t["expires_at"],
            "bot_online": conn.get("bot_online", False),
            "chat_count": conn.get("chat_count", 0),
        })
    return {"tokens": result}


@router.post("/api/admin/tokens")
async def admin_create_token(body: dict = {}, admin_session: str | None = Cookie(default=None)):
    denied = await _require_admin(admin_session)
    if denied:
        return denied
    name = body.get("name", "")
    expires_in = body.get("expires_in", 86400)
    token = await state.token_manager.generate(name=name, expires_in=expires_in)
    logger.info("Admin created token: {}... (name={})", token[:16], name)
    return {"token": token}


@router.delete("/api/admin/tokens/{token_value}")
async def admin_delete_token(token_value: str, admin_session: str | None = Cookie(default=None)):
    denied = await _require_admin(admin_session)
    if denied:
        return denied
    await state.token_manager.remove(token_value)
    await state.bridge.remove_bot_sessions(token_value)
    logger.info("Admin deleted token: {}...", token_value[:16])
    return {"ok": True}


@router.patch("/api/admin/tokens/{token_value}")
async def admin_update_token(token_value: str, body: dict, admin_session: str | None = Cookie(default=None)):
    denied = await _require_admin(admin_session)
    if denied:
        return denied
    name = body.get("name")
    expires_in = body.get("expires_in")
    if not await state.token_manager.update(token_value, name=name, expires_in=expires_in):
        return JSONResponse({"error": "Token not found"}, status_code=404)
    logger.info("Admin updated token: {}...", token_value[:16])
    return {"ok": True}


@router.post("/api/admin/cleanup")
async def admin_cleanup(admin_session: str | None = Cookie(default=None)):
    denied = await _require_admin(admin_session)
    if denied:
        return denied
    token_count = await state.token_manager.cleanup_expired()
    media_count = await state.media_manager.cleanup_expired()
    logger.info("Admin cleanup: removed {} tokens, {} media files", token_count, media_count)
    return {"removed_tokens": token_count, "removed_media": media_count}
