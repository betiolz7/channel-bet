import asyncio
import json
import logging
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import generator, services, youtube
from .config import BASE_DIR, VIDEOS_DIR
from .db import Channel, GenerationJob, Post, SessionLocal, init_db, utcnow
from .worker import worker_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PRIVACY = {"public": "Público", "unlisted": "Não listado", "private": "Privado"}
POST_STATUS = {
    "draft": "Rascunho",
    "queued": "Na fila",
    "uploading": "Enviando",
    "uploaded": "Enviado",
    "failed": "Falhou",
}
JOB_STATUS = {"pending": "Aguardando", "processing": "Gerando", "done": "Pronto", "failed": "Falhou"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        services.recover_interrupted_uploads(db)
    stop = asyncio.Event()
    task = asyncio.create_task(worker_loop(stop))
    yield
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=5)
    except (TimeoutError, asyncio.CancelledError):
        pass


app = FastAPI(title="Channel Bet", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


# ---------- Helpers ----------

def _local(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc).astimezone() if dt else None


def fmt_dt(dt: datetime | None) -> str:
    return _local(dt).strftime("%d/%m/%Y %H:%M") if dt else "—"


def fmt_input_dt(dt: datetime | None) -> str:
    return _local(dt).strftime("%Y-%m-%dT%H:%M") if dt else ""


def fmt_num(n: int | float | None) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", ".")


def fmt_delta(n: int | None) -> str:
    if not n:
        return ""
    return ("+" if n > 0 else "−") + fmt_num(abs(n))


templates.env.filters.update(dt=fmt_dt, input_dt=fmt_input_dt, num=fmt_num, delta=fmt_delta)
templates.env.globals.update(PRIVACY=PRIVACY, POST_STATUS=POST_STATUS, JOB_STATUS=JOB_STATUS)


def get_db():
    with SessionLocal() as db:
        yield db


def go(url: str, msg: str | None = None, error: bool = False) -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + ("err=" if error else "msg=") + quote(msg)
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    return templates.TemplateResponse(request, name, ctx)


def parse_local_dt(value: str | None) -> datetime | None:
    """'2026-11-20T15:00' no fuso do PC -> UTC sem fuso."""
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None)


def all_channels(db: Session) -> list[Channel]:
    return list(db.scalars(select(Channel).order_by(Channel.title)).all())


def validate_post(title: str, description: str, publish_at: datetime | None) -> str | None:
    if not title.strip():
        return "O título é obrigatório."
    if len(title) > 100:
        return "O título pode ter no máximo 100 caracteres."
    if "<" in title or ">" in title:
        return "O YouTube não aceita < ou > no título."
    if len(description) > 5000:
        return "A descrição pode ter no máximo 5000 caracteres."
    if publish_at and publish_at <= utcnow() + timedelta(minutes=10):
        return "A data de publicação precisa estar pelo menos 10 minutos no futuro."
    return None


# ---------- Painel ----------

@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    cards = []
    totals = {"subscribers": 0, "views": 0, "videos": 0, "subs_delta": 0, "views_delta": 0}
    for ch in all_channels(db):
        last = services.latest_snapshot(db, ch.id)
        base = services.snapshot_since(db, ch.id, 7)
        card = {"channel": ch, "last": last, "subs_delta": 0, "views_delta": 0}
        if last:
            totals["subscribers"] += last.subscribers
            totals["views"] += last.views
            totals["videos"] += last.videos
            if base:
                card["subs_delta"] = last.subscribers - base.subscribers
                card["views_delta"] = last.views - base.views
                totals["subs_delta"] += card["subs_delta"]
                totals["views_delta"] += card["views_delta"]
        cards.append(card)

    upcoming = db.scalars(
        select(Post)
        .where(Post.status.in_(["draft", "queued", "uploading", "failed"]) | (Post.publish_at > utcnow()))
        .order_by(Post.publish_at.is_(None), Post.publish_at, Post.created_at.desc())
        .limit(10)
    ).all()
    active_jobs = db.query(GenerationJob).filter(GenerationJob.status.in_(["pending", "processing"])).count()
    names = {c["channel"].id: c["channel"].title for c in cards}
    return render(request, "dashboard.html", cards=cards, totals=totals, upcoming=upcoming,
                  names=names, active_jobs=active_jobs)


