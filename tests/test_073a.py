"""vocero-crm 073-A — Cero silencios y cero apagones (lado Nea).

- A1: rondas de herramientas agotadas → una respuesta más SIN herramientas; si
  ni así hay texto, handoff `error` (nunca silencio negro). Errores terminales
  del proveedor sin reintentos; 429/5xx con jitter y `Retry-After`;
  `temperature`; 402 → handoff `sin_saldo`.
- A2: handoff con reintentos y cola durable; envío 4xx terminal; `pending_send`
  de 15 min.
- A3: marca de efectos, reanudación de turnos interrumpidos sin efectos y
  cierre ordenado.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from app import llm as llm_mod
from app import turn
from app.crm import canonical_handoff_reason, envio_terminal, error_de_envio
from app.dispatch_worker import _consumidor, drain_one, puede_reanudar
from app.efectos import marcar_efecto, registrar_efectos
from app.llm import LlmExhausted, LlmRechazado, LlmReply, OpenAiLlm, ToolCall
from app.main import _apagar_con_orden
from app.profile import profile_from_payload, temperatura_valida
from app.sender import SenderWorker
from app.state import KIND_HANDOFF, InboundMessage, utcnow
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    IDENTITY,
    despachar,
    make_ctx,
    make_settings,
    mock_crm_basics,
)

URL = "https://prov.test/chat/completions"


@pytest.fixture
def sin_esperas(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Anula los sleeps (turno y LLM) y registra cuánto se iba a esperar."""
    esperas: list[float] = []

    async def anotar(segundos: float) -> None:
        esperas.append(segundos)

    monkeypatch.setattr(turn.asyncio, "sleep", anotar)
    monkeypatch.setattr(llm_mod.asyncio, "sleep", anotar)
    return esperas


def _herramienta(i: int) -> LlmReply:
    return LlmReply(
        content=None,
        tool_calls=[ToolCall(id=f"t{i}", name="herramienta_que_no_existe", arguments={})],
    )


def _cuerpo(ruta: Any, i: int = 0) -> dict[str, Any]:
    return json.loads(ruta.calls[i].request.content)


# ── A1: bucle de herramientas ─────────────────────────────────────────────


async def test_rondas_agotadas_piden_una_respuesta_sin_herramientas(ctx, client, respx_mock):
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [_herramienta(i) for i in range(turn.MAX_TOOL_ROUNDS)] + [
        LlmReply(content="Te cuento: el plan básico cuesta 150.000 COP al mes.")
    ]
    await despachar(client, ctx, text="¿cuánto cuesta?", esperar=True)

    assert rutas["messages"].call_count == 1
    assert "150.000" in _cuerpo(rutas["messages"])["text"]
    assert rutas["handoff"].call_count == 0
    ultima = ctx.llm.calls[-1]
    assert ultima["tools"] is None
    assert ultima["messages"][-1]["content"] == turn.CIERRA_SIN_HERRAMIENTAS


async def test_rondas_agotadas_y_sin_texto_es_handoff_error_no_silencio(ctx, client, respx_mock):
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [_herramienta(i) for i in range(turn.MAX_TOOL_ROUNDS)] + [
        LlmReply(content=None)
    ]
    await despachar(client, ctx, text="hola", esperar=True)

    assert rutas["messages"].call_count == 0
    assert rutas["handoff"].call_count == 1
    assert _cuerpo(rutas["handoff"])["reason"] == "error"


async def test_el_reintento_de_una_respuesta_invalida_va_sin_herramientas(ctx, client, respx_mock):
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [
        LlmReply(content="(Registro actualizado — conversación en curso)"),
        LlmReply(content="¡Claro! ¿Para qué fecha es tu evento?"),
    ]
    await despachar(client, ctx, text="quiero cotizar", esperar=True)

    assert rutas["messages"].call_count == 1
    assert ctx.llm.calls[0]["tools"] is not None
    assert ctx.llm.calls[1]["tools"] is None


async def test_sin_saldo_es_handoff_sin_saldo(ctx, client, respx_mock):
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.raise_exc = LlmRechazado(402, "sin_saldo", "Insufficient credits")
    await despachar(client, ctx, text="hola", esperar=True)

    assert rutas["messages"].call_count == 0
    assert _cuerpo(rutas["handoff"])["reason"] == "sin_saldo"


async def test_otros_agotamientos_siguen_siendo_error(ctx, client, respx_mock):
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.raise_exc = LlmExhausted("proveedor caído")
    await despachar(client, ctx, text="hola", esperar=True)
    assert _cuerpo(rutas["handoff"])["reason"] == "error"


