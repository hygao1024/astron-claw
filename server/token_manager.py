import secrets
import time
from typing import Optional


class TokenManager:
    """In-memory token management with sk- prefix and optional expiry."""

    def __init__(self, expiry_seconds: int = 86400):
        self._tokens: dict[str, float] = {}  # token -> creation_time
        self._expiry_seconds = expiry_seconds

    def generate(self) -> str:
        token = "sk-" + secrets.token_hex(24)
        self._tokens[token] = time.time()
        return token

    def validate(self, token: Optional[str]) -> bool:
        if not token or token not in self._tokens:
            return False
        created_at = self._tokens[token]
        if time.time() - created_at > self._expiry_seconds:
            del self._tokens[token]
            return False
        return True

    def remove(self, token: str) -> None:
        self._tokens.pop(token, None)