@app.post("/stats/refresh")
def refresh_stats(db: Session = Depends(get_db)):
    errors = services.refresh_all_stats(db)
    if errors:
        return go("/", "Alguns canais falharam: " + "; ".join(errors), error=True)
    return go("/", "Estatísticas atualizadas.")


# ---------- OAuth / canais ----------

@app.get("/oauth/start")
def oauth_start():
    try:
        return RedirectResponse(youtube.start_oauth(), status_code=303)
    except youtube.YouTubeSetupError as e:
        return go("/", str(e), error=True)


@app.get("/oauth/callback")
def oauth_callback(request: Request, db: Session = Depends(get_db)):
    if request.query_params.get("error") == "access_denied":
        return go("/", "Acesso negado pelo Google. Se você não cancelou, adicione o e-mail da sua conta em "
                  "Google Cloud → Tela de consentimento OAuth (Público-alvo) → Usuários de teste.", error=True)
    if request.query_params.get("error"):
        return go("/", "Login cancelado no Google.", error=True)
    try:
        creds = youtube.finish_oauth(request.query_params.get("state", ""), str(request.url))
        channel = services.connect_channel(db, creds)
    except Exception as e:
        logging.exception("Falha no OAuth")
        return go("/", f"Não foi possível conectar: {e}", error=True)
    return go("/", f"Canal “{channel.title}” conectado.")


@app.get("/channels/{channel_id}")
def channel_detail(channel_id: str, request: Request, db: Session = Depends(get_db)):
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(404)
    videos, daily, api_error = [], [], None
    try:
        creds = services.get_creds(db, channel)
        if channel.uploads_playlist_id:
            videos = youtube.fetch_recent_videos(creds, channel.uploads_playlist_id)
        daily = youtube.fetch_daily_analytics(creds)
    except Exception as e:
        logging.exception("Falha ao ler dados do canal")
        api_error = str(e)
    chart = {
        "labels": [f"{d['day'][8:10]}/{d['day'][5:7]}" for d in daily],
        "views": [d.get("views", 0) for d in daily],
        "subs": [d.get("subscribersGained", 0) - d.get("subscribersLost", 0) for d in daily],
        "minutes": [d.get("estimatedMinutesWatched", 0) for d in daily],
    }
    summary = {
        "views": sum(chart["views"]),
        "subs": sum(chart["subs"]),
        "hours": sum(chart["minutes"]) // 60,
    }
    last = services.latest_snapshot(db, channel.id)
    posts = db.scalars(select(Post).where(Post.channel_id == channel.id).order_by(Post.created_at.desc()).limit(20)).all()
    return render(request, "channel.html", channel=channel, last=last, videos=videos,
                  chart_json=json.dumps(chart), summary=summary, has_daily=bool(daily),
                  api_error=api_error, posts=posts)


@app.post("/channels/{channel_id}/remove")
def channel_remove(channel_id: str, db: Session = Depends(get_db)):
    channel = db.get(Channel, channel_id)
    if channel:
        services.remove_channel(db, channel)
    return go("/", "Canal desconectado.")


# ---------- Postagens ----------

@app.get("/posts")
def posts_list(request: Request, db: Session = Depends(get_db)):
    posts = db.scalars(select(Post).order_by(Post.created_at.desc()).limit(200)).all()
    channels = all_channels(db)
    names = {c.id: c.title for c in channels}
    busy = any(p.status in ("queued", "uploading") for p in posts)
    return render(request, "posts.html", posts=posts, channels=channels, names=names, busy=busy)


