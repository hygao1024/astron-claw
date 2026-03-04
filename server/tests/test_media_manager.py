"""Tests for services/media_manager.py — MediaManager."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from services.media_manager import MediaManager, DEFAULT_MEDIA_DIR, MAX_FILE_SIZE


class TestIsMimeAllowed:
    @pytest.mark.parametrize("mime", [
        "image/png", "image/jpeg", "audio/mp3", "audio/wav",
        "video/mp4", "application/pdf", "text/plain", "text/csv",
        "application/zip", "application/octet-stream",
    ])
    def test_is_mime_allowed_valid(self, mime, mock_session_factory, tmp_path):
        mm = MediaManager(mock_session_factory, media_dir=tmp_path)
        assert mm._is_mime_allowed(mime) is True

    @pytest.mark.parametrize("mime", [
        "application/javascript", "font/woff2", "",
    ])
    def test_is_mime_allowed_invalid(self, mime, mock_session_factory, tmp_path):
        mm = MediaManager(mock_session_factory, media_dir=tmp_path)
        assert mm._is_mime_allowed(mime) is False


class TestStore:
    async def test_store_too_large(self, mock_session_factory, tmp_path):
        mm = MediaManager(mock_session_factory, media_dir=tmp_path)
        data = b"x" * (MAX_FILE_SIZE + 1)
        result = await mm.store(data, "big.bin", "application/octet-stream", "tok")
        assert result is None

    async def test_store_empty_file(self, mock_session_factory, tmp_path):
        mm = MediaManager(mock_session_factory, media_dir=tmp_path)
        result = await mm.store(b"", "empty.txt", "text/plain", "tok")
        assert result is None

    async def test_store_bad_mime(self, mock_session_factory, tmp_path):
        mm = MediaManager(mock_session_factory, media_dir=tmp_path)
        result = await mm.store(b"data", "script.js", "application/javascript", "tok")
        assert result is None

    async def test_store_path_traversal(self, mock_session_factory, tmp_path):
        mm = MediaManager(mock_session_factory, media_dir=tmp_path)
        result = await mm.store(b"data", "../../etc/passwd", "text/plain", "tok")
        assert result is not None
        assert result["fileName"] == "passwd"
        # File should be written inside the media dir, not elsewhere
        files = list(tmp_path.iterdir())
        assert len(files) == 1

    async def test_store_success(self, mock_session_factory, tmp_path):
        mm = MediaManager(mock_session_factory, media_dir=tmp_path)
        data = b"PNG file content"
        result = await mm.store(data, "photo.png", "image/png", "sk-tok123")

        assert result is not None
        assert result["fileName"] == "photo.png"
        assert result["mimeType"] == "image/png"
        assert result["fileSize"] == len(data)
        assert result["mediaId"].startswith("media_")

        # Verify file written to disk
        stored_files = list(tmp_path.iterdir())
        assert len(stored_files) == 1
        assert stored_files[0].read_bytes() == data


class TestDefaultMediaDir:
    def test_default_media_dir(self):
        expected = Path(__file__).resolve().parent.parent / "media"
        assert DEFAULT_MEDIA_DIR == expected
