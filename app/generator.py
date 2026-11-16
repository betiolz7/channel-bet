"""Cliente da API do MoneyPrinterTurbo (https://github.com/harry0703/MoneyPrinterTurbo).

O MoneyPrinterTurbo roda como serviço separado (padrão: porta 8080) e gera vídeos
curtos a partir de um tema: roteiro por LLM, imagens de banco, narração e legendas.
"""

from pathlib import Path

import httpx

from .config import MPT_URL

STATE_FAILED = -1
STATE_COMPLETE = 1

VOICES = [
    ("pt-BR-AntonioNeural-Male", "Antonio (masculina, PT-BR)"),
    ("pt-BR-FranciscaNeural-Female", "Francisca (feminina, PT-BR)"),
    ("pt-BR-ThalitaNeural-Female", "Thalita (feminina, PT-BR)"),
    ("en-US-GuyNeural-Male", "Guy (masculina, EN-US)"),
    ("en-US-JennyNeural-Female", "Jenny (feminina, EN-US)"),
]


class GeneratorOffline(RuntimeError):
    pass


def _unwrap(resp: httpx.Response) -> dict:
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") not in (200, None):
        raise RuntimeError(payload.get("message") or "Erro no MoneyPrinterTurbo")
    return payload.get("data") or {}


def submit(subject: str, script: str, aspect: str, voice: str) -> str:
    params = {
        "video_subject": subject,
        "video_script": script,
        "video_aspect": aspect,
        "video_count": 1,
        "voice_name": voice,
        "subtitle_enabled": True,
    }
    try:
        data = _unwrap(httpx.post(f"{MPT_URL}/api/v1/videos", json=params, timeout=30))
    except httpx.ConnectError as e:
        raise GeneratorOffline(f"MoneyPrinterTurbo não está rodando em {MPT_URL}") from e
    task_id = data.get("task_id")
    if not task_id:
        raise RuntimeError("MoneyPrinterTurbo não devolveu task_id")
    return task_id


def get_task(task_id: str) -> dict:
    try:
        return _unwrap(httpx.get(f"{MPT_URL}/api/v1/tasks/{task_id}", timeout=30))
    except httpx.ConnectError as e:
        raise GeneratorOffline(f"MoneyPrinterTurbo não está rodando em {MPT_URL}") from e


def video_urls(task: dict) -> list[str]:
    urls = task.get("videos") or task.get("combined_videos") or []
    return [u if u.startswith("http") else f"{MPT_URL}/{u.lstrip('/')}" for u in urls]


def download(url: str, dest: Path) -> None:
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1024 * 1024):
                f.write(chunk)
