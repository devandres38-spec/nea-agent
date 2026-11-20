"""Auditoría 073-B — regresiones del parche (lado Nea).

1. CRÍTICO: tras `solicitar_baja` el bucle corta en el acto: ni otra vuelta
   al modelo, ni más herramientas del lote, ni un `handoff` que pise `opt_out`.
2. MEDIO: el `summary` sobrevive a la cola (`pending_send` de tipo handoff),
   con lectura compatible de las filas viejas en texto plano.
3. BAJO: el pie de foto va enmarcado como los demás datos («…»).
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app import turn
from app.llm import LlmReply, ToolCall
from app.media import _caption_line
from app.sender import SenderWorker
from app.state import KIND_HANDOFF, InboundMessage, contenido_de_handoff, leer_handoff, utcnow
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, despachar, make_ctx, mock_crm_basics


def _cuerpo(ruta: Any, i: int = 0) -> dict[str, Any]:
    return json.loads(ruta.calls[i].request.content)


# ── 1. Baja: corte inmediato ──────────────────────────────────────────────


async def test_tras_solicitar_baja_no_hay_otra_vuelta_al_modelo(ctx, client, respx_mock):
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="b1", name="solicitar_baja", arguments={})]),
        # Un modelo que «obedece» a medias: escribiría y movería la ficha.
        LlmReply(
            content="¡Listo, te di de baja! Por cierto, ¿te interesa la promo?",
            tool_calls=[ToolCall(id="f1", name="update_ficha", arguments={"interes": "promo"})],
        ),
        LlmReply(content="Último mensaje que no debe salir."),
    ]
    await despachar(client, ctx, text="no quiero que me escriban, bórrenme", esperar=True)

    assert len(ctx.llm.calls) == 1  # nada después de la baja
    assert rutas["messages"].call_count == 0
    assert rutas["ficha"].call_count == 0
    assert rutas["handoff"].call_count == 1
    assert _cuerpo(rutas["handoff"])["reason"] == "opt_out"


async def test_las_herramientas_del_lote_despues_de_la_baja_no_corren(ctx, client, respx_mock):
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[
                ToolCall(id="b1", name="solicitar_baja", arguments={}),
                ToolCall(id="f1", name="update_ficha", arguments={"interes": "nada"}),
                ToolCall(id="h1", name="handoff", arguments={"reason": "pidió humano", "resumen": "x"}),
            ],
        ),
    ]
    await despachar(client, ctx, text="denme de baja ya", esperar=True)

    assert rutas["ficha"].call_count == 0
    assert rutas["handoff"].call_count == 1
    cuerpo = _cuerpo(rutas["handoff"])
    assert cuerpo["reason"] == "opt_out" and "summary" not in cuerpo


async def test_un_handoff_antes_de_la_baja_no_la_pisa(ctx, client, respx_mock):
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[
                ToolCall(id="h1", name="handoff", arguments={"reason": "pidió humano", "resumen": "quiere irse"}),
                ToolCall(id="b1", name="solicitar_baja", arguments={}),
            ],
        ),
    ]
    await despachar(client, ctx, text="no me escriban más y pásenme con alguien", esperar=True)
    assert _cuerpo(rutas["handoff"])["reason"] == "opt_out"
    assert rutas["messages"].call_count == 0


def test_handoff_despues_de_la_baja_no_cambia_el_motivo():
    from app.tools import ToolRuntime

    rt = ToolRuntime.__new__(ToolRuntime)
    rt.baja = True
    rt.handoff_reason = "opt_out"
    rt.handoff_summary = None
    rt._handoff({"reason": "pidió humano", "resumen": "algo"})
    assert rt.handoff_reason == "opt_out" and rt.handoff_summary is None


# ── 2. Resumen en la cola ─────────────────────────────────────────────────


def test_contenido_de_handoff_es_retrocompatible():
    assert contenido_de_handoff("error") == "error"
    assert leer_handoff("error") == ("error", None)
    assert leer_handoff("") == ("error", None)
    assert leer_handoff(None) == ("error", None)
    assert leer_handoff(contenido_de_handoff("cliente", "Quiere precio")) == ("cliente", "Quiere precio")
    assert leer_handoff("{roto") == ("{roto", None)
    assert len(leer_handoff(contenido_de_handoff("cliente", "x" * 900))[1] or "") == 500


async def test_el_resumen_sobrevive_si_el_handoff_cae_a_la_cola(respx_mock, monkeypatch):
    async def nada(_s: float) -> None:
        return None

    monkeypatch.setattr(turn.asyncio, "sleep", nada)
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    handoff = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(return_value=httpx.Response(503, json={}))

    await turn._safe_handoff(ctx, CRM_CONV_ID, "cliente", None, conv.id, resumen="Quiere ortodoncia; falta el día")
    pendiente = next(p for p in ctx.store.pending_sends.values() if p.kind == KIND_HANDOFF)
    assert leer_handoff(pendiente.content) == ("cliente", "Quiere ortodoncia; falta el día")

    handoff.mock(return_value=httpx.Response(200, json={}))
    await SenderWorker(ctx).tick(utcnow())
    cuerpo = _cuerpo(handoff, -1)
    assert cuerpo["reason"] == "cliente" and cuerpo["summary"] == "Quiere ortodoncia; falta el día"
    await ctx.crm.aclose()


async def test_una_fila_vieja_en_texto_plano_sale_igual(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "error", kind=KIND_HANDOFF)
    handoff = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(return_value=httpx.Response(200, json={}))
    await SenderWorker(ctx).tick(utcnow())
    assert _cuerpo(handoff) == {"conversationId": CRM_CONV_ID, "reason": "error"}
    await ctx.crm.aclose()


# ── 3. Pie de foto ────────────────────────────────────────────────────────


def test_el_pie_de_foto_va_entre_comillas_latinas():
    msg = InboundMessage(wa_message_id="w", identity="i", type="image", media_caption="mi «foto» [x]")
    assert _caption_line(msg, "la imagen") == ' Nota del lead junto a la imagen: «mi "foto" (x)».'
