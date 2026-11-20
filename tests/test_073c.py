"""vocero-crm 073-C — Calidad conversacional, comprobantes y Colombia (Nea)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app import media
from app.comprobantes import NOTA, posible_comprobante
from app.confirmacion import confirmar, evaluar
from app.dias import nombra_un_dia
from app.dispatch import mensajes_del_despacho
from app.hostility import is_hostile
from app.llm import LlmReply, OpenAiLlm, ToolCall
from app.memoria import con_separadores, separador, ventana
from app.obsoleta import hay_despacho_mas_nuevo, registrar_chequeo
from app.partir import limite_de, partir
from app.stall import es_relleno, sin_rumbo
from app.state import BotMessage, InboundMessage
from app.tools import dia_confirmado_valido, etiqueta_con_profesional
from app.validador import limpiar_pensamiento, promete_handoff, revisar
from tests.conftest import despachar, mock_crm_basics


def _cuerpo(ruta: Any, i: int = 0) -> dict[str, Any]:
    return json.loads(ruta.calls[i].request.content)


# ── D7: validador post-turno ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("crudo", "limpio"),
    [
        ("<think>calculo el precio</think>Hola, son 120.000", "Hola, son 120.000"),
        ("Hola<think>sin cerrar y sigue", "Hola"),
        ("razonamiento suelto</think>\nRespuesta final", "Respuesta final"),
        ("<THINK>a</THINK> <think>b</think>listo", "listo"),
        ("Sin nada raro", "Sin nada raro"),
    ],
)
def test_think_se_limpia(crudo: str, limpio: str) -> None:
    assert limpiar_pensamiento(crudo) == limpio


def test_lo_que_no_puede_salir() -> None:
    assert revisar("Listo, llamo a book_session ahora", reservo=False)
    assert revisar('<tool_call>{"name":"x"}</tool_call>', reservo=False)
    assert revisar("Te cuento el precio.\n(Nota: ficha actualizada)", reservo=False)
    assert revisar("¡Listo! Tu cita quedó agendada para el lunes.", reservo=False)
    assert revisar("¡Listo! Tu cita quedó agendada para el lunes.", reservo=True) is None
    assert revisar("Te paso las opciones del lunes y el martes.", reservo=False) is None
    assert promete_handoff("Ya te paso con un asesor 🙌")
    assert promete_handoff("Un asesor te contactará en breve")
    assert not promete_handoff("¿Quieres que te pase con un asesor?")


async def test_glm_que_filtra_think_sale_limpio(ctx, client, respx_mock) -> None:
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="<think>el lead saluda, respondo corto</think>¡Hola! ¿En qué te ayudo?")]
    await despachar(client, ctx, text="hola", esperar=True)
    assert _cuerpo(rutas["messages"])["text"] == "¡Hola! ¿En qué te ayudo?"


async def test_nombrar_una_herramienta_pide_otra_respuesta_sin_herramientas(ctx, client, respx_mock) -> None:
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [
        LlmReply(content="Voy a usar propose_slots para ver la agenda."),
        LlmReply(content="Déjame revisar qué horarios tenemos."),
    ]
    await despachar(client, ctx, text="¿tienen cita mañana?", esperar=True)
    assert ctx.llm.calls[-1]["tools"] is None
    assert _cuerpo(rutas["messages"])["text"] == "Déjame revisar qué horarios tenemos."


async def test_prometer_un_asesor_sin_handoff_lo_cumple(ctx, client, respx_mock) -> None:
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="Claro, ya te paso con un asesor del equipo.")]
    await despachar(client, ctx, text="quiero hablar con alguien", esperar=True)
    assert rutas["messages"].call_count == 1
    assert rutas["handoff"].call_count == 1
    assert _cuerpo(rutas["handoff"])["reason"] == "modelo"


# ── D5: comprobantes (decisión #4) ────────────────────────────────────────


def test_detector_de_comprobantes() -> None:
    assert posible_comprobante(["te envío el comprobante de Nequi"], hay_adjunto=True)
    assert posible_comprobante(["listo, pagado por Daviplata"], hay_adjunto=True)
    assert posible_comprobante(["ya te pagué la cita"], hay_adjunto=False)
    assert posible_comprobante(["ya hice la transferencia"], hay_adjunto=False)
    assert not posible_comprobante(["¿puedo pagar por Nequi?"], hay_adjunto=False)
    assert not posible_comprobante(["mira mi diente"], hay_adjunto=True)


async def test_un_comprobante_es_handoff_con_nota(ctx, client, respx_mock) -> None:
    rutas = mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="¡Gracias! El equipo valida el pago y te confirma.")]
    await despachar(client, ctx, text="ya te pagué por Bancolombia, te mando el soporte", esperar=True)
    assert rutas["messages"].call_count == 1
    cuerpo = _cuerpo(rutas["handoff"])
    assert cuerpo["summary"].startswith(NOTA)
    assert "validar en banco" in cuerpo["summary"]


# ── D5: multimedia ────────────────────────────────────────────────────────


def test_la_imagen_en_el_historial_va_en_pasado() -> None:
    presente = f"[El lead mandó una imagen — {media.IMAGEN_ADJUNTA}]"
    pasado = media.en_pasado(presente)
    assert "ya no la tienes a la vista" in pasado and media.IMAGEN_ADJUNTA not in pasado
    assert media.en_presente(pasado) == presente


async def test_como_mucho_tres_imagenes_y_el_historial_en_pasado(ctx, client, respx_mock) -> None:
    mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="Las veo, gracias.")]
    mensajes = [
        {"id": f"wamid.i{i}", "at": "2026-11-20T10:00:00Z", "type": "image", "text": None,
         "mediaId": f"m{i}", "mimeType": "image/jpeg", "caption": None}
        for i in range(5)
    ]
    await despachar(client, ctx, mensajes=mensajes, esperar=True)
    ultimo = [m for m in ctx.llm.calls[0]["messages"] if m["role"] == "user"][-1]
    imagenes = [p for p in ultimo["content"] if p.get("type") == "image_url"]
    assert len(imagenes) == 3
    assert "otras 2 imágenes" in ultimo["content"][0]["text"]
    conv = next(iter(ctx.store.conversations.values()))
    guardado = (await ctx.store.recent_messages(conv.id, 5))[0].content
    assert "ya no la tienes a la vista" in guardado and media.IMAGEN_ADJUNTA not in guardado


async def test_una_reaccion_no_abre_turno(ctx, client, respx_mock) -> None:
    rutas = mock_crm_basics(respx_mock)
    await despachar(
        client, ctx, esperar=True,
        mensajes=[{"id": "wamid.r", "at": "2026-11-20T10:00:00Z", "type": "reaction", "text": "👍", "mediaId": None}],
    )
    assert ctx.llm.calls == []
    assert rutas["messages"].call_count == 0


async def test_un_adjunto_de_zernio_con_texto_se_procesa(ctx, client, respx_mock) -> None:
    mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="La veo.")]
    await despachar(
        client, ctx, esperar=True, channel="instagram",
        mensajes=[{"id": "wamid.z", "at": "2026-11-20T10:00:00Z", "type": "image", "text": "así quedó",
                   "mediaId": "m1", "mimeType": "image/jpeg", "caption": None}],
    )
    ultimo = [m for m in ctx.llm.calls[0]["messages"] if m["role"] == "user"][-1]
    assert any(p.get("type") == "image_url" for p in ultimo["content"])
    assert "«así quedó»" in ultimo["content"][0]["text"]


async def test_sin_modelo_que_oiga_no_pide_notas_de_voz() -> None:
    ctx = SimpleNamespace(llm=SimpleNamespace(puede_transcribir=False))
    msg = InboundMessage(wa_message_id="w", identity="i", type="video")
    parte = await media._video(ctx, msg)  # type: ignore[arg-type]
    assert "nota de voz" not in (parte.text or "")
    assert OpenAiLlm("k", "m", transcribe_model="").puede_transcribir is False
    assert OpenAiLlm("k", "m", transcribe_model="whisper-1").puede_transcribir is True


def test_el_despacho_trae_la_cita_y_los_telefonos() -> None:
    payload = {
        "contact": {"identity": "573001112233", "displayName": "Ana"},
        "conversation": {"channel": "whatsapp"},
        "messages": [
            {"id": "m1", "type": "text", "text": "ese", "mediaId": None,
             "replyTo": {"id": "x", "author": "negocio", "type": "text", "text": "¿martes 10:00 o jueves 3:00 p. m.?"}},
            {"id": "m2", "type": "contacts", "text": None, "mediaId": None,
             "contacts": [{"name": "Luis", "phones": ["+57 300 111 2233"]}]},
        ],
    }
    a, b = mensajes_del_despacho(payload)
    assert a.reply_to == {"author": "negocio", "type": "text", "text": "¿martes 10:00 o jueves 3:00 p. m.?"}
    assert b.contact_names == ["Luis"] and b.contact_phones == ["+57 300 111 2233"]


async def test_responder_citando_llega_al_modelo(ctx, client, respx_mock) -> None:
    mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="¡Listo!")]
    await despachar(
        client, ctx, esperar=True,
        mensajes=[{"id": "wamid.c", "at": "2026-11-20T10:00:00Z", "type": "text", "text": "ese", "mediaId": None,
                   "replyTo": {"id": "x", "author": "negocio", "type": "text", "text": "Jueves [3:00 p. m.]"}}],
    )
    ultimo = [m for m in ctx.llm.calls[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert ultimo.startswith("[El lead responde citando un mensaje tuyo: «Jueves (3:00 p. m.)»]\nese")


# ── D5: partición por canal ───────────────────────────────────────────────


def test_partir_respeta_el_tope_de_cada_canal() -> None:
    parrafo = ("Este es un párrafo bastante largo con información útil. " * 12).strip()
    texto = "\n\n".join([parrafo] * 6) + "\n\nMás info: https://ejemplo.co/una-url-larga-que-no-se-corta"
    for canal in ("whatsapp", "messenger", "instagram"):
        partes = partir(texto, canal)
        assert all(len(p) <= limite_de(canal) for p in partes), canal
        assert "https://ejemplo.co/una-url-larga-que-no-se-corta" in " ".join(partes)
    assert len(partir(texto, "instagram")) > len(partir(texto, "whatsapp"))
    assert partir("corto", "instagram") == ["corto"]


async def test_una_respuesta_larga_sale_en_partes(ctx, client, respx_mock) -> None:
    rutas = mock_crm_basics(respx_mock)
    largo = "\n\n".join(["Párrafo con detalles del servicio. " * 20] * 3)
    ctx.llm.replies = [LlmReply(content=largo)]
    await despachar(client, ctx, text="cuéntame todo", esperar=True, channel="instagram")
    assert rutas["messages"].call_count >= 3
    assert all(len(_cuerpo(rutas["messages"], i)["text"]) <= 1000 for i in range(rutas["messages"].call_count))


# ── D5: respuesta obsoleta ────────────────────────────────────────────────


async def test_el_chequeo_de_obsoleta_nunca_lanza() -> None:
    assert await hay_despacho_mas_nuevo() is False

    async def roto() -> bool:
        raise RuntimeError("base caída")

    with registrar_chequeo(roto):
        assert await hay_despacho_mas_nuevo() is False
    with registrar_chequeo(AsyncMock(return_value=True)):
        assert await hay_despacho_mas_nuevo() is True


# ── D6: memoria ───────────────────────────────────────────────────────────


def _m(i: int, horas: float, texto: str = "hola", rol: str = "user") -> BotMessage:
    base = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    return BotMessage(id=i, conversation_id=1, role=rol, content=texto, created_at=base + timedelta(hours=horas))


def test_ventana_de_20_con_presupuesto() -> None:
    historial = [_m(i, i * 0.1) for i in range(40)]
    assert len(ventana(historial, 20)) == 20
    gordos = [_m(i, i * 0.1, "x" * 5000) for i in range(10)]
    assert len(ventana(gordos, 20)) == 2  # 12 000 caracteres de presupuesto
    assert ventana([_m(1, 0, "x" * 50_000)], 20)  # el último siempre entra


def test_separador_solo_con_hueco_de_mas_de_6_horas() -> None:
    msgs = con_separadores([_m(1, 0), _m(2, 5), _m(3, 5 + 49)])
    assert [m["content"] for m in msgs if m["role"] == "system"] == ["— 2 días después —"]
    assert separador(timedelta(hours=7)) == "— 7 horas después —"
    assert separador(timedelta(hours=30)) == "— 1 día después —"


# ── D6/D7: stall y hostilidad en Colombia ─────────────────────────────────


@pytest.mark.parametrize("texto", ["listo", "dale 👍", "hágale", "de una", "breve", "chévere", "súper", "perfecto", "de acuerdo", "chao", "bendiciones"])
def test_relleno_colombiano(texto: str) -> None:
    assert es_relleno(texto)


def test_un_sticker_es_relleno_por_su_tipo() -> None:
    marcador = "[El lead mandó un sticker — tómalo como gesto/emoción y sigue la conversación natural; no expliques nada técnico.]"
    assert es_relleno(marcador)
    assert not es_relleno("¿cuánto cuesta la limpieza?")


def test_sin_agenda_no_corta_por_cantidad_de_mensajes() -> None:
    largos = ["quiero saber más del servicio de ortodoncia"] * 20
    assert sin_rumbo(largos, "descubrimiento", max_mensajes=14)
    assert not sin_rumbo(largos, "descubrimiento", max_mensajes=0)


@pytest.mark.parametrize(
    "texto",
    ["hijueputa", "son unos malparidos", "gonorrea de servicio", "pirobos", "carechimba", "ladrones", "me tumbaron con ese plan", "esto es una estafa"],
)
def test_hostilidad_colombiana(texto: str) -> None:
    assert is_hostile(texto)


@pytest.mark.parametrize(
    "texto",
    ["parce, ¿cuánto vale?", "qué chimba ese plan", "¿esto no será una estafa?", "¿cómo sé que no me van a robar?", "listo parcero"],
)
def test_coloquialismos_y_dudas_legitimas_no_son_hostiles(texto: str) -> None:
    assert not is_hostile(texto)


# ── D7: confirmación, días y dia_confirmado ───────────────────────────────

DATOS = {
    "label": "lunes 23 de noviembre, 10:00 a. m.",
    "start_utc": "2026-11-23T15:00:00Z",
    "tz": "America/Bogota",
    "hoy": "2026-11-20",
}


def test_la_url_con_punto_final_es_la_misma() -> None:
    datos = {**DATOS, "meeting_url": "https://zoom.us/j/123"}
    texto = "Listo, tu cita quedó el lunes 23 de noviembre a las 10:00 am. Enlace: https://zoom.us/j/123."
    assert evaluar(texto, datos) == "ok"


def test_la_plantilla_no_pierde_la_direccion_ni_maps() -> None:
    texto = (
        "Listo, tu cita es el martes 24 a las 4:00 pm.\n"
        "Dirección: Cra. 15 # 93-60, consultorio 402\n"
        "https://maps.app.goo.gl/abc123"
    )
    salida = confirmar(texto, DATOS)
    assert salida.startswith("Listo, tu cita quedó confirmada para lunes 23 de noviembre")
    assert "📍 https://maps.app.goo.gl/abc123" in salida
    assert "Cra. 15 # 93-60" in salida


def test_a_la_una_y_separar() -> None:
    datos = {**DATOS, "label": "lunes 23 de noviembre, 1:00 p. m.", "start_utc": "2026-11-23T18:00:00Z"}
    assert evaluar("Te separé el lunes 23 de noviembre a la 1 de la tarde.", datos) in ("ok", "pie")
    assert evaluar("Te separé el lunes 23 de noviembre a la 1.", datos) == "ok"


@pytest.mark.parametrize("texto", ["¿se puede el festivo?", "después del puente", "de hoy en ocho"])
def test_fechas_colombianas(texto: str) -> None:
    assert nombra_un_dia(texto)


def test_dia_confirmado_tiene_que_citar_algo_que_se_dijo() -> None:
    reciente = "¿Te separo el lunes 23 a las 10:00 a. m.?\nSí, el lunes!"
    assert dia_confirmado_valido("Sí, el lunes", reciente)
    assert not dia_confirmado_valido("sí, el martes", reciente)
    assert not dia_confirmado_valido("", reciente)
    assert dia_confirmado_valido("lo que sea", None)  # sin conversación a mano


def test_la_etiqueta_conserva_al_profesional() -> None:
    assert etiqueta_con_profesional("lun 23 nov, 10:00 a. m.", {"label": "lun 23 nov, 10:00 a. m. con Dra. Ana"}) == (
        "lun 23 nov, 10:00 a. m. con Dra. Ana"
    )
    assert etiqueta_con_profesional("lun 23 nov, 10:00", {"professional": {"name": "Dr. Luis"}}) == "lun 23 nov, 10:00 con Dr. Luis"
    assert etiqueta_con_profesional("lun 23 nov con Ana", {"label": "otra"}) == "lun 23 nov con Ana"


async def test_book_session_sin_confirmacion_citada_no_reserva(respx_mock) -> None:
    from app.state import OfferedSlot
    from app.tools import ToolRuntime
    from tests.conftest import CRM_CONV_ID, IDENTITY, make_ctx

    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    inicio = datetime(2026, 11, 23, 15, tzinfo=timezone.utc)
    await ctx.store.replace_offered_slots(conv.id, [OfferedSlot(conversation_id=conv.id, start_utc=inicio, end_utc=None, label="lunes 23")])
    rt = ToolRuntime(ctx, conv, CRM_CONV_ID, conversacion_reciente="¿te sirve el lunes 23?\nmmm déjame ver")
    r = await rt.execute("book_session", {"start_utc": "2026-11-23T15:00:00Z", "dia_confirmado": "sí, el lunes"})
    assert r["error"] == "dia_sin_confirmar"
    assert rt.booked is False
    await ctx.crm.aclose()
