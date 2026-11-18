import asyncio
import calendar
import json
import logging
import shutil
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ai, generator, instagram, services, youtube
from .config import BASE_DIR, VIDEOS_DIR
from .db import Channel, ChannelSnapshot, GenerationJob, Post, SessionLocal, init_db, utcnow
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
PLATFORMS = {"youtube": "YouTube", "instagram": "Instagram"}
JOB_STATUS = {"pending": "Aguardando", "processing": "Gerando", "done": "Pronto", "failed": "Falhou"}
PALETTE = ["#e11d48", "#2563eb", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d"]
MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
          "agosto", "setembro", "outubro", "novembro", "dezembro"]


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


# ---------- Formatação ----------

def _local(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc).astimezone() if dt else None


def fmt_dt(dt: datetime | None) -> str:
    return _local(dt).strftime("%d/%m/%Y %H:%M") if dt else "—"


def fmt_time(dt: datetime | None) -> str:
    return _local(dt).strftime("%H:%M") if dt else ""


def fmt_input_dt(dt: datetime | None) -> str:
    return _local(dt).strftime("%Y-%m-%dT%H:%M") if dt else ""


def fmt_num(n: int | float | None) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", ".")


def fmt_compact(n: int | float | None) -> str:
    if n is None:
        return "—"
    n = int(n)
    for limit, suffix in ((1_000_000_000, " bi"), (1_000_000, " mi"), (10_000, " mil")):
        if abs(n) >= limit:
            return f"{n / limit:.1f}".rstrip("0").rstrip(".").replace(".", ",") + suffix
    return fmt_num(n)


def fmt_delta(n: int | None) -> str:
    if not n:
        return ""
    return ("+" if n > 0 else "−") + fmt_num(abs(n))


