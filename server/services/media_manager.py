import time
import uuid
from pathlib import Path
from typing import Optional

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.models import Media
from infra.log import logger

DEFAULT_MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MEDIA_EXPIRY_SECONDS = 7 * 24 * 3600  # 7 days

ALLOWED_MIME_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "application/pdf",
    "application/zip",
    "application/octet-stream",
    "text/",
)


class MediaManager:
    """Manages file upload, download, and metadata storage for media."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        media_dir: Path = DEFAULT_MEDIA_DIR,
    ):
        self._session = session_factory
        self._media_dir = media_dir
        self._media_dir.mkdir(parents=True, exist_ok=True)

    async def store(
        self,
        file_data: bytes,
        file_name: str,
        mime_type: str,
        uploaded_by: str,
    ) -> Optional[dict]:
        file_size = len(file_data)
        if file_size > MAX_FILE_SIZE:
            logger.warning("Media rejected: file too large ({} bytes, max={})", file_size, MAX_FILE_SIZE)
            return None
        if file_size == 0:
            logger.warning("Media rejected: empty file (name={})", file_name)
            return None
        if not self._is_mime_allowed(mime_type):
            logger.warning("Media rejected: unsupported MIME type '{}' (name={})", mime_type, file_name)
            return None

        media_id = f"media_{uuid.uuid4().hex}"
        now = time.time()
        expires_at = now + MEDIA_EXPIRY_SECONDS

        safe_name = Path(file_name).name
        if not safe_name:
            safe_name = "unnamed"

        ext = Path(safe_name).suffix
        stored_name = media_id + ext
        file_path = self._media_dir / stored_name
        file_path.write_bytes(file_data)

        async with self._session() as session:
            session.add(Media(
                media_id=media_id,
                file_name=safe_name,
                mime_type=mime_type,
                file_size=file_size,
                uploaded_by=uploaded_by,
                uploaded_at=now,
                expires_at=expires_at,
            ))
            await session.commit()

        logger.info(
            "Stored media {} ({}, {} bytes) by {}...",
            media_id, mime_type, file_size, uploaded_by[:10],
        )
        return {
            "mediaId": media_id,
            "fileName": safe_name,
            "mimeType": mime_type,
            "fileSize": file_size,
        }

    async def get_metadata(self, media_id: str) -> Optional[dict]:
        async with self._session() as session:
            result = await session.execute(
                select(Media).where(
                    Media.media_id == media_id,
                    Media.expires_at >= time.time(),
                )
            )
            row = result.scalar_one_or_none()

        if not row:
            return None
        return {
            "mediaId": row.media_id,
            "fileName": row.file_name,
            "mimeType": row.mime_type,
            "fileSize": row.file_size,
            "uploadedBy": row.uploaded_by,
            "uploadedAt": row.uploaded_at,
            "expiresAt": row.expires_at,
        }

    async def get_file_path(self, media_id: str) -> Optional[Path]:
        meta = await self.get_metadata(media_id)
        if not meta:
            return None
        ext = Path(meta["fileName"]).suffix
        stored_name = media_id + ext
        file_path = self._media_dir / stored_name
        if not file_path.is_file():
            logger.error("Media file missing on disk: {} (path={})", media_id, file_path)
            return None
        return file_path

    async def cleanup_expired(self) -> int:
        now = time.time()
        async with self._session() as session:
            result = await session.execute(
                select(Media.media_id, Media.file_name).where(Media.expires_at < now)
            )
            rows = result.all()

            count = 0
            for media_id, file_name in rows:
                ext = Path(file_name).suffix
                stored_name = media_id + ext
                file_path = self._media_dir / stored_name
                try:
                    if file_path.is_file():
                        file_path.unlink()
                except OSError:
                    logger.warning("Failed to delete media file: {}", file_path)
                count += 1

            if rows:
                await session.execute(
                    delete(Media).where(Media.expires_at < now)
                )
                await session.commit()
                logger.info("Cleaned up {} expired media files", count)

        return count

    def _is_mime_allowed(self, mime_type: str) -> bool:
        if not mime_type:
            return False
        for prefix in ALLOWED_MIME_PREFIXES:
            if mime_type.startswith(prefix):
                return True
        return False
