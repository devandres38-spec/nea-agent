"""Parche final de la auditoría 073-C (2 ALTO, 2 MEDIO, 1 BAJO)."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app import turn
from app.confirmacion import _lo_presencial
from app.llm import LlmReply
from app.validador import promete_handoff, revisar
from tests.conftest import despachar, mock_crm_basics


def _textos(ruta: Any) -> list[str]:
    return [json.loads(c.request.content)["text"] for c in ruta.calls]


# ── ALTO 1: subjuntivo ≠ pretérito ────────────────────────────────────────


@pytest.mark.parametrize(
    "texto",
    [
        "¿Quieres que te agende el lunes?",
        "¿Te agende el lunes a las 3:00 p. m.?",
        "Para que te reserve, dime tu nombre completo.",
        "Dime qué día te sirve y que te separe el cupo.",
        "¿Ya te agendé? Déjame revisar.",
        "¿Tu cita quedó confirmada? Te cuento en un momento.",
    ],
)
def test_preguntas_y_subjuntivos_no_son_cita_prometida(texto: str) -> None:
    assert revisar(texto, reservo=False) is None


@pytest.mark.parametrize(
    "texto",
    [
        "Listo, ya te agendé el lunes a las 3:00 p. m.",
        "Te reservé el cupo del martes.",
        "Perfecto. Tu cita quedó agendada para el jueves.",
        "Ya quedó confirmada, nos vemos.",
        # NFD (tilde como carácter combinante): sigue siendo pretérito.
        "Ya te agendé el lunes.",
    ],
)
def test_el_preterito_con_tilde_sigue_siendo_promesa(texto: str) -> None:
    assert revisar(texto, reservo=False) is not None
    assert revisar(texto, reservo=True) is None


# ── ALTO 2: preguntas y condicionales no son handoff ──────────────────────


@pytest.mark.parametrize(
    "texto",
    [
        "¿Te paso con un asesor?",
        "¿Quieres que te paso con un asesor del equipo?",
        "Si quieres, te paso con un asesor.",
        "Te paso con un asesor si lo prefieres.",
        "Si necesitas, un asesor te va a escribir.",
        "Si prefieres hablar con una persona, avísame.",
    ],
)
def test_preguntas_y_ofertas_no_son_promesa_de_asesor(texto: str) -> None:
    assert promete_handoff(texto) is False


@pytest.mark.parametrize(
    "texto",
    [
        "Claro, ya te paso con un asesor.",
        "Un asesor te va a escribir en unos minutos.",
        "Una asesora te va a atender enseguida.",
        "Nuestro equipo te va a escribir hoy mismo.",
        "Listo. ¿Algo más? Te paso con un asesor.",
    ],
)
def test_promesas_afirmativas_de_asesor(texto: str) -> None:
    assert promete_handoff(texto) is True


async def test_preguntar_si_lo_pasa_con_un_asesor_no_dispara_handoff(ctx, client, respx_mock) -> None:
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="Entiendo. ¿Te paso con un asesor?")]
    await despachar(client, ctx, text="no me queda claro el precio", esperar=True)
    assert rutas["messages"].call_count == 1
    assert rutas["handoff"].call_count == 0


async def test_preguntar_si_lo_agenda_no_pide_correccion(ctx, client, respx_mock) -> None:
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="¡Claro! ¿Quieres que te agende el lunes?")]
    await despachar(client, ctx, text="quiero una cita", esperar=True)
    assert len(ctx.llm.calls) == 1
    assert _textos(rutas["messages"]) == ["¡Claro! ¿Quieres que te agende el lunes?"]


# ── MEDIO 3: un trozo que falla no se lleva los demás ─────────────────────


async def test_trozo_encolado_encola_los_que_siguen_en_orden(ctx, client, respx_mock, monkeypatch) -> None:
    async def sin_espera(_s: float) -> None:
        return None

    monkeypatch.setattr(turn.asyncio, "sleep", sin_espera)
    rutas = mock_crm_basics(respx_mock)
    llamadas = {"n": 0}

    def primero_bien_luego_caido(request: httpx.Request) -> httpx.Response:
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return httpx.Response(200, json={"messageId": "msg_1"})
        return httpx.Response(503, json={"error": {"code": "meta_unavailable"}})

    rutas["messages"].mock(side_effect=primero_bien_luego_caido)
    parrafos = [f"Párrafo {i}. " + "detalle del servicio " * 40 for i in range(3)]
    ctx.llm.replies = [LlmReply(content="\n\n".join(parrafos))]
    await despachar(client, ctx, text="cuéntame todo", esperar=True, channel="instagram")

    enviados = _textos(rutas["messages"])
    assert enviados[0].startswith("Párrafo 0.")
    pendientes = sorted(ctx.store.pending_sends.values(), key=lambda p: p.id)
    assert [p.content.split(".")[0] for p in pendientes] == ["Párrafo 1", "Párrafo 2"]
    conv = next(iter(ctx.store.conversations.values()))
    historial = [m.content for m in await ctx.store.recent_messages(conv.id, 10) if m.role == "assistant"]
    # Solo lo que salió va al historial; lo encolado entra cuando se entregue.
    assert historial and historial[-1].startswith("Párrafo 0.") and "Párrafo 1" not in historial[-1]


async def test_trozo_bloqueado_por_el_crm_corta_sin_encolar(ctx, client, respx_mock) -> None:
    rutas = mock_crm_basics(respx_mock)
    llamadas = {"n": 0}

    def primero_bien_luego_pausa(request: httpx.Request) -> httpx.Response:
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return httpx.Response(200, json={"messageId": "msg_1"})
        return httpx.Response(409, json={"error": {"code": "ai_paused"}})

    rutas["messages"].mock(side_effect=primero_bien_luego_pausa)
    parrafos = [f"Párrafo {i}. " + "detalle del servicio " * 40 for i in range(3)]
    ctx.llm.replies = [LlmReply(content="\n\n".join(parrafos))]
    await despachar(client, ctx, text="cuéntame todo", esperar=True, channel="instagram")

    assert rutas["messages"].call_count == 2
    assert ctx.store.pending_sends == {}


# ── MEDIO 4: una foto no se descarta por obsoleta ─────────────────────────


async def test_texto_sin_efectos_con_despacho_nuevo_se_descarta(ctx, client, respx_mock, monkeypatch) -> None:
    monkeypatch.setattr(turn, "hay_despacho_mas_nuevo", AsyncMock(return_value=True))
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="¡Hola! ¿En qué te ayudo?")]
    await despachar(client, ctx, text="hola", esperar=True)
    assert rutas["messages"].call_count == 0


async def test_una_foto_nunca_se_descarta_por_obsoleta(ctx, client, respx_mock, monkeypatch) -> None:
    monkeypatch.setattr(turn, "hay_despacho_mas_nuevo", AsyncMock(return_value=True))
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="Ya veo la foto: es el modelo azul.")]
    await despachar(
        client, ctx, esperar=True,
        mensajes=[{"id": "wamid.f", "at": "2026-11-20T10:00:00Z", "type": "image", "text": None,
                   "mediaId": "m1", "mimeType": "image/jpeg", "caption": None}],
    )
    assert _textos(rutas["messages"]) == ["Ya veo la foto: es el modelo azul."]


# ── BAJO 5: Maps sin duplicar ─────────────────────────────────────────────


def test_maps_repetido_sale_una_sola_vez() -> None:
    texto = (
        "Te esperamos en https://maps.app.goo.gl/abc123\n"
        "De nuevo el mapa: https://maps.app.goo.gl/abc123"
    )
    extra = _lo_presencial(texto, {})
    assert extra.count("📍 https://maps.app.goo.gl/abc123") == 1
