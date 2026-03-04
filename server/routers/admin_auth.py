from fastapi import APIRouter, Cookie
from fastapi.responses import JSONResponse

from infra.log import logger
import services.state as state

router = APIRouter()


@router.get("/api/admin/auth/status")
async def admin_auth_status(admin_session: str | None = Cookie(default=None)):
    return {
        "need_setup": not await state.admin_auth.is_password_set(),
        "authenticated": await state.admin_auth.validate_session(admin_session),
    }


@router.post("/api/admin/auth/setup")
async def admin_auth_setup(body: dict):
    if await state.admin_auth.is_password_set():
        return JSONResponse({"error": "Password already set"}, status_code=400)
    password = body.get("password", "")
    if len(password) < 4:
        return JSONResponse({"error": "Password too short"}, status_code=400)
    await state.admin_auth.set_password(password)
    session = await state.admin_auth.create_session()
    logger.info("Admin password set up for the first time")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key="admin_session", value=session,
        httponly=True, path="/", samesite="lax", max_age=86400,
    )
    return resp


@router.post("/api/admin/auth/login")
async def admin_auth_login(body: dict):
    password = body.get("password", "")
    if not await state.admin_auth.verify_password(password):
        logger.warning("Admin login failed — wrong password")
        return JSONResponse({"error": "Wrong password"}, status_code=401)
    session = await state.admin_auth.create_session()
    logger.info("Admin logged in successfully")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key="admin_session", value=session,
        httponly=True, path="/", samesite="lax", max_age=86400,
    )
    return resp


@router.post("/api/admin/auth/logout")
async def admin_auth_logout(admin_session: str | None = Cookie(default=None)):
    await state.admin_auth.remove_session(admin_session)
    logger.info("Admin logged out")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key="admin_session", path="/")
    return resp
