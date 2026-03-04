"""Tests for infra/config.py — load_config() and URL encoding."""

import os
from unittest.mock import patch

from infra.config import load_config, MysqlConfig

_CONFIG_KEYS = [
    "MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE",
    "REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD", "REDIS_DB", "REDIS_CLUSTER",
    "SERVER_HOST", "SERVER_PORT", "SERVER_WORKERS", "SERVER_LOG_LEVEL", "SERVER_ACCESS_LOG",
]


def _clean_env():
    """Return a copy of os.environ with all config keys removed."""
    return {k: v for k, v in os.environ.items() if k not in _CONFIG_KEYS}


class TestLoadConfigDefaults:
    def test_load_config_defaults(self):
        """All default values are applied when no env vars are set."""
        with patch.dict(os.environ, _clean_env(), clear=True):
            cfg = load_config()

        assert cfg.mysql.host == "127.0.0.1"
        assert cfg.mysql.port == 3306
        assert cfg.mysql.user == "root"
        assert cfg.mysql.password == ""
        assert cfg.mysql.database == "astron_claw"

        assert cfg.redis.host == "127.0.0.1"
        assert cfg.redis.port == 6379
        assert cfg.redis.password == ""
        assert cfg.redis.db == 0
        assert cfg.redis.cluster is False

        assert cfg.server.host == "0.0.0.0"
        assert cfg.server.port == 8765
        assert cfg.server.log_level == "info"
        assert cfg.server.access_log is True

    def test_load_config_custom_env(self):
        """All env vars are picked up and parsed."""
        env = {
            **_clean_env(),
            "MYSQL_HOST": "db.example.com",
            "MYSQL_PORT": "3307",
            "MYSQL_USER": "admin",
            "MYSQL_PASSWORD": "secret",
            "MYSQL_DATABASE": "mydb",
            "REDIS_HOST": "redis.example.com",
            "REDIS_PORT": "6380",
            "REDIS_PASSWORD": "redispw",
            "REDIS_DB": "5",
            "REDIS_CLUSTER": "true",
            "SERVER_HOST": "127.0.0.1",
            "SERVER_PORT": "9000",
            "SERVER_WORKERS": "4",
            "SERVER_LOG_LEVEL": "debug",
            "SERVER_ACCESS_LOG": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()

        assert cfg.mysql.host == "db.example.com"
        assert cfg.mysql.port == 3307
        assert cfg.mysql.user == "admin"
        assert cfg.mysql.password == "secret"
        assert cfg.mysql.database == "mydb"

        assert cfg.redis.host == "redis.example.com"
        assert cfg.redis.port == 6380
        assert cfg.redis.password == "redispw"
        assert cfg.redis.db == 5
        assert cfg.redis.cluster is True

        assert cfg.server.host == "127.0.0.1"
        assert cfg.server.port == 9000
        assert cfg.server.workers == 4
        assert cfg.server.log_level == "debug"
        assert cfg.server.access_log is False


class TestMysqlUrl:
    def test_mysql_url_special_chars(self):
        """Passwords with @, #, / are properly URL-encoded."""
        from urllib.parse import quote_plus

        cfg = MysqlConfig(
            host="localhost", port=3306, user="root",
            password="p@ss#w/rd", database="testdb",
        )
        url = cfg.url
        encoded_pw = quote_plus("p@ss#w/rd")
        assert encoded_pw in url
        assert "@localhost:3306/testdb" in url
        assert url.startswith("mysql+aiomysql://")
        assert url.endswith("?charset=utf8mb4")


class TestBooleanParsing:
    def test_redis_cluster_flag_true(self):
        with patch.dict(os.environ, {**_clean_env(), "REDIS_CLUSTER": "true"}, clear=True):
            assert load_config().redis.cluster is True

    def test_redis_cluster_flag_false(self):
        with patch.dict(os.environ, {**_clean_env(), "REDIS_CLUSTER": "false"}, clear=True):
            assert load_config().redis.cluster is False

    def test_redis_cluster_flag_uppercase(self):
        with patch.dict(os.environ, {**_clean_env(), "REDIS_CLUSTER": "TRUE"}, clear=True):
            assert load_config().redis.cluster is True

    def test_server_access_log_false(self):
        with patch.dict(os.environ, {**_clean_env(), "SERVER_ACCESS_LOG": "false"}, clear=True):
            assert load_config().server.access_log is False
