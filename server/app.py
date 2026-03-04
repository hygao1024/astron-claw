from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from infra.log import logger
from infra.config import load_config
from infra.database import init_db, get_session_factory, close_db
from infra.cache import init_redis, close_redis

from services.token_manager import TokenManager
from services.bridge import ConnectionBridge
from services.admin_auth import AdminAuth
from services.media_manager import MediaManager
import services.state as state

from routers import health, tokens, admin_auth, admin, media, websocket


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()

    # Initialize MySQL
    await init_db(config.mysql)
    session_factory = get_session_factory()

    # Initialize Redis
    redis = await init_redis(config.redis)

    # Initialize managers and publish to shared state
    state.token_manager = TokenManager(session_factory)
    state.admin_auth = AdminAuth(session_factory, redis)
    state.media_manager = MediaManager(session_factory)
    state.bridge = ConnectionBridge(redis)
    state.bridge.set_media_manager(state.media_manager)
    await state.bridge.start()

    # Resolve frontend directory
    _server_dir = Path(__file__).resolve().parent
    _candidate = _server_dir.parent / "frontend"
    state.frontend_dir = _candidate if _candidate.is_dir() else _server_dir / "frontend"

    logger.info("Astron Claw Bridge Server started")
    yield

    # Shutdown — close connections + stop pub/sub before closing infrastructure
    await state.bridge.shutdown()
    await close_redis()
    await close_db()
    logger.info("Astron Claw Bridge Server stopped")


app = FastAPI(title="Astron Claw Bridge Server", lifespan=lifespan)

# ── Register routers ─────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(tokens.router)
app.include_router(admin_auth.router)
app.include_router(admin.router)
app.include_router(media.router)
app.include_router(websocket.router)

# ── Static assets (CSS, JS, etc.) ────────────────────────────────────────────
_server_dir = Path(__file__).resolve().parent
_candidate = _server_dir.parent / "frontend"
_frontend_dir = _candidate if _candidate.is_dir() else _server_dir / "frontend"

if _frontend_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")