@app.post("/posts")
def posts_create(
    channel_id: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),
    privacy: str = Form("private"),
    publish_at: str = Form(""),
    action: str = Form("draft"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    when = parse_local_dt(publish_at)
    problem = validate_post(title, description, when)
    if problem:
        return go("/posts", problem, error=True)
    if not file.filename:
        return go("/posts", "Escolha um arquivo de vídeo.", error=True)
    if db.get(Channel, channel_id) is None:
        return go("/posts", "Canal inválido.", error=True)

    dest = VIDEOS_DIR / f"{uuid.uuid4().hex}{Path(file.filename).suffix.lower() or '.mp4'}"
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)

    post = Post(
        channel_id=channel_id,
        title=title.strip(),
        description=description,
        tags=tags,
        privacy=privacy if privacy in PRIVACY else "private",
        publish_at=when,
        file_path=str(dest),
        status="queued" if action == "queue" else "draft",
    )
    db.add(post)
    db.commit()
    return go("/posts", "Vídeo na fila de envio." if post.status == "queued" else "Rascunho salvo.")


@app.get("/posts/{post_id}")
def post_edit(post_id: int, request: Request, db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(404)
    return render(request, "post_edit.html", post=post, channels=all_channels(db))


@app.post("/posts/{post_id}")
def post_update(
    post_id: int,
    channel_id: str = Form(""),
    title: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),
    privacy: str = Form("private"),
    publish_at: str = Form(""),
    action: str = Form("save"),
    db: Session = Depends(get_db),
):
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(404)
    if post.status in ("uploading", "uploaded"):
        return go(f"/posts/{post_id}", "Este vídeo já foi enviado e não pode ser editado aqui.", error=True)
    when = parse_local_dt(publish_at)
    problem = validate_post(title, description, when)
    if not problem and action == "queue" and not db.get(Channel, channel_id):
        problem = "Escolha o canal antes de enviar."
    if problem:
        return go(f"/posts/{post_id}", problem, error=True)

    post.channel_id = channel_id or None
    post.title = title.strip()
    post.description = description
    post.tags = tags
    post.privacy = privacy if privacy in PRIVACY else "private"
    post.publish_at = when
    post.error = None
    post.status = "queued" if action == "queue" else "draft"
    db.commit()
    return go("/posts", "Vídeo na fila de envio." if post.status == "queued" else "Alterações salvas.")


@app.post("/posts/{post_id}/delete")
def post_delete(post_id: int, db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if post is None or post.status == "uploading":
        return go("/posts", "Não é possível excluir agora.", error=True)
    Path(post.file_path).unlink(missing_ok=True)
    db.query(GenerationJob).filter(GenerationJob.post_id == post.id).update({"post_id": None})
    db.delete(post)
    db.commit()
    return go("/posts", "Postagem excluída (o vídeo no YouTube, se houver, não foi apagado).")


@app.get("/media/{post_id}")
def post_media(post_id: int, db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if post is None or not Path(post.file_path).exists():
        raise HTTPException(404)
    return FileResponse(post.file_path)


# ---------- Geração de vídeo ----------

@app.get("/generate")
def generate_page(request: Request, db: Session = Depends(get_db)):
    jobs = db.scalars(select(GenerationJob).order_by(GenerationJob.created_at.desc()).limit(50)).all()
    channels = all_channels(db)
    names = {c.id: c.title for c in channels}
    active = any(j.status in ("pending", "processing") for j in jobs)
    return render(request, "generate.html", jobs=jobs, channels=channels, names=names,
                  voices=generator.VOICES, active=active)


@app.post("/generate")
def generate_create(
    subject: str = Form(...),
    script: str = Form(""),
    channel_id: str = Form(""),
    aspect: str = Form("9:16"),
    voice: str = Form(generator.VOICES[0][0]),
    db: Session = Depends(get_db),
):
    if not subject.strip():
        return go("/generate", "Informe o tema do vídeo.", error=True)
    job = GenerationJob(
        subject=subject.strip()[:300],
        script=script.strip(),
        channel_id=channel_id or None,
        aspect=aspect if aspect in ("9:16", "16:9", "1:1") else "9:16",
        voice=voice,
    )
    db.add(job)
    db.commit()
    return go("/generate", "Pedido enviado. O vídeo aparece como rascunho em Postagens quando ficar pronto.")


@app.post("/generate/{job_id}/retry")
def generate_retry(job_id: int, db: Session = Depends(get_db)):
    job = db.get(GenerationJob, job_id)
    if job and job.status == "failed":
        job.status, job.task_id, job.progress, job.error = "pending", None, 0, None
        db.commit()
    return go("/generate", "Geração reenviada.")
