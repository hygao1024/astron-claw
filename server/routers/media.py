from fastapi import APIRouter, Header, Query, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse

from infra.log import logger
from services.media_manager import MAX_FILE_SIZE
import services.state as state

router = APIRouter()


async def _validate_token_header(authorization: str | None) -> str | None:
    """Extract and validate token from Authorization header (Bearer scheme)."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1]
    else:
        token = authorization
    if await state.token_manager.validate(token):
        return token
    return None


@router.post("/api/media/upload")
async def upload_media(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    token = await _validate_token_header(authorization)
    if not token:
        return JSONResponse({"error": "Invalid or missing token"}, status_code=401)

    file_data = await file.read()

    if len(file_data) > MAX_FILE_SIZE:
        logger.warning("Media upload rejected: file too large ({} bytes)", len(file_data))
        return JSONResponse(
            {"error": f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)}MB"},
            status_code=413,
        )

    mime_type = file.content_type or "application/octet-stream"
    file_name = file.filename or "unnamed"

    result = await state.media_manager.store(file_data, file_name, mime_type, token)
    if not result:
        logger.warning("Media upload rejected: invalid file (name={}, mime={})", file_name, mime_type)
        return JSONResponse({"error": "Invalid file or unsupported type"}, status_code=400)

    result["downloadUrl"] = f"/api/media/download/{result['mediaId']}"
    return result


@router.get("/api/media/download/{media_id}")
async def download_media(
    media_id: str,
    authorization: str | None = Header(default=None),
    token: str = Query(default=""),
):
    auth_token = await _validate_token_header(authorization)
    if not auth_token and token:
        auth_token = token if await state.token_manager.validate(token) else None
    if not auth_token:
        return JSONResponse({"error": "Invalid or missing token"}, status_code=401)

    meta = await state.media_manager.get_metadata(media_id)
    if not meta:
        return JSONResponse({"error": "Media not found or expired"}, status_code=404)

    file_path = await state.media_manager.get_file_path(media_id)
    if not file_path:
        logger.error("Media file missing on disk: {}", media_id)
        return JSONResponse({"error": "Media file missing"}, status_code=404)

    return FileResponse(
        path=str(file_path),
        media_type=meta["mimeType"],
        filename=meta["fileName"],
    )
