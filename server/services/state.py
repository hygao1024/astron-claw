from pathlib import Path

from services.token_manager import TokenManager
from services.bridge import ConnectionBridge
from services.admin_auth import AdminAuth
from services.media_manager import MediaManager

token_manager: TokenManager
bridge: ConnectionBridge
admin_auth: AdminAuth
media_manager: MediaManager
frontend_dir: Path
