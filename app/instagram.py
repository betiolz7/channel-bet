"""Integração com o Instagram (API de Gráficos via Login do Facebook).

Por que Login do Facebook e não "Login do Instagram": só o Login do Facebook aceita enviar o vídeo
direto do PC (upload retomável em rupload.facebook.com). O Login do Instagram exige que o vídeo
esteja em um servidor público, o que um app local não tem.

Requisitos: conta do Instagram Profissional (Criador ou Empresa) vinculada a uma Página do Facebook.
"""

import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .config import META_API_VERSION, META_APP_ID, META_APP_SECRET, META_CONFIG_ID, META_REDIRECT_URI

GRAPH = f"https://graph.facebook.com/{META_API_VERSION}"
SCOPES = [
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_insights",
    "pages_show_list",
    "pages_read_engagement",
    "business_management",
]
CAPTION_LIMIT = 2200
PROCESSING_TIMEOUT = 15 * 60  # segundos esperando o Instagram processar o vídeo

_pending_states: set[str] = set()


class InstagramError(RuntimeError):
    pass


# ---------- HTTP ----------

def _check(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise InstagramError(f"Resposta inesperada da Meta (HTTP {resp.status_code}).")
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        msg = err.get("error_user_msg") or err.get("message") or str(err)
        raise InstagramError(f"Meta: {msg}")
    if resp.status_code >= 400:
        raise InstagramError(f"Meta: HTTP {resp.status_code}")
    return data


def _get(path: str, **params) -> dict:
    return _check(httpx.get(f"{GRAPH}/{path.lstrip('/')}", params=params, timeout=30))


def _post(path: str, **data) -> dict:
    return _check(httpx.post(f"{GRAPH}/{path.lstrip('/')}", data=data, timeout=60))


# ---------- OAuth ----------

def configured() -> bool:
    return bool(META_APP_ID and META_APP_SECRET)


def start_oauth() -> str:
    if not configured():
        raise InstagramError("Configure META_APP_ID e META_APP_SECRET no arquivo .env. Veja o README.")
    state = secrets.token_urlsafe(24)
    _pending_states.add(state)
    params = {
        "client_id": META_APP_ID,
        "redirect_uri": META_REDIRECT_URI,
        "state": state,
        "response_type": "code",
    }
    # Apps do tipo "Empresa" usam uma configuração do Login do Facebook para Empresas.
    if META_CONFIG_ID:
        params["config_id"] = META_CONFIG_ID
    else:
        params["scope"] = ",".join(SCOPES)
    return f"https://www.facebook.com/{META_API_VERSION}/dialog/oauth?{urlencode(params)}"


def finish_oauth(state: str, code: str) -> list[dict]:
    """Troca o código por tokens e devolve as contas do Instagram acessíveis."""
    if state not in _pending_states:
        raise InstagramError("Sessão de login expirada. Tente conectar de novo.")
    _pending_states.discard(state)
    short = _get(
        "oauth/access_token",
        client_id=META_APP_ID,
        client_secret=META_APP_SECRET,
        redirect_uri=META_REDIRECT_URI,
        code=code,
    )["access_token"]
    # Token de usuário de longa duração: os tokens de Página derivados dele não expiram.
    user_token = _get(
        "oauth/access_token",
        grant_type="fb_exchange_token",
        client_id=META_APP_ID,
        client_secret=META_APP_SECRET,
        fb_exchange_token=short,
    )["access_token"]
    return list_accounts(user_token)


def list_accounts(user_token: str) -> list[dict]:
    fields = ("id,name,access_token,instagram_business_account"
              "{id,username,name,profile_picture_url,followers_count,media_count}")
    accounts, after = [], None
    while True:
        params = {"fields": fields, "limit": 100, "access_token": user_token}
        if after:
            params["after"] = after
        resp = _get("me/accounts", **params)
        for page in resp.get("data", []):
            ig = page.get("instagram_business_account")
            if not ig:
                continue
            accounts.append({
                "id": ig["id"],
                "username": ig.get("username") or ig.get("name") or ig["id"],
                "picture": ig.get("profile_picture_url"),
                "followers": ig.get("followers_count", 0),
                "media_count": ig.get("media_count", 0),
                "page_id": page["id"],
                "page_name": page.get("name", ""),
                "token": page["access_token"],
            })
        after = resp.get("paging", {}).get("cursors", {}).get("after")
        if not after or not resp.get("paging", {}).get("next"):
            return accounts


# ---------- Leitura ----------

def account_info(token: str, ig_id: str) -> dict:
    data = _get(ig_id, fields="username,name,profile_picture_url,followers_count,media_count", access_token=token)
    return {
        "username": data.get("username") or data.get("name") or ig_id,
        "picture": data.get("profile_picture_url"),
        "followers": data.get("followers_count", 0),
        "media_count": data.get("media_count", 0),
    }


_MEDIA_FIELDS = ("id,caption,media_type,media_product_type,thumbnail_url,media_url,permalink,"
                 "timestamp,like_count,comments_count")


def _insight_value(item: dict, metric: str) -> int:
    for m in item.get("insights", {}).get("data", []):
        if m.get("name") == metric:
            if "total_value" in m:
                return int(m["total_value"].get("value") or 0)
            values = m.get("values") or [{}]
            return int(values[0].get("value") or 0)
    return 0


def recent_media(token: str, ig_id: str, limit: int = 24) -> list[dict]:
    """Posts recentes com views. Usa expansão de campo; se a Meta recusar, busca as views uma a uma."""
    try:
        items = _get(f"{ig_id}/media", fields=_MEDIA_FIELDS + ",insights.metric(views)",
                     limit=limit, access_token=token).get("data", [])
    except InstagramError:
        items = _get(f"{ig_id}/media", fields=_MEDIA_FIELDS, limit=limit, access_token=token).get("data", [])
        for item in items:
            try:
                item["insights"] = _get(f"{item['id']}/insights", metric="views", access_token=token)
            except InstagramError:
                pass  # posts antigos ou de tipos sem essa métrica
    return [
        {
            "id": it["id"],
            "caption": it.get("caption") or "",
            "type": it.get("media_product_type") or it.get("media_type", ""),
            "is_video": it.get("media_type") == "VIDEO",
            "thumb": it.get("thumbnail_url") or (it.get("media_url") if it.get("media_type") != "VIDEO" else None),
            "permalink": it.get("permalink"),
            "timestamp": it.get("timestamp", ""),
            "likes": it.get("like_count", 0),
            "comments": it.get("comments_count", 0),
            "views": _insight_value(it, "views"),
        }
        for it in items
    ]


# ---------- Publicação ----------

def publish_reel(token: str, ig_id: str, path: Path, caption: str) -> tuple[str, str | None]:
    """Envia o vídeo do PC como Reel e publica. Retorna (id da mídia, link do post)."""
    if not path.exists():
        raise InstagramError("Arquivo de vídeo não encontrado no PC.")
    container = _post(f"{ig_id}/media", media_type="REELS", upload_type="resumable",
                      caption=caption[:CAPTION_LIMIT], access_token=token)
    cid = container["id"]
    upload_uri = container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{META_API_VERSION}/{cid}"

    with open(path, "rb") as f:
        resp = httpx.post(
            upload_uri,
            headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(path.stat().st_size)},
            content=f,
            timeout=httpx.Timeout(60, write=None, read=600),
        )
    result = _check(resp)
    if result.get("success") is False:
        raise InstagramError(f"Falha no envio do vídeo: {result.get('message') or result}")

    # O Instagram processa o vídeo antes de permitir a publicação.
    deadline = time.monotonic() + PROCESSING_TIMEOUT
    while True:
        status = _get(cid, fields="status_code,status", access_token=token)
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError(f"O Instagram recusou o vídeo: {status.get('status') or code}. "
                                 "Reels precisam ser MP4/MOV, de 3 s a 15 min, de preferência 9:16.")
        if time.monotonic() > deadline:
            raise InstagramError("O Instagram demorou demais para processar o vídeo. Tente de novo mais tarde.")
        time.sleep(10)

    media_id = _post(f"{ig_id}/media_publish", creation_id=cid, access_token=token)["id"]
    try:
        permalink = _get(media_id, fields="permalink", access_token=token).get("permalink")
    except InstagramError:
        permalink = None
    return media_id, permalink
