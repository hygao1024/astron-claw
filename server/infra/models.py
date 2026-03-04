from sqlalchemy import String, Text, Double, BigInteger, Integer, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Token(Base):
    """API 访问令牌"""

    __tablename__ = "tokens"
    __table_args__ = (
        Index("idx_tokens_expires_at", "expires_at"),
        Index("uk_tokens_token", "token", unique=True),
        {"comment": "API 访问令牌表，管理 bot 和 chat 客户端的接入凭证"},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
        comment="自增主键",
    )
    token: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="令牌值，sk- 前缀 + 24字节hex，全局唯一标识",
    )
    created_at: Mapped[float] = mapped_column(
        Double, nullable=False,
        comment="创建时间，Unix 时间戳（秒）",
    )
    name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="",
        comment="令牌名称，便于管理员识别用途",
    )
    expires_at: Mapped[float] = mapped_column(
        Double, nullable=False,
        comment="过期时间，Unix 时间戳（秒），9999999999.0 表示永不过期",
    )


class AdminConfig(Base):
    """管理后台配置"""

    __tablename__ = "admin_config"
    __table_args__ = (
        Index("uk_admin_config_key", "key", unique=True),
        {"comment": "管理后台配置表，存储管理员密码哈希等系统配置"},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
        comment="自增主键",
    )
    key: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="配置键名，如 password_salt、password_hash",
    )
    value: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="配置值，存储对应键的具体内容",
    )


class Media(Base):
    """媒体文件元数据"""

    __tablename__ = "media"
    __table_args__ = (
        Index("idx_media_expires_at", "expires_at"),
        Index("idx_media_uploaded_by", "uploaded_by"),
        Index("uk_media_media_id", "media_id", unique=True),
        {"comment": "媒体文件元数据表，记录上传文件的信息，实际文件存储在本地文件系统"},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
        comment="自增主键",
    )
    media_id: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="媒体文件唯一标识，media_ 前缀 + UUID hex",
    )
    file_name: Mapped[str] = mapped_column(
        String(255), nullable=False,
        comment="原始文件名（已做路径遍历安全处理）",
    )
    mime_type: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="MIME 类型，如 image/png、application/pdf",
    )
    file_size: Mapped[int] = mapped_column(
        BigInteger, nullable=False,
        comment="文件大小（字节），最大 50MB",
    )
    uploaded_by: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="上传者的令牌值，关联 tokens.token",
    )
    uploaded_at: Mapped[float] = mapped_column(
        Double, nullable=False,
        comment="上传时间，Unix 时间戳（秒）",
    )
    expires_at: Mapped[float] = mapped_column(
        Double, nullable=False,
        comment="过期时间，Unix 时间戳（秒），默认上传后 7 天",
    )
