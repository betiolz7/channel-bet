from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import DB_URL

engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def utcnow() -> datetime:
    """UTC sem fuso (SQLite não guarda fuso; tudo no banco é UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # ID do canal no YouTube ou da conta no Instagram
    platform: Mapped[str] = mapped_column(String(16), default="youtube", server_default="youtube")
    title: Mapped[str] = mapped_column(String(200))
    thumbnail_url: Mapped[str | None] = mapped_column(String(500))
    uploads_playlist_id: Mapped[str | None] = mapped_column(String(64))
    credentials_json: Mapped[str] = mapped_column(Text)
    connected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChannelSnapshot(Base):
    __tablename__ = "channel_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    subscribers: Mapped[int] = mapped_column(Integer, default=0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    videos: Mapped[int] = mapped_column(Integer, default=0)


class Post(Base):
    """Um vídeo a ser publicado em um canal.

    status: draft -> queued -> uploading -> uploaded | failed
    """

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id"), index=True)
    title: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(Text, default="")  # separadas por vírgula
    privacy: Mapped[str] = mapped_column(String(16), default="private")
    publish_at: Mapped[datetime | None] = mapped_column(DateTime)  # UTC
    file_path: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | generated
    youtube_video_id: Mapped[str | None] = mapped_column(String(32))  # ID do vídeo/mídia na plataforma
    external_url: Mapped[str | None] = mapped_column(String(500))  # link do post (Instagram)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GenerationJob(Base):
    """Pedido de geração de vídeo no MoneyPrinterTurbo.

    status: pending -> processing -> done | failed
    """

    __tablename__ = "generation_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id"))
    subject: Mapped[str] = mapped_column(String(300))
    script: Mapped[str] = mapped_column(Text, default="")
    aspect: Mapped[str] = mapped_column(String(8), default="9:16")
    voice: Mapped[str] = mapped_column(String(100), default="pt-BR-AntonioNeural-Male")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    task_id: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# Colunas adicionadas depois da primeira versão (create_all não altera tabelas existentes).
_NEW_COLUMNS = {
    "channels": {"platform": "VARCHAR(16) NOT NULL DEFAULT 'youtube'"},
    "posts": {"external_url": "VARCHAR(500)"},
}


def init_db() -> None:
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _NEW_COLUMNS.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
