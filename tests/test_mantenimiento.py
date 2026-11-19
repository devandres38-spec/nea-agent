"""vocero-crm 071 (D7) — Retención: lo viejo se borra; lo vivo, nunca."""
from __future__ import annotations

from datetime import timedelta

from app.mantenimiento import MantenimientoWorker
from app.state import utcnow
from tests.conftest import CRM_CONV_ID, IDENTITY, make_ctx, make_settings


def _despacho(n: int) -> dict:
    return {"dispatchId": f"dsp_{n}", "organization": {"id": "org_a", "slug": "a"},
            "conversation": {"id": f"cv_{n}"}, "contact": {"identity": "57300"},
            "messages": [{"id": f"m{n}", "type": "text", "text": "hola"}]}


async def test_purga_solo_lo_terminado_y_viejo():
    ctx = make_ctx(make_settings(retention_days=14, pending_retention_days=30))
    store = ctx.store
    for n in (1, 2, 3):
        await store.enqueue_dispatch("org_a", _despacho(n))
    async with store.claim_dispatch() as viejo:
        await viejo.finish()
    async with store.claim_dispatch() as reciente:
        await reciente.finish()
    filas = store.dispatch_inbox
    filas[("org_a", "dsp_1")]["done_at"] = utcnow() - timedelta(days=15)
    # dsp_3 sigue en cola: jamás se purga, por viejo que sea.

    conv = await store.get_or_create_conversation(IDENTITY)
    entregado = await store.enqueue_pending_send(conv.id, CRM_CONV_ID, "a")
    vivo = await store.enqueue_pending_send(conv.id, CRM_CONV_ID, "b")
    store.pending_sends[entregado].delivered_at = utcnow()
    for pid in (entregado, vivo):
        store.pending_sends[pid].created_at = utcnow() - timedelta(days=40)

    borradas = await MantenimientoWorker(ctx).tick()

    assert borradas == {"dispatch_inbox": 1, "processed_message": 0, "pending_send": 1}
    assert set(filas) == {("org_a", "dsp_2"), ("org_a", "dsp_3")}
    assert set(store.pending_sends) == {vivo}  # sin entregar: se queda
    await ctx.crm.aclose()


def test_la_retencion_no_baja_de_dos_dias():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_settings(retention_days=1)