# ── A1: errores del proveedor ─────────────────────────────────────────────


def _cliente(**kw: Any) -> OpenAiLlm:
    return OpenAiLlm("k", "z-ai/glm-5.3-flash", base_url="https://prov.test", **kw)


def _ok(texto: str = "hola") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": texto}, "finish_reason": "stop"}]}
    )


@pytest.mark.parametrize(
    ("status", "causa", "motivo"),
    [
        (400, "peticion_invalida", "error"),
        (401, "credencial", "error"),
        (402, "sin_saldo", "sin_saldo"),
        (403, "credencial", "error"),
        (422, "peticion_invalida", "error"),
    ],
)
@respx.mock
async def test_4xx_terminal_no_se_reintenta(sin_esperas, status: int, causa: str, motivo: str) -> None:
    ruta = respx.post(URL).mock(return_value=httpx.Response(status, json={"error": {"message": "no"}}))
    with pytest.raises(LlmRechazado) as info:
        await _cliente().complete([{"role": "user", "content": "hola"}])
    assert len(ruta.calls) == 1
    assert info.value.causa == causa
    assert info.value.motivo_handoff == motivo


@respx.mock
async def test_429_respeta_retry_after(sin_esperas) -> None:
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "3"}, json={"error": {"message": "rate"}}),
            _ok(),
        ]
    )
    reply = await _cliente().complete([{"role": "user", "content": "hola"}])
    assert reply.content == "hola"
    assert sin_esperas == [3.0]


@respx.mock
async def test_retry_after_enorme_se_acota(sin_esperas) -> None:
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "600"}, json={"error": {"message": "rate"}}),
            _ok(),
        ]
    )
    await _cliente().complete([{"role": "user", "content": "hola"}])
    assert sin_esperas == [llm_mod.RETRY_AFTER_MAX_SEGUNDOS]


@respx.mock
async def test_5xx_reintenta_con_jitter(sin_esperas) -> None:
    caida = httpx.Response(503, json={"error": {"message": "upstream"}})
    respx.post(URL).mock(side_effect=[caida, caida, _ok()])
    await _cliente().complete([{"role": "user", "content": "hola"}])
    assert len(sin_esperas) == 2
    assert 1.0 <= sin_esperas[0] <= 1.5
    assert 2.0 <= sin_esperas[1] <= 3.0


# ── A1: temperature ───────────────────────────────────────────────────────


@respx.mock
async def test_temperature_por_defecto_es_0_4() -> None:
    ruta = respx.post(URL).mock(return_value=_ok())
    await _cliente().complete([{"role": "user", "content": "hola"}])
    assert _cuerpo(ruta)["temperature"] == 0.4


def test_temperature_de_entorno_y_de_perfil(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make_settings().llm_temperature == 0.4
    monkeypatch.setenv("LLM_TEMPERATURE", "0.2")
    from app.config import Settings

    assert Settings().llm_temperature == 0.2
    perfil = profile_from_payload({"llm": {"model": "m", "temperature": 0.7}}, "Asistente")
    assert perfil.llm_temperature == 0.7
    assert profile_from_payload({"llm": {"temperature": "alta"}}, "A").llm_temperature is None
    assert temperatura_valida(3) is None
    assert temperatura_valida(True) is None


@respx.mock
async def test_un_400_que_nombra_temperature_la_apaga_y_reintenta(sin_esperas) -> None:
    ruta = respx.post(URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "temperature is not supported"}}),
            _ok(),
        ]
    )
    llm = _cliente()
    reply = await llm.complete([{"role": "user", "content": "hola"}])
    assert reply.content == "hola"
    assert "temperature" not in _cuerpo(ruta, 1)


# ── A2: handoff resiliente ────────────────────────────────────────────────


def test_motivos_nuevos_del_catalogo() -> None:
    for motivo in ("sin_saldo", "opt_out", "abuso"):
        assert canonical_handoff_reason(motivo) == motivo
    assert canonical_handoff_reason("sin_credencial") == "sin_saldo"
    # Nada difuso: estas frases del modelo NO son motivos del sistema.
    assert canonical_handoff_reason("pregunta por crédito para pagar") == "modelo"
    assert canonical_handoff_reason("quiere trabajar con nosotros") == "modelo"


