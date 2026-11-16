"""Loop em segundo plano: uploads da fila, acompanhamento de gerações e coleta de estatísticas."""

import asyncio
import logging

from . import services
from .config import WORKER_INTERVAL_SECONDS
from .db import SessionLocal

log = logging.getLogger("channelbet")


def run_once() -> None:
    with SessionLocal() as db:
        services.process_generation_jobs(db)
        # Um upload por ciclo para não segurar o loop por muito tempo.
        services.process_next_upload(db)
        services.refresh_all_stats(db, only_stale=True)


async def worker_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(run_once)
        except Exception:
            log.exception("Erro no worker")
        try:
            await asyncio.wait_for(stop.wait(), timeout=WORKER_INTERVAL_SECONDS)
        except TimeoutError:
            pass
