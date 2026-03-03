import sqlite3
import time
import uuid
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "tokens.db"
DEFAULT_MEDIA_DIR = Path(__file__).resolve().parent / "media"
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
        db_path: Path = DEFAULT_DB_PATH,
        media_dir: Path = DEFAULT_MEDIA_DIR,
    ):
        self._media_dir = media_dir
        self._media_dir.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS media ("
            "  media_id TEXT PRIMARY KEY,"
            "  file_name TEXT NOT NULL,"
            "  mime_type TEXT NOT NULL,"
            "  file_size INTEGER NOT NULL,"
            "  uploaded_by TEXT NOT NULL,"
            "  uploaded_at REAL NOT NULL,"
            "  expires_at REAL NOT NULL"
            ")"
        )
        self._conn.commit()

    def store(
        self,
        file_data: bytes,
        file_name: str,
        mime_type: str,
        uploaded_by: str,
    ) -> Optional[dict]:
        """Store a file and return its metadata. Returns None on validation failure."""
        file_size = len(file_data)
        if file_size > MAX_FILE_SIZE:
            return None
        if file_size == 0:
            return None

        # Validate MIME type
        if not self._is_mime_allowed(mime_type):
            return None

        media_id = f"media_{uuid.uuid4().hex}"
        now = time.time()
        expires_at = now + MEDIA_EXPIRY_SECONDS

        # Sanitize file name to prevent path traversal
        safe_name = Path(file_name).name
        if not safe_name:
            safe_name = "unnamed"

        # Determine file extension
        ext = Path(safe_name).suffix
        stored_name = media_id + ext
        file_path = self._media_dir / stored_name

        file_path.write_bytes(file_data)

        self._conn.execute(
            "INSERT INTO media (media_id, file_name, mime_type, file_size, uploaded_by, uploaded_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (media_id, safe_name, mime_type, file_size, uploaded_by, now, expires_at),
        )
        self._conn.commit()

        logger.info("Stored media %s (%s, %d bytes) by %s", media_id, mime_type, file_size, uploaded_by[:10])

        return {
            "mediaId": media_id,
            "fileName": safe_name,
            "mimeType": mime_type,
            "fileSize": file_size,
        }

    def get_metadata(self, media_id: str) -> Optional[dict]:
        """Get metadata for a media item. Returns None if not found or expired."""
        row = self._conn.execute(
            "SELECT media_id, file_name, mime_type, file_size, uploaded_by, uploaded_at, expires_at "
            "FROM media WHERE media_id = ? AND expires_at >= ?",
            (media_id, time.time()),
        ).fetchone()
        if not row:
            return None
        return {
            "mediaId": row[0],
            "fileName": row[1],
            "mimeType": row[2],
            "fileSize": row[3],
            "uploadedBy": row[4],
            "uploadedAt": row[5],
            "expiresAt": row[6],
        }

    def get_file_path(self, media_id: str) -> Optional[Path]:
        """Get the file path for a media item. Returns None if not found."""
        meta = self.get_metadata(media_id)
        if not meta:
            return None

        ext = Path(meta["fileName"]).suffix
        stored_name = media_id + ext
        file_path = self._media_dir / stored_name
        if not file_path.is_file():
            return None
        return file_path

    def cleanup_expired(self) -> int:
        """Remove expired media files and their database entries. Returns count removed."""
        now = time.time()
        rows = self._conn.execute(
            "SELECT media_id, file_name FROM media WHERE expires_at < ?",
            (now,),
        ).fetchall()

        count = 0
        for media_id, file_name in rows:
            ext = Path(file_name).suffix
            stored_name = media_id + ext
            file_path = self._media_dir / stored_name
            try:
                if file_path.is_file():
                    file_path.unlink()
            except OSError:
                logger.warning("Failed to delete media file: %s", file_path)
            count += 1

        if rows:
            self._conn.execute("DELETE FROM media WHERE expires_at < ?", (now,))
            self._conn.commit()
            logger.info("Cleaned up %d expired media files", count)

        return count

    def _is_mime_allowed(self, mime_type: str) -> bool:
        """Check if a MIME type is in the allowed list."""
        if not mime_type:
            return False
        for prefix in ALLOWED_MIME_PREFIXES:
            if mime_type.startswith(prefix):
                return True
        return False
