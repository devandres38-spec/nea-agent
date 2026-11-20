"""Auditoría 073-A — regresiones del parche corto (lado Nea).

1. ALTO: el handoff tras abandonar una respuesta no se pierde si el CRM no
   contesta: queda en `pending_send` como `kind=handoff` (24 h).
2. MEDIO: `Retry-After` acepta fecha HTTP, rechaza no finitos y siempre queda
   en `[0, RETRY_AFTER_MAX_SEGUNDOS]`.
3. MEDIO: la marca de efectos solo cuenta como hecha si la base la anotó.
4. BAJO: el ensayo respeta la `temperature` que manda el CRM.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import llm as llm_mod
from app.crm import CrmRechazo
from app.efectos import marcar_efecto, registrar_efectos
from app.multiorg import RegistroDeOrganizaciones
from app.sender import SenderWorker
from app.state import KIND_HANDOFF, KIND_MESSAGE, utcnow
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx


# ── 1. Handoff tras abandono ──────────────────────────────────────────────


def _handoffs_encolados(ctx) -> list:
    return [p for p in ctx.store.pending_sends.values() if p.kind == KIND_HANDOFF]


async def test_respuesta_caducada_y_crm_caido_encola_el_handoff(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "vieja", "org_1", "acme", "dsp_1")
    ctx.store.pending_sends[pid].created_at = utcnow() - timedelta(minutes=16)
    respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(return_value=httpx.Response(503, json={}))

    await SenderWorker(ctx).tick(utcnow())

    assert ctx.store.pending_sends[pid].abandoned_at is not None
    encolados = _handoffs_encolados(ctx)
    assert len(encolados) == 1
    h = encolados[0]
    assert (h.content, h.crm_conversation_id, h.conversation_id) == ("error", CRM_CONV_ID, conv.id)
    assert (h.organization_id, h.organization_slug, h.dispatch_id) == ("org_1", "acme", "dsp_1")
    await ctx.crm.aclose()


async def test_rechazo_terminal_y_crm_caido_encola_el_handoff(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "respuesta")
    respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(return_value=httpx.Response(422, json={}))
    respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(side_effect=httpx.ConnectError("caído"))

    await SenderWorker(ctx).tick(utcnow())

    assert ctx.store.pending_sends[pid].abandoned_at is not None
    assert len(_handoffs_encolados(ctx)) == 1
    await ctx.crm.aclose()


async def test_el_handoff_encolado_sale_en_el_siguiente_barrido(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "vieja")
    ctx.store.pending_sends[pid].created_at = utcnow() - timedelta(minutes=16)
    handoff = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(
        side_effect=[httpx.Response(503, json={})] * 10 + [httpx.Response(200, json={})]
    )
    worker = SenderWorker(ctx)
    await worker.tick(utcnow())
    pendiente = _handoffs_encolados(ctx)[0]
    llamadas = handoff.call_count
    handoff.side_effect = None
    handoff.return_value = httpx.Response(200, json={})

    await worker.tick(utcnow() + timedelta(minutes=1))

    assert handoff.call_count == llamadas + 1
    assert json.loads(handoff.calls[-1].request.content)["reason"] == "error"
    assert ctx.store.pending_sends[pendiente.id].delivered_at is not None
    await ctx.crm.aclose()


async def test_sin_credencial_no_encola_el_handoff(respx_mock, monkeypatch):
    """`CrmRechazo` (cloud): tampoco habría credencial para el handoff encolado."""
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "vieja")
    ctx.store.pending_sends[pid].created_at = utcnow() - timedelta(minutes=16)
    monkeypatch.setattr(ctx.crm, "post_handoff", AsyncMock(side_effect=CrmRechazo(401)))

    await SenderWorker(ctx).tick(utcnow())

    assert _handoffs_encolados(ctx) == []
    assert all(p.kind == KIND_MESSAGE for p in ctx.store.pending_sends.values())
    await ctx.crm.aclose()


# ── 2. Retry-After ────────────────────────────────────────────────────────


def _exc(valor: str) -> Exception:
    exc = Exception("429")
    exc.response = SimpleNamespace(headers={"retry-after": valor})  # type: ignore[attr-defined]
    return exc


@pytest.mark.parametrize("valor", ["nan", "inf", "-inf", "NaN", "basura", ""])
def test_retry_after_ilegible_o_no_finito_es_none(valor: str) -> None:
    assert llm_mod._retry_after(_exc(valor)) is None


def test_retry_after_numerico_se_acota() -> None:
    assert llm_mod._retry_after(_exc("3")) == 3.0
    assert llm_mod._retry_after(_exc("1e9")) == llm_mod.RETRY_AFTER_MAX_SEGUNDOS
    assert llm_mod._retry_after(_exc("-5")) == 0.0


def test_retry_after_con_fecha_http() -> None:
    dentro_de_4 = datetime.now(timezone.utc) + timedelta(seconds=4)
    segundos = llm_mod._retry_after(_exc(format_datetime(dentro_de_4, usegmt=True)))
    assert segundos is not None and 2.0 <= segundos <= 4.0


def test_retry_after_con_fecha_lejana_o_pasada_queda_en_el_rango() -> None:
    lejos = datetime.now(timezone.utc) + timedelta(hours=2)
    pasada = datetime.now(timezone.utc) - timedelta(hours=2)
    assert llm_mod._retry_after(_exc(format_datetime(lejos, usegmt=True))) == llm_mod.RETRY_AFTER_MAX_SEGUNDOS
    assert llm_mod._retry_after(_exc(format_datetime(pasada, usegmt=True))) == 0.0


# ── 3. Marca de efectos ───────────────────────────────────────────────────


async def test_si_la_marca_falla_el_siguiente_efecto_la_reintenta() -> None:
    marcador = AsyncMock(side_effect=[RuntimeError("base caída"), None])
    with registrar_efectos(marcador):
        await marcar_efecto()  # falla: nunca lanza
        await marcar_efecto()  # vuelve a intentar y queda
        await marcar_efecto()  # ya está: no vuelve a escribir
    assert marcador.await_count == 2


# ── 4. Temperatura del ensayo ─────────────────────────────────────────────


def _por_defecto(**kw) -> SimpleNamespace:
    base = dict(
        llm_model="z-ai/glm-5.3-flash",
        llm_reasoning_effort="minimal",
        llm_provider_sort="throughput",
        llm_timeout_seconds=30.0,
        llm_max_tokens=2048,
        llm_temperature=0.4,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("config", "esperada"),
    [
        ({"temperature": 0.7}, 0.7),
        ({"temperature": 0}, 0.0),
        ({}, 0.4),
        ({"temperature": "alta"}, 0.4),
        ({"temperature": True}, 0.4),
        ({"temperature": 9}, 0.4),
    ],
)
async def test_el_ensayo_respeta_la_temperatura_del_crm(config, esperada) -> None:
    reg = RegistroDeOrganizaciones(CRM_URL, "s", agenda_ttl=60)
    try:
        llm = reg.llm_de_ensayo("org_1", "acme", config, _por_defecto(), "ficha")
        assert llm._temperature == esperada
        # La misma regla que el turno: el dueño prueba al agente de verdad.
        assert reg.llm("org_1", "acme", config, _por_defecto())._temperature == esperada
    finally:
        await reg.aclose()

