"""Regras de negócio que unem banco, YouTube e gerador."""

import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import generator, instagram, youtube
from .config import STATS_REFRESH_HOURS, VIDEOS_DIR
from .db import Channel, ChannelSnapshot, GenerationJob, Post, utcnow

log = logging.getLogger("channelbet")


# ---------- Canais ----------

def get_creds(db: Session, channel: Channel):
    creds, refreshed = youtube.credentials_from_json(channel.credentials_json)
    if refreshed:
        channel.credentials_json = creds.to_json()
        db.commit()
    return creds


def connect_channel(db: Session, creds) -> Channel:
    info = _channel_info(creds)
    channel = db.get(Channel, info["id"])
    if channel is None:
        channel = Channel(id=info["id"], title=info["title"], credentials_json=creds.to_json())
        db.add(channel)
    channel.title = info["title"]
    channel.thumbnail_url = info["thumbnail_url"]
    channel.uploads_playlist_id = info["uploads_playlist_id"]
    channel.credentials_json = creds.to_json()
    db.add(_snapshot(channel.id, info))
    db.commit()
    return channel


def ig_token(channel: Channel) -> str:
    return json.loads(channel.credentials_json)["token"]


def connect_instagram(db: Session, accounts: list[dict]) -> list[Channel]:
    """Salva (ou atualiza) as contas do Instagram devolvidas pelo login."""
    channels = []
    for acc in accounts:
        channel = db.get(Channel, acc["id"])
        creds = json.dumps({"token": acc["token"], "page_id": acc["page_id"]})
        if channel is None:
            channel = Channel(id=acc["id"], platform="instagram", title="", credentials_json=creds)
            db.add(channel)
        channel.platform = "instagram"
        channel.title = "@" + acc["username"]
        channel.thumbnail_url = acc["picture"]
        channel.credentials_json = creds
        db.flush()
        try:
            info = _instagram_info(channel)
        except Exception:
            log.exception("Falha ao ler estatísticas de %s", channel.title)
            info = {"subscribers": acc["followers"], "views": 0, "videos": acc["media_count"]}
        db.add(_snapshot(channel.id, info))
        channels.append(channel)
    db.commit()
    return channels


def _instagram_info(channel: Channel) -> dict:
    token = ig_token(channel)
    acc = instagram.account_info(token, channel.id)
    channel.title = "@" + acc["username"]
    channel.thumbnail_url = acc["picture"]
    # Não existe total de views da conta; somamos as views dos posts recentes.
    media = instagram.recent_media(token, channel.id, limit=50)
    return {"subscribers": acc["followers"], "views": sum(m["views"] for m in media), "videos": acc["media_count"]}


def remove_channel(db: Session, channel: Channel) -> None:
    db.query(ChannelSnapshot).filter(ChannelSnapshot.channel_id == channel.id).delete()
    db.execute(update(Post).where(Post.channel_id == channel.id).values(channel_id=None))
    db.execute(update(GenerationJob).where(GenerationJob.channel_id == channel.id).values(channel_id=None))
    db.delete(channel)
    db.commit()


def _snapshot(channel_id: str, info: dict) -> ChannelSnapshot:
    return ChannelSnapshot(
        channel_id=channel_id,
        subscribers=info["subscribers"],
        views=info["views"],
        videos=info["videos"],
    )


def _channel_info(creds) -> dict:
    """Dados do canal, com views corrigidas pela soma dos vídeos (o total do canal atrasa)."""
    info = youtube.parse_channel(youtube.fetch_my_channel(creds))
    if info["uploads_playlist_id"]:
        try:
            info["views"] = max(info["views"], youtube.fetch_total_video_views(creds, info["uploads_playlist_id"]))
        except Exception:
            log.exception("Falha ao somar views dos vídeos de %s", info["title"])
    return info


def refresh_channel_stats(db: Session, channel: Channel) -> None:
    if channel.platform == "instagram":
        db.add(_snapshot(channel.id, _instagram_info(channel)))
        db.commit()
        return
    info = _channel_info(get_creds(db, channel))
    channel.title = info["title"]
    channel.thumbnail_url = info["thumbnail_url"]
    channel.uploads_playlist_id = info["uploads_playlist_id"]
    db.add(_snapshot(channel.id, info))
    db.commit()


def refresh_all_stats(db: Session, only_stale: bool = False) -> list[str]:
    """Atualiza estatísticas. Retorna lista de erros (um por canal que falhou)."""
    errors = []
    cutoff = utcnow() - timedelta(hours=STATS_REFRESH_HOURS)
    for channel in db.scalars(select(Channel)).all():
        if only_stale:
            last = latest_snapshot(db, channel.id)
            if last and last.taken_at > cutoff:
                continue
        try:
            refresh_channel_stats(db, channel)
        except Exception as e:  # um canal com problema não pode travar os outros
            db.rollback()
            log.exception("Falha ao atualizar %s", channel.title)
            errors.append(f"{channel.title}: {e}")
    return errors


