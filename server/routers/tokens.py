from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import services.state as state

router = APIRouter()


@router.post("/api/token")
async def create_token():
    token = await state.token_manager.generate()
    return {"token": token}


@router.post("/api/token/validate")
async def validate_token(body: dict):
    token = body.get("token", "")
    valid = await state.token_manager.validate(token)
    return {
        "valid": valid,
        "bot_connected": await state.bridge.is_bot_connected(token) if valid else False,
    }


@router.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = state.frontend_dir / "index.html"
    if index_file.is_file():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Astron Claw</h1><p>Frontend not found.</p>")


@router.get("/admin", response_class=HTMLResponse)
async def serve_admin():
    admin_file = state.frontend_dir / "admin.html"
    if admin_file.is_file():
        return HTMLResponse(content=admin_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Admin</h1><p>Admin page not found.</p>")