async def test_handoff_que_falla_se_reintenta_y_luego_se_encola(respx_mock, sin_esperas):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    ruta = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(return_value=httpx.Response(503, json={}))
    estado = turn._Turno(crm_conversation_id=CRM_CONV_ID)

    await turn._safe_handoff(ctx, CRM_CONV_ID, "sin_saldo", estado, conv.id)

    assert ruta.call_count == turn.HANDOFF_ATTEMPTS
    pendientes = await ctx.store.due_pending_sends(utcnow())
    assert [(p.kind, p.content) for p in pendientes] == [(KIND_HANDOFF, "sin_saldo")]
    # La red de seguridad no registra otro encima: la cola ya lo lleva.
    assert estado.handoff == "sin_saldo"

    ruta.mock(return_value=httpx.Response(200, json={}))
    await SenderWorker(ctx).tick(utcnow())
    assert json.loads(ruta.calls.last.request.content)["reason"] == "sin_saldo"
    assert ctx.store.pending_sends[pendientes[0].id].delivered_at is not None
    await ctx.crm.aclose()


async def test_un_handoff_pendiente_vive_24_h(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "error", kind=KIND_HANDOFF)
    ctx.store.pending_sends[pid].created_at = utcnow() - timedelta(hours=2)
    ruta = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(return_value=httpx.Response(200, json={}))
    await SenderWorker(ctx).tick(utcnow())
    assert ruta.call_count == 1
    assert ctx.store.pending_sends[pid].delivered_at is not None
    await ctx.crm.aclose()


# ── A2: envío terminal y pending_send de 15 min ───────────────────────────


@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_envio_terminal_por_status(status: int) -> None:
    assert envio_terminal(error_de_envio(status))
    assert not envio_terminal(error_de_envio(502))
    assert not envio_terminal(error_de_envio(503))


async def test_envio_rechazado_para_siempre_no_se_reintenta_ni_se_encola(ctx, client, respx_mock, sin_esperas):
    rutas = mock_crm_basics(respx_mock)
    rutas["messages"].mock(
        return_value=httpx.Response(422, json={"error": {"code": "envio_permanente", "message": "131026"}})
    )
    await despachar(client, ctx, text="hola", esperar=True)

    assert rutas["messages"].call_count == 1
    assert await ctx.store.due_pending_sends(utcnow()) == []
    assert _cuerpo(rutas["handoff"])["reason"] == "error"


async def test_pending_send_caduca_a_los_15_min_con_handoff(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "respuesta vieja")
    ctx.store.pending_sends[pid].created_at = utcnow() - timedelta(minutes=16)
    mensajes = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m"})
    )
    handoff = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(return_value=httpx.Response(200, json={}))

    await SenderWorker(ctx).tick(utcnow())

    assert mensajes.call_count == 0  # nunca una respuesta obsoleta
    assert ctx.store.pending_sends[pid].abandoned_at is not None
    assert json.loads(handoff.calls[0].request.content)["reason"] == "error"
    await ctx.crm.aclose()


async def test_pending_send_dentro_de_los_15_min_sigue_saliendo(respx_mock):
    ctx = make_ctx(make_settings(pending_send_max_minutes=15))
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "respuesta")
    ctx.store.pending_sends[pid].created_at = utcnow() - timedelta(minutes=10)
    mensajes = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m"})
    )
    await SenderWorker(ctx).tick(utcnow())
    assert mensajes.call_count == 1
    assert ctx.store.pending_sends[pid].delivered_at is not None
    await ctx.crm.aclose()


async def test_pending_send_rechazado_para_siempre_se_abandona_con_handoff(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "respuesta")
    respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(return_value=httpx.Response(422, json={}))
    handoff = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(return_value=httpx.Response(200, json={}))
    await SenderWorker(ctx).tick(utcnow())
    assert ctx.store.pending_sends[pid].abandoned_at is not None
    assert handoff.call_count == 1
    await ctx.crm.aclose()


# ── A3: efectos, reanudación y cierre ordenado ────────────────────────────


async def test_la_marca_de_efectos_se_escribe_una_vez() -> None:
    marcador = AsyncMock()
    with registrar_efectos(marcador):
        await marcar_efecto()
        await marcar_efecto()
    await marcar_efecto()  # fuera del bloque: no hace nada
    marcador.assert_awaited_once()


async def test_enviar_marca_el_efecto_antes(respx_mock, ctx, client):
    """Un turno que envió queda marcado en su fila: no se reanuda."""
    mock_crm_basics(respx_mock)
    await despachar(client, ctx, text="hola", esperar=True)
    fila = next(iter(ctx.store.dispatch_inbox.values()))
    assert fila["state"] == "done"
    assert fila["effects_at"] is not None


async def test_un_turno_que_solo_leyo_no_queda_marcado(respx_mock, ctx, client):
    """Gate de IA en pausa: no envió ni ejecutó nada → sin marca."""
    mock_crm_basics(respx_mock, ai_enabled=False)
    await despachar(client, ctx, text="hola", esperar=True)
    fila = next(iter(ctx.store.dispatch_inbox.values()))
    assert fila["effects_at"] is None