def sparkline(values: list[int], w: int = 120, h: int = 36) -> str:
    """Pontos de uma polyline SVG; vazio se não houver dados suficientes."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1
    step = w / (len(values) - 1)
    return " ".join(f"{i * step:.1f},{h - 3 - (v - lo) / rng * (h - 6):.1f}" for i, v in enumerate(values))


templates.env.filters.update(dt=fmt_dt, time=fmt_time, input_dt=fmt_input_dt, num=fmt_num,
                             compact=fmt_compact, delta=fmt_delta)
templates.env.globals.update(PRIVACY=PRIVACY, POST_STATUS=POST_STATUS, JOB_STATUS=JOB_STATUS, PLATFORMS=PLATFORMS,
                             CAPTION_LIMIT=instagram.CAPTION_LIMIT)


# ---------- Helpers ----------

def get_db():
    with SessionLocal() as db:
        yield db


def go(url: str, msg: str | None = None, error: bool = False) -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + ("err=" if error else "msg=") + quote(msg)
    return RedirectResponse(url, status_code=303)


def all_channels(db: Session) -> list[Channel]:
    return list(db.scalars(select(Channel).order_by(Channel.connected_at)).all())


def channel_colors(channels: list[Channel]) -> dict[str, str]:
    """Cor fixa por canal, pela ordem de conexão."""
    return {c.id: PALETTE[i % len(PALETTE)] for i, c in enumerate(channels)}


def render(request: Request, name: str, **ctx):
    with SessionLocal() as db:
        channels = all_channels(db)
        ctx.setdefault("nav_channels", channels)
        ctx.setdefault("colors", channel_colors(channels))
        ctx.setdefault("platform_of", {c.id: c.platform for c in channels})
        ctx.setdefault("pending_count", db.query(Post).filter(Post.status.in_(["draft", "failed"])).count())
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    return templates.TemplateResponse(request, name, ctx)


def parse_local_dt(value: str | None) -> datetime | None:
    """'2026-11-20T15:00' no fuso do PC -> UTC sem fuso."""
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None)


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
    channels = all_channels(db)
    month_ago = utcnow() - timedelta(days=30)
    cards = []
    totals = {"subscribers": 0, "views": 0, "videos": 0, "subs_delta": 0, "views_delta": 0}
    for ch in channels:
        snaps = db.scalars(
            select(ChannelSnapshot)
            .where(ChannelSnapshot.channel_id == ch.id, ChannelSnapshot.taken_at >= month_ago)
            .order_by(ChannelSnapshot.taken_at)
        ).all()
        last = snaps[-1] if snaps else services.latest_snapshot(db, ch.id)
        base = services.snapshot_since(db, ch.id, 7)
        # Um ponto por dia (último snapshot do dia) para o mini-gráfico.
        per_day = {}
        for s in snaps:
            per_day[_local(s.taken_at).date()] = s
        card = {
            "channel": ch,
            "last": last,
            "subs_delta": 0,
            "views_delta": 0,
            "spark": sparkline([s.views for s in per_day.values()]),
        }
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

    now = utcnow()
    week = db.scalars(
        select(Post)
        .where(Post.publish_at >= now, Post.publish_at < now + timedelta(days=7))
        .order_by(Post.publish_at)
    ).all()
    attention = db.scalars(
        select(Post).where(Post.status.in_(["draft", "failed", "queued", "uploading"])).order_by(Post.created_at.desc()).limit(8)
    ).all()
    active_jobs = db.query(GenerationJob).filter(GenerationJob.status.in_(["pending", "processing"])).count()
    names = {c.id: c.title for c in channels}
    return render(request, "dashboard.html", cards=cards, totals=totals, week=week, attention=attention,
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
    return go("/", f"Canal “{channel.title.strip()}” conectado.")


@app.get("/oauth/meta/start")
def meta_oauth_start():
    try:
        return RedirectResponse(instagram.start_oauth(), status_code=303)
    except instagram.InstagramError as e:
        return go("/", str(e), error=True)


@app.get("/oauth/meta/callback")
def meta_oauth_callback(request: Request, db: Session = Depends(get_db)):
    q = request.query_params
    if q.get("error"):
        return go("/", "Login do Facebook cancelado ou negado. Se você não cancelou, confira se sua conta tem "
                  "função de administrador ou testador no app da Meta.", error=True)
    try:
        accounts = instagram.finish_oauth(q.get("state", ""), q.get("code", ""))
        if not accounts:
            return go("/", "Nenhuma conta do Instagram encontrada. A conta precisa ser Profissional (Criador ou "
                      "Empresa), estar vinculada a uma Página do Facebook, e a Página precisa ser marcada na "
                      "tela de permissões do login.", error=True)
        channels = services.connect_instagram(db, accounts)
    except Exception as e:
        logging.exception("Falha no login da Meta")
        return go("/", f"Não foi possível conectar o Instagram: {e}", error=True)
    names = ", ".join(c.title for c in channels)
    return go("/", f"Instagram conectado: {names}.")


@app.get("/channels/{channel_id}")
def channel_detail(channel_id: str, request: Request, db: Session = Depends(get_db)):
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(404)
    if channel.platform == "instagram":
        return instagram_detail(channel, request, db)
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
    last_day = daily[-1]["day"] if daily else None
    analytics_lagging = bool(videos) and summary["views"] == 0 and sum(v["views"] for v in videos) > 0
    top_views = max((v["views"] for v in videos), default=0)
    last = services.latest_snapshot(db, channel.id)
    posts = db.scalars(select(Post).where(Post.channel_id == channel.id).order_by(Post.created_at.desc()).limit(20)).all()
    return render(request, "channel.html", channel=channel, last=last, videos=videos,
                  chart_json=json.dumps(chart), summary=summary, has_daily=any(chart["views"]),
                  api_error=api_error, posts=posts, last_day=last_day,
                  analytics_lagging=analytics_lagging, top_views=top_views)


def instagram_detail(channel: Channel, request: Request, db: Session):
    media, api_error = [], None
    try:
        media = instagram.recent_media(services.ig_token(channel), channel.id)
    except Exception as e:
        logging.exception("Falha ao ler dados do Instagram")
        api_error = str(e)
    totals = {k: sum(m[k] for m in media) for k in ("views", "likes", "comments")}
    last = services.latest_snapshot(db, channel.id)
    posts = db.scalars(select(Post).where(Post.channel_id == channel.id).order_by(Post.created_at.desc()).limit(20)).all()
    return render(request, "channel_ig.html", channel=channel, last=last, media=media, totals=totals,
                  api_error=api_error, posts=posts)


@app.post("/channels/{channel_id}/remove")
def channel_remove(channel_id: str, db: Session = Depends(get_db)):
    channel = db.get(Channel, channel_id)
    if channel:
        services.remove_channel(db, channel)
    return go("/", "Canal desconectado.")


# ---------- Calendário ----------

@app.get("/calendar")
def calendar_page(request: Request, m: str | None = None, db: Session = Depends(get_db)):
    today = datetime.now().date()
    try:
        year, month = (int(x) for x in m.split("-")) if m else (today.year, today.month)
        first = date(year, month, 1)
    except (ValueError, AttributeError):
        first = today.replace(day=1)
    weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month)

    events = defaultdict(list)
    undated = 0
    for p in db.scalars(select(Post)).all():
        when = p.publish_at or (p.updated_at if p.status == "uploaded" else None)
        if when is None:
            undated += 1
            continue
        day = _local(when).date()
        if weeks[0][0] <= day <= weeks[-1][-1]:
            events[day].append((p, when))
    for day in events:
        events[day].sort(key=lambda e: e[1])

    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    next_m = (first + timedelta(days=32)).replace(day=1).strftime("%Y-%m")
    return render(request, "calendar.html", weeks=weeks, events=events, first=first, today=today,
                  title=f"{MONTHS[first.month - 1].capitalize()} de {first.year}",
                  prev_m=prev_m, next_m=next_m, undated=undated)


# ---------- Postagens ----------

@app.get("/posts")
def posts_list(request: Request, status: str = "", channel: str = "", db: Session = Depends(get_db)):
    query = select(Post).order_by(Post.created_at.desc()).limit(300)
    if status in POST_STATUS:
        query = query.where(Post.status == status)
    if channel:
        query = query.where(Post.channel_id == channel)
    posts = db.scalars(query).all()
    counts = defaultdict(int)
    for st, in db.query(Post.status).all():
        counts[st] += 1
    counts["all"] = sum(counts.values())
    names = {c.id: c.title for c in all_channels(db)}
    busy = any(p.status in ("queued", "uploading") for p in posts)
    return render(request, "posts.html", posts=posts, names=names, busy=busy, counts=counts,
                  status=status, channel=channel)


@app.get("/posts/new")
def post_new(request: Request, db: Session = Depends(get_db)):
    prefill = ""
    if d := request.query_params.get("date"):
        try:
            prefill = f"{date.fromisoformat(d).isoformat()}T18:00"
        except ValueError:
            pass
    return render(request, "post_new.html", channels=all_channels(db), prefill_date=prefill,
                  preselect=request.query_params.get("channel"))


@app.post("/posts")
async def posts_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    channel_ids = [c for c in form.getlist("channel_ids") if c]
    title = str(form.get("title", ""))
    description = str(form.get("description", ""))
    tags = str(form.get("tags", ""))
    caption = str(form.get("caption", "")).strip()
    privacy = str(form.get("privacy", "private"))
    action = str(form.get("action", "draft"))
    file = form.get("file")

    when = parse_local_dt(str(form.get("publish_at", "")))
    problem = validate_post(title, description, when)
    if not problem and not channel_ids:
        problem = "Escolha pelo menos um canal."
    if not problem and not getattr(file, "filename", None):
        problem = "Escolha um arquivo de vídeo."
    platforms = {c.id: c.platform for c in all_channels(db)}
    if not problem and not set(channel_ids) <= platforms.keys():
        problem = "Canal inválido."
    if not caption:  # legenda padrão do Instagram: título + descrição
        caption = f"{title.strip()}\n\n{description.strip()}".strip()
    if not problem and len(caption) > instagram.CAPTION_LIMIT and any(platforms[c] == "instagram" for c in channel_ids):
        problem = f"A legenda do Instagram pode ter no máximo {instagram.CAPTION_LIMIT} caracteres."
    if problem:
        return go("/posts/new", problem, error=True)

    dest = VIDEOS_DIR / f"{uuid.uuid4().hex}{Path(file.filename).suffix.lower() or '.mp4'}"
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)

    status = "queued" if action == "queue" else "draft"
    for cid in channel_ids:  # o mesmo arquivo é usado por todas as postagens
        is_ig = platforms[cid] == "instagram"
        db.add(Post(
            channel_id=cid,
            title=title.strip(),
            description=caption if is_ig else description,  # no Instagram a descrição é a legenda
            tags="" if is_ig else tags,
            privacy="public" if is_ig else (privacy if privacy in PRIVACY else "private"),
            publish_at=when,
            file_path=str(dest),
            status=status,
        ))
    db.commit()
    n = len(channel_ids)
    what = f"{n} postagens criadas" if n > 1 else "Postagem criada"
    return go("/posts", f"{what}{' e na fila de envio' if status == 'queued' else ' como rascunho'}.")


@app.get("/posts/{post_id}")
def post_edit(post_id: int, request: Request, db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(404)
    channel = db.get(Channel, post.channel_id) if post.channel_id else None
    return render(request, "post_edit.html", post=post, channels=all_channels(db),
                  is_ig=bool(channel and channel.platform == "instagram"))


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
    channel = db.get(Channel, channel_id) if channel_id else None
    if not problem and action == "queue" and not channel:
        problem = "Escolha o canal antes de enviar."
    is_ig = bool(channel and channel.platform == "instagram")
    if not problem and is_ig and len(description) > instagram.CAPTION_LIMIT:
        problem = f"A legenda do Instagram pode ter no máximo {instagram.CAPTION_LIMIT} caracteres."
    if problem:
        return go(f"/posts/{post_id}", problem, error=True)

    post.channel_id = channel_id or None
    post.title = title.strip()
    post.description = description
    post.tags = tags
    post.privacy = "public" if is_ig else (privacy if privacy in PRIVACY else "private")
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
    shared = db.query(Post).filter(Post.file_path == post.file_path, Post.id != post.id).count()
    if not shared:  # o arquivo pode ser usado por postagens em outros canais
        Path(post.file_path).unlink(missing_ok=True)
    db.query(GenerationJob).filter(GenerationJob.post_id == post.id).update({"post_id": None})
    db.delete(post)
    db.commit()
    return go("/posts", "Postagem excluída (o que já foi publicado no YouTube ou Instagram não foi apagado).")


@app.get("/media/{post_id}")
def post_media(post_id: int, db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if post is None or not Path(post.file_path).exists():
        raise HTTPException(404)
    return FileResponse(post.file_path)


# ---------- IA ----------

@app.post("/ai/suggest")
async def ai_suggest(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    topic = str(data.get("topic", "")).strip()
    if not topic:
        return JSONResponse({"error": "Escreva um título provisório ou tema primeiro."}, status_code=400)
    channel = db.get(Channel, data.get("channel_id") or "")
    try:
        result = await asyncio.to_thread(
            ai.suggest, topic, str(data.get("notes", "")),
            channel.title.strip() if channel else "", bool(data.get("is_short")),
        )
    except ai.AIError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return result


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
