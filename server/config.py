import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the server directory
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)


@dataclass(frozen=True)
class MysqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str

    @property
    def url(self) -> str:
        from urllib.parse import quote_plus
        pwd = quote_plus(self.password)
        return (
            f"mysql+aiomysql://{self.user}:{pwd}"
            f"@{self.host}:{self.port}/{self.database}?charset=utf8mb4"
        )


@dataclass(frozen=True)
class RedisConfig:
    host: str
    port: int
    password: str
    db: int
    cluster: bool


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    workers: int
    log_level: str
    access_log: bool


@dataclass(frozen=True)
class AppConfig:
    mysql: MysqlConfig
    redis: RedisConfig
    server: ServerConfig


def load_config() -> AppConfig:
    return AppConfig(
        mysql=MysqlConfig(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", "astron_claw"),
        ),
        redis=RedisConfig(
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD", ""),
            db=int(os.getenv("REDIS_DB", "0")),
            cluster=os.getenv("REDIS_CLUSTER", "false").lower() == "true",
        ),
        server=ServerConfig(
            host=os.getenv("SERVER_HOST", "0.0.0.0"),
            port=int(os.getenv("SERVER_PORT", "8765")),
            workers=int(os.getenv("SERVER_WORKERS", str((os.cpu_count() or 1) + 1))),
            log_level=os.getenv("SERVER_LOG_LEVEL", "info"),
            access_log=os.getenv("SERVER_ACCESS_LOG", "true").lower() == "true",
        ),
    )
