"""vocero-crm 071 (D1, D2) — Turnos concurrentes, serializados por conversación.

La cola reclama el despacho más viejo sin terminar de cada conversación
`(organización, conversación)` y solo si ninguno de ella está en curso. El
worker corre `DISPATCH_CONCURRENCY` consumidores y ningún negocio ocupa más
de `DISPATCH_MAX_POR_ORG`.

Estas pruebas van contra `MemoryDispatchStore`; las mismas propiedades contra
Postgres (dos «réplicas», empates de `created_at`) están en
`tests/test_pg_store.py`.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from app import dispatch_worker
from app.state import MemoryStore
from tests.conftest import make_ctx, make_settings


def _despacho(org: str, conv: str, n: int) -> dict[str, Any]:
    return {
        "dispatchId": f"dsp_{org}_{conv}_{n}",
        "organization": {"id": org, "slug": org},
        "conversation": {"id": conv},
        "contact": {"identity": f"57300{conv}"},
        "messages": [{"id": f"m_{conv}_{n}", "type": "text", "text": "hola"}],
    }


def _olvido(org: str, conv: str) -> dict[str, Any]:
    return {
        "type": "conversation.deleted",
        "eventId": f"olv_{conv}",
        "dispatchId": f"olvido:olv_{conv}",
        "organization": {"id": org, "slug": org},
        "conversation": {"id": conv},
        "contact": {"identity": f"57300{conv}"},
    }


async def test_dos_conversaciones_del_mismo_negocio_corren_a_la_vez():
    store = MemoryStore()
    await store.enqueue_dispatch("org_a", _despacho("org_a", "cv_1", 1))
    await store.enqueue_dispatch("org_a", _despacho("org_a", "cv_2", 1))
    async with store.claim_dispatch() as uno:
        async with store.claim_dispatch() as otro:
            assert uno and otro
            assert {uno.conversation_id, otro.conversation_id} == {"cv_1", "cv_2"}
            await otro.finish()
        await uno.finish()


async def test_la_misma_conversacion_espera_y_conserva_el_orden():
    store = MemoryStore()
    for n in (1, 2, 3):
        await store.enqueue_dispatch("org_a", _despacho("org_a", "cv_1", n))
    async with store.claim_dispatch() as primero:
        assert primero.payload["dispatchId"] == "dsp_org_a_cv_1_1"
        async with store.claim_dispatch() as nada:
            assert nada is None  # el 2 y el 3 esperan al 1
        await primero.finish()
    async with store.claim_dispatch() as segundo:
        assert segundo.payload["dispatchId"] == "dsp_org_a_cv_1_2"
        await segundo.finish()


async def test_el_olvido_espera_al_turno_en_vuelo_de_su_conversacion():
    """Verificación pedida al aprobar D1: `conversation.deleted` queda detrás
    de cualquier turno en vuelo de esa (organización, conversación), y otra
    conversación del mismo negocio no lo adelanta ni lo bloquea."""
    store = MemoryStore()
    await store.enqueue_dispatch("org_a", _despacho("org_a", "cv_1", 1))
    async with store.claim_dispatch() as turno:
        assert turno.conversation_id == "cv_1"
        # Llega el borrado MIENTRAS el turno corre, y un mensaje de otra conversación.
        await store.enqueue_dispatch("org_a", _olvido("org_a", "cv_1"))
        await store.enqueue_dispatch("org_a", _despacho("org_a", "cv_2", 1))
        async with store.claim_dispatch() as siguiente:
            assert siguiente.conversation_id == "cv_2"  # el olvido no se cuela
            await siguiente.finish()
        async with store.claim_dispatch() as nada:
            assert nada is None
        await turno.finish()
    async with store.claim_dispatch() as olvido:
        assert olvido.payload["type"] == "conversation.deleted"
        await olvido.finish()


async def test_un_mensaje_que_llega_tras_el_olvido_va_despues_de_el():
    store = MemoryStore()
    await store.enqueue_dispatch("org_a", _olvido("org_a", "cv_1"))
    await store.enqueue_dispatch("org_a", _despacho("org_a", "cv_1", 9))
    async with store.claim_dispatch() as primero:
        assert primero.payload["type"] == "conversation.deleted"
        async with store.claim_dispatch() as nada:
            assert nada is None
        await primero.finish()
    async with store.claim_dispatch() as luego:
        assert luego.payload["dispatchId"] == "dsp_org_a_cv_1_9"


async def test_un_negocio_al_tope_deja_pasar_a_otro():
    store = MemoryStore()
    await store.enqueue_dispatch("org_a", _despacho("org_a", "cv_1", 1))
    await store.enqueue_dispatch("org_b", _despacho("org_b", "cv_9", 1))
    async with store.claim_dispatch(excluir=["org_a"]) as job:
        assert job.organization_id == "org_b"


async def test_lease_vencido_se_recupera_y_lo_mas_nuevo_espera_detras():
    store = MemoryStore()
    await store.enqueue_dispatch("org_a", _despacho("org_a", "cv_1", 1))
    await store.enqueue_dispatch("org_a", _despacho("org_a", "cv_1", 2))
    async with store.claim_dispatch() as job:
        assert not job.recovery  # el proceso «muere» sin finish
    fila = store.dispatch_inbox[("org_a", "dsp_org_a_cv_1_1")]
    fila["available_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    async with store.claim_dispatch() as recuperado:
        assert recuperado.payload["dispatchId"] == "dsp_org_a_cv_1_1"
        assert recuperado.recovery
        await recuperado.finish()


async def test_un_finish_tardio_no_pisa_la_recuperacion():
    store = MemoryStore()
    await store.enqueue_dispatch("org_a", _despacho("org_a", "cv_1", 1))
    async with store.claim_dispatch() as viejo:
        pass
    store.dispatch_inbox[("org_a", "dsp_org_a_cv_1_1")]["available_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    async with store.claim_dispatch() as nuevo:
        assert nuevo.recovery
        try:
            await viejo.finish()
        except RuntimeError as exc:
            assert "ownership" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("el finish del dueño viejo no debía valer")
        await nuevo.finish()


async def test_el_worker_corre_en_paralelo_con_tope_por_negocio(monkeypatch):
    """8 conversaciones de org_a y 2 de org_b, 2 mensajes cada una. Con 4
    consumidores y tope 2: org_a nunca pasa de 2, hay paralelismo real, la
    misma conversación nunca corre dos veces a la vez y su orden se respeta."""
    ctx = make_ctx(make_settings(dispatch_concurrency=4, dispatch_max_por_org=2))
    for conv in range(8):
        for n in (1, 2):
            await ctx.store.enqueue_dispatch("org_a", _despacho("org_a", f"a{conv}", n))
    for conv in range(2):
        for n in (1, 2):
            await ctx.store.enqueue_dispatch("org_b", _despacho("org_b", f"b{conv}", n))

    en_curso: Counter[str] = Counter()
    por_conv: Counter[str] = Counter()
    maximo: dict[str, int] = defaultdict(int)
    orden: dict[str, list[str]] = defaultdict(list)
    total = 0

    async def procesar(ctx: Any, payload: dict[str, Any], *, durable: bool = False) -> None:
        nonlocal total
        org = payload["organization"]["id"]
        conv = payload["conversation"]["id"]
        en_curso[org] += 1
        en_curso["*"] += 1
        por_conv[conv] += 1
        assert por_conv[conv] == 1, f"{conv} corrió dos veces a la vez"
        maximo[org] = max(maximo[org], en_curso[org])
        maximo["*"] = max(maximo["*"], en_curso["*"])
        await asyncio.sleep(0.02)
        orden[conv].append(payload["dispatchId"])
        por_conv[conv] -= 1
        en_curso[org] -= 1
        en_curso["*"] -= 1
        total += 1

    monkeypatch.setattr("app.dispatch._procesar", procesar)
    tarea = asyncio.create_task(dispatch_worker.run(ctx))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if total == 20:
            break
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)
    await ctx.crm.aclose()

    assert total == 20
    assert maximo["org_a"] == 2  # el tope por negocio
    assert maximo["*"] >= 3  # org_b entró mientras org_a estaba al tope
    for conv, ids in orden.items():
        assert ids == sorted(ids), conv