def test_puede_reanudar() -> None:
    class Job:
        recovery = True
        effects_at = None
        attempts = 2

    assert puede_reanudar(Job())
    Job.effects_at = utcnow()
    assert not puede_reanudar(Job())
    Job.effects_at = None
    Job.attempts = 4
    assert not puede_reanudar(Job())
    Job.attempts = 1
    Job.recovery = False
    assert not puede_reanudar(Job())


async def test_la_reanudacion_no_duplica_el_mensaje_del_lead(respx_mock):
    rutas = mock_crm_basics(respx_mock)
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    # El turno interrumpido ya había guardado el mensaje (y lo marcó leído).
    await ctx.store.add_message(conv.id, "user", "¿tienen sillas Tiffany?", wa_message_id="wamid.1")
    inbound = [InboundMessage(wa_message_id="wamid.1", identity=IDENTITY, type="text", text="¿tienen sillas Tiffany?")]

    await turn.handle_flush(ctx, IDENTITY, inbound, crm_conversation_id=CRM_CONV_ID, reanudacion=True)

    historial = await ctx.store.recent_messages(conv.id, 10)
    assert [m.role for m in historial] == ["user", "assistant"]
    assert rutas["messages"].call_count == 1
    await ctx.crm.aclose()


async def test_reanudar_no_filtra_los_mensajes_ya_leidos(monkeypatch):
    from app import dispatch
    from app.turn import marca_de_leido
    from tests.conftest import evento_despacho

    ctx = make_ctx()
    evento = evento_despacho(text="hola")
    for m in evento["messages"]:
        await ctx.store.mark_processed(marca_de_leido("", m["id"]))
    llamado = AsyncMock()
    monkeypatch.setattr(dispatch, "handle_flush", llamado)

    await dispatch._procesar(ctx, evento, durable=True)
    llamado.assert_not_awaited()  # lo normal: ya leído, nada que contestar

    await dispatch._procesar(ctx, evento, durable=True, reanudacion=True)
    llamado.assert_awaited_once()
    assert llamado.await_args.kwargs["reanudacion"] is True
    await ctx.crm.aclose()


async def test_apagando_los_consumidores_no_reclaman() -> None:
    ctx = make_ctx()
    ctx.apagando.set()
    from collections import Counter

    await asyncio.wait_for(_consumidor(ctx, Counter()), timeout=1)
    await ctx.crm.aclose()


async def test_apagar_espera_a_los_turnos_en_curso() -> None:
    ctx = make_ctx(make_settings(nea_shutdown_grace_seconds=5))
    terminado = asyncio.Event()

    async def turno_en_curso() -> None:
        await ctx.apagando.wait()
        await asyncio.sleep(0.05)  # termina su turno
        terminado.set()

    async def otro_worker() -> None:
        await asyncio.sleep(3600)

    despacho = asyncio.create_task(turno_en_curso(), name="dispatch-worker")
    otro = asyncio.create_task(otro_worker(), name="followup-worker")
    await _apagar_con_orden(ctx, [despacho, otro])

    assert terminado.is_set()
    assert otro.cancelled()
    await ctx.crm.aclose()


async def test_apagar_corta_si_el_margen_se_agota() -> None:
    ctx = make_ctx(make_settings(nea_shutdown_grace_seconds=0.05))

    async def colgado() -> None:
        await asyncio.sleep(3600)

    despacho = asyncio.create_task(colgado(), name="dispatch-worker")
    await asyncio.wait_for(_apagar_con_orden(ctx, [despacho]), timeout=2)
    assert despacho.cancelled()
    await ctx.crm.aclose()


async def test_drain_reanuda_de_punta_a_punta(respx_mock, monkeypatch):
    """Un despacho que quedó `started` sin efectos se vuelve a contestar."""
    from datetime import datetime, timezone

    from tests.conftest import evento_despacho

    rutas = mock_crm_basics(respx_mock)
    ctx = make_ctx()
    evento = evento_despacho(text="¿hacen domicilios?")
    await ctx.store.enqueue_dispatch("", evento)
    async with ctx.store.claim_dispatch() as job:
        assert job is not None  # Nea muere aquí: sin finish ni efectos
    fila = next(iter(ctx.store.dispatch_inbox.values()))
    fila["available_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert await drain_one(ctx)

    assert rutas["messages"].call_count == 1
    assert rutas["handoff"].call_count == 0
    assert fila["state"] == "done"
    await ctx.crm.aclose()
