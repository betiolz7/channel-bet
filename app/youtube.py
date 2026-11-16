"""Integração com YouTube Data API v3, YouTube Analytics API v2 e OAuth do Google."""

import json
from datetime import date, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import CLIENT_SECRETS_FILE, OAUTH_REDIRECT_URI, SCOPES

# Fluxos OAuth em andamento, indexados pelo "state" (guardam o code_verifier do PKCE).
_pending_flows: dict[str, Flow] = {}


class YouTubeSetupError(RuntimeError):
    pass


# ---------- OAuth ----------

def start_oauth() -> str:
    if not CLIENT_SECRETS_FILE.exists():
        raise YouTubeSetupError(
            f"Arquivo {CLIENT_SECRETS_FILE.name} não encontrado. Veja o README para criar as credenciais OAuth."
        )
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRETS_FILE), scopes=SCOPES, redirect_uri=OAUTH_REDIRECT_URI
    )
    # prompt=consent garante refresh_token; select_account permite escolher contas de marca.
    url, state = flow.authorization_url(access_type="offline", prompt="consent select_account")
    _pending_flows[state] = flow
    return url


def finish_oauth(state: str, authorization_response: str) -> Credentials:
    flow = _pending_flows.pop(state, None)
    if flow is None:
        raise YouTubeSetupError("Sessão de login expirada. Tente conectar o canal novamente.")
    flow.fetch_token(authorization_response=authorization_response)
    return flow.credentials


def credentials_from_json(raw: str) -> tuple[Credentials, bool]:
    """Carrega credenciais e renova o token se necessário. Retorna (creds, foi_renovado)."""
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise YouTubeSetupError("Credenciais sem refresh token. Reconecte o canal.")
        creds.refresh(Request())
        return creds, True
    return creds, False


def _yt(creds: Credentials):
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _analytics(creds: Credentials):
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


# ---------- Leitura ----------

def fetch_my_channel(creds: Credentials) -> dict:
    """Canal autorizado no login (custo: 1 unidade de cota)."""
    resp = _yt(creds).channels().list(part="snippet,statistics,contentDetails", mine=True).execute()
    items = resp.get("items") or []
    if not items:
        raise YouTubeSetupError("Esta conta Google não tem canal no YouTube.")
    return items[0]


def parse_channel(item: dict) -> dict:
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    thumbs = snippet.get("thumbnails", {})
    thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url")
    return {
        "id": item["id"],
        "title": snippet.get("title", item["id"]),
        "thumbnail_url": thumb,
        "uploads_playlist_id": item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
        "subscribers": int(stats.get("subscriberCount", 0)),
        "views": int(stats.get("viewCount", 0)),
        "videos": int(stats.get("videoCount", 0)),
    }


def fetch_recent_videos(creds: Credentials, uploads_playlist_id: str, limit: int = 15) -> list[dict]:
    """Últimos vídeos com estatísticas (custo: 2 unidades, bem mais barato que search)."""
    yt = _yt(creds)
    items = (
        yt.playlistItems()
        .list(part="contentDetails", playlistId=uploads_playlist_id, maxResults=limit)
        .execute()
        .get("items", [])
    )
    ids = [i["contentDetails"]["videoId"] for i in items]
    if not ids:
        return []
    videos = yt.videos().list(part="snippet,statistics,status", id=",".join(ids)).execute().get("items", [])
    out = []
    for v in videos:
        s = v.get("statistics", {})
        out.append(
            {
                "id": v["id"],
                "title": v["snippet"]["title"],
                "published_at": v["snippet"].get("publishedAt", "")[:10],
                "privacy": v.get("status", {}).get("privacyStatus"),
                "publish_at": v.get("status", {}).get("publishAt"),
                "views": int(s.get("viewCount", 0)),
                "likes": int(s.get("likeCount", 0)),
                "comments": int(s.get("commentCount", 0)),
            }
        )
    order = {vid: n for n, vid in enumerate(ids)}
    out.sort(key=lambda v: order.get(v["id"], 0))
    return out


def fetch_daily_analytics(creds: Credentials, days: int = 28) -> list[dict]:
    """Métricas diárias do canal. O YouTube costuma ter 2-3 dias de atraso nesses dados."""
    end = date.today()
    start = end - timedelta(days=days)
    resp = (
        _analytics(creds)
        .reports()
        .query(
            ids="channel==MINE",
            startDate=start.isoformat(),
            endDate=end.isoformat(),
            metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost",
            dimensions="day",
            sort="day",
        )
        .execute()
    )
    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    return [dict(zip(headers, row)) for row in resp.get("rows", [])]


# ---------- Upload ----------

def upload_video(
    creds: Credentials,
    file_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy: str,
    publish_at_rfc3339: str | None,
) -> str:
    """Envia o vídeo (custo alto de cota). Retorna o ID do vídeo.

    Com publish_at, o vídeo sobe privado e o próprio YouTube o torna público na data,
    então o PC não precisa estar ligado no horário da publicação.
    """
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at_rfc3339:
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at_rfc3339
    body = {
        "snippet": {"title": title, "description": description, "tags": tags, "categoryId": "22"},
        "status": status,
    }
    media = MediaFileUpload(str(file_path), chunksize=8 * 1024 * 1024, resumable=True)
    request = _yt(creds).videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk(num_retries=3)
    return response["id"]