def latest_snapshot(db: Session, channel_id: str) -> ChannelSnapshot | None:
    return db.scalars(
        select(ChannelSnapshot)
        .where(ChannelSnapshot.channel_id == channel_id)
        .order_by(ChannelSnapshot.taken_at.desc())
        .limit(1)
    ).first()


def snapshot_since(db: Session, channel_id: str, days: int) -> ChannelSnapshot | None:
    """Snapshot mais antigo dentro da janela, usado como base para variação."""
    return db.scalars(
        select(ChannelSnapshot)
        .where(
            ChannelSnapshot.channel_id == channel_id,
            ChannelSnapshot.taken_at >= utcnow() - timedelta(days=days),
        )
        .order_by(ChannelSnapshot.taken_at.asc())
        .limit(1)
    ).first()


# ---------- Uploads ----------

def recover_interrupted_uploads(db: Session) -> None:
    """Uploads que estavam em andamento quando o app fechou viram 'failed'.

    Não reenfileiramos automaticamente porque o vídeo pode ter chegado ao YouTube.
    """
    db.execute(
        update(Post)
        .where(Post.status == "uploading")
        .values(status="failed", error="Envio interrompido (app fechado). Confira no YouTube Studio ou no Instagram antes de tentar de novo.")
    )
    db.commit()


def _next_due_post(db: Session) -> Post | None:
    """Próximo post da fila. No YouTube o agendamento é feito pelo próprio YouTube (envia na hora);
    no Instagram não há agendamento pela API, então o post só sai quando chega o horário."""
    now = utcnow()
    for post in db.scalars(select(Post).where(Post.status == "queued").order_by(Post.created_at)).all():
        channel = db.get(Channel, post.channel_id) if post.channel_id else None
        if channel and channel.platform == "instagram" and post.publish_at and post.publish_at > now:
            continue
        return post
    return None


def process_next_upload(db: Session) -> bool:
    post = _next_due_post(db)
    if post is None:
        return False
    channel = db.get(Channel, post.channel_id) if post.channel_id else None
    if channel is None:
        post.status, post.error = "failed", "Canal não definido ou removido."
        db.commit()
        return True

    post_id = post.id
    post.status, post.error = "uploading", None
    db.commit()
    try:
        if channel.platform == "instagram":
            media_id, permalink = instagram.publish_reel(
                ig_token(channel), channel.id, Path(post.file_path), post.description or post.title
            )
            post.youtube_video_id, post.external_url, post.status = media_id, permalink, "uploaded"
            log.info("Reel publicado: %s -> %s", post.title, media_id)
            db.commit()
            return True
        publish_at = None
        if post.publish_at and post.publish_at > utcnow() + timedelta(minutes=5):
            publish_at = post.publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        tags = [t.strip() for t in post.tags.split(",") if t.strip()]
        video_id = youtube.upload_video(
            get_creds(db, channel),
            Path(post.file_path),
            post.title,
            post.description,
            tags,
            post.privacy,
            publish_at,
        )
        post.youtube_video_id = video_id
        post.status = "uploaded"
        log.info("Upload concluído: %s -> %s", post.title, video_id)
    except Exception as e:
        db.rollback()
        post = db.get(Post, post_id)
        post.status, post.error = "failed", str(e)
        log.exception("Falha no upload de %s", post.title)
    db.commit()
    return True


# ---------- Geração ----------

def process_generation_jobs(db: Session) -> None:
    jobs = db.scalars(
        select(GenerationJob).where(GenerationJob.status.in_(["pending", "processing"]))
    ).all()
    for job in jobs:
        try:
            if job.status == "pending":
                job.task_id = generator.submit(job.subject, job.script, job.aspect, job.voice)
                job.status, job.error = "processing", None
            else:
                _poll_job(db, job)
        except generator.GeneratorOffline as e:
            job.error = str(e)  # mantém o status e tenta de novo no próximo ciclo
        except Exception as e:
            log.exception("Falha na geração %s", job.id)
            job.status, job.error = "failed", str(e)
        db.commit()


def _poll_job(db: Session, job: GenerationJob) -> None:
    task = generator.get_task(job.task_id)
    job.progress = int(task.get("progress") or job.progress)
    job.error = None
    state = task.get("state")
    if state == generator.STATE_FAILED:
        job.status, job.error = "failed", "O MoneyPrinterTurbo informou falha. Veja o log dele."
    elif state == generator.STATE_COMPLETE:
        urls = generator.video_urls(task)
        if not urls:
            raise RuntimeError("Tarefa concluída sem vídeo.")
        dest = VIDEOS_DIR / f"gen-{job.id}-{uuid.uuid4().hex[:8]}.mp4"
        generator.download(urls[0], dest)
        post = Post(
            channel_id=job.channel_id,
            title=job.subject[:100],
            description=job.script,
            file_path=str(dest),
            status="draft",
            source="generated",
        )
        db.add(post)
        db.flush()
        job.post_id, job.status, job.progress = post.id, "done", 100
