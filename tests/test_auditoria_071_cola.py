"""Auditoría 071 — regresiones de la cola y del envío diferido.

A-1: con un reclamo que tarda (como contra Postgres), ocho consumidores ya no
se llevan ocho turnos del mismo negocio: el tope `DISPATCH_MAX_POR_ORG` se
respeta.
A-3: el sender reserva lotes chicos y no envía una fila cuyo lease ya venció
(otra réplica podría haberla tomado).
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from app import dispatch_worker
from app.sender import SenderWorker
from app.state import utcnow
from tests.conftest import make_ctx, make_settings


def _despacho(org: str, conv: str) -> dict[str, Any]:
    return {
        "dispatchId": f"dsp_{org}_{conv}",
        "organization": {"id": org, "slug": org},
        "conversation": {"id": conv},
        "contact": {"identity": f"57300{conv}"},
        "messages": [{"id": f"m_{conv}", "type": "text", "text": "hola"}],
    }


def _reclamo_lento(store: Any) -> None:
    """El reclamo cede el loop antes de leer, como un viaje a Postgres."""
    original = store.claim_dispatch

    @asynccontextmanager
    async def lento(excluir=()):
        await asyncio.sleep(0.01)
        async with original(excluir) as job:
            yield job

    store.claim_dispatch = lento


async def test_a1_ocho_consumidores_respetan_el_tope_por_negocio(monkeypatch):
    ctx = make_ctx(make_settings(dispatch_concurrency=8, dispatch_max_por_org=3))
    _reclamo_lento(ctx.store)
    for conv in range(8):
        await ctx.store.enqueue_dispatch("org_a", _despacho("org_a", f"a{conv}"))

    en_curso = 0
    maximo = 0
    hechos = 0

    async def procesar(ctx: Any, payload: dict[str, Any], *, durable: bool = False) -> None:
        nonlocal en_curso, maximo, hechos
        en_curso += 1
        maximo = max(maximo, en_curso)
        await asyncio.sleep(0.05)
        en_curso -= 1
        hechos += 1

    monkeypatch.setattr("app.dispatch._procesar", procesar)
    en_vuelo: Counter[str] = Counter()
    # Los ocho a la vez, como los consumidores al despertar con la ráfaga.
    while hechos < 8:
        await asyncio.gather(*(dispatch_worker.drain_one(ctx, en_vuelo) for _ in range(8)))
    await ctx.crm.aclose()

    assert hechos == 8
    assert maximo == 3, f"el tope por negocio se rebasó: {maximo}"
    assert not en_vuelo  # el contador vuelve a cero


async def test_a1_sin_tope_no_hace_falta_candado(monkeypatch):
    ctx = make_ctx()
    await ctx.store.enqueue_dispatch("org_a", _despacho("org_a", "a1"))

    async def procesar(ctx: Any, payload: dict[str, Any], *, durable: bool = False) -> None:
        return None

    monkeypatch.setattr("app.dispatch._procesar", procesar)
    assert await dispatch_worker.drain_one(ctx) is True
    assert await dispatch_worker.drain_one(ctx) is False
    await ctx.crm.aclose()


# ──────────────────────────────────────────────────────────── A-3 ───


async def test_a3_no_envia_una_fila_cuyo_lease_ya_vencio(monkeypatch):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation("573001112233")
    for i in range(3):
        await ctx.store.enqueue_pending_send(conv.id, "cv_1", f"texto {i}", "", "", "dsp_1")
    for p in ctx.store.pending_sends.values():
        p.next_retry_at = utcnow() - timedelta(seconds=1)

    enviados: list[str] = []

    async def enviar_lento(conversation_id: str, text: str, dispatch_id: str = "") -> dict:
        enviados.append(text)
        await asyncio.sleep(0.05)
        return {"messageId": "m"}

    monkeypatch.setattr(ctx.crm, "send_message", enviar_lento)

    # Lease corto: tras el primer envío ya no queda margen para el siguiente.
    original = ctx.store.claim_pending_sends

    async def reclamar(now, limit=10, lease_seconds=120):
        return await original(now, limit=limit, lease_seconds=1)

    monkeypatch.setattr(ctx.store, "claim_pending_sends", reclamar)
    monkeypatch.setattr("app.sender.MARGEN_DEL_LEASE", timedelta(seconds=0.97))
    await SenderWorker(ctx).tick()
    await ctx.crm.aclose()
    assert enviados == ["texto 0"]
    # Las demás siguen pendientes: vuelven cuando venza su lease.
    pendientes = [p for p in ctx.store.pending_sends.values() if p.delivered_at is None]
    assert len(pendientes) == 2


def test_a3_lote_chico_y_lease_que_cubre_el_lote():
    import inspect

    from app.db import PgStore
    from app.sender import LEASE_SECONDS, LOTE

    assert LOTE <= 10
    assert LEASE_SECONDS >= LOTE * 15
    fuente = inspect.getsource(PgStore.claim_pending_sends)
    assert "$1 + make_interval" in fuente and "now() +" not in fuente
