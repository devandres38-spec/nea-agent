"""Auditoría 071 — regresiones del turno y de las herramientas.

A-2: `CrmRechazo` (401/403) es terminal en `_send`, `_fetch_context`,
`get_media` y `describe_item`: sin reintentos ni `pending_send`.
A-7: el catálogo recibe el contexto del turno, y `ToolRuntime.execute()` solo
ejecuta lo que se le ofreció (perfil ∩ rol ∩ capacidades).
A-8: la puerta 1.5 reusa el contexto en la 2, no traga `CrmRechazo` y no
reabre una conversación en handoff o con la ventana cerrada.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app import media, turn
from app.crm import CrmClient, CrmError, CrmRechazo
from app.crm_brains import BrainsCrmClient
from app.profile import BusinessProfile
from app.state import Conversation, InboundMessage, utcnow
from app.tools import ToolRuntime, tool_schemas
from tests.conftest import CRM_URL, IDENTITY, crm_context, make_ctx, make_settings, mock_crm_basics


class _Perfil:
    def __init__(self, perfil: BusinessProfile) -> None:
        self._p = perfil

    async def get(self) -> BusinessProfile:
        return self._p


def _msg(texto: str, wamid: str = "wamid.a1") -> InboundMessage:
    return InboundMessage(wa_message_id=wamid, identity=IDENTITY, type="text", text=texto)


def _nombres(schemas):
    return [t["function"]["name"] for t in schemas]


# ──────────────────────────────────────────────────────────── A-2 ───


async def test_a2_send_no_reintenta_ni_encola_un_rechazo():
    ctx = make_ctx()
    llamadas = 0

    async def rechaza(conversation_id: str, text: str, dispatch_id: str = "") -> dict:
        nonlocal llamadas
        llamadas += 1
        raise CrmRechazo(403, "organizacion_suspendida")

    ctx.crm.send_message = rechaza  # type: ignore[method-assign]
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    with pytest.raises(CrmRechazo):
        await turn._send(ctx, conv.id, "cv_1", "hola")
    assert llamadas == 1
    assert ctx.store.pending_sends == {}
    await ctx.crm.aclose()


async def test_a2_fetch_context_no_reintenta_un_rechazo():
    ctx = make_ctx()
    llamadas = 0

    async def rechaza(identity: str) -> dict:
        nonlocal llamadas
        llamadas += 1
        raise CrmRechazo(401)

    ctx.crm.get_context = rechaza  # type: ignore[method-assign]
    with pytest.raises(CrmRechazo):
        await turn._fetch_context(ctx, IDENTITY)
    assert llamadas == 1
    await ctx.crm.aclose()


async def test_a2_get_media_de_cerebros_levanta_rechazo(respx_mock):
    respx_mock.get(f"{CRM_URL}/api/brains/media/m1").mock(return_value=httpx.Response(401))
    cliente = BrainsCrmClient(CRM_URL, "secreto", "org_1")
    with pytest.raises(CrmRechazo):
        await cliente.get_media("m1")
    await cliente.aclose()


async def test_a2_get_media_de_la_raiz_sigue_siendo_un_error_comun(respx_mock):
    respx_mock.get(f"{CRM_URL}/api/bot/media/m1").mock(return_value=httpx.Response(401))
    cliente = CrmClient(CRM_URL, "key")
    with pytest.raises(CrmError) as exc:
        await cliente.get_media("m1")
    assert not isinstance(exc.value, CrmRechazo)
    await cliente.aclose()


async def test_a2_describe_item_no_traga_el_rechazo():
    ctx = make_ctx()

    async def rechaza(media_id: str):
        raise CrmRechazo(403)

    ctx.crm.get_media = rechaza  # type: ignore[method-assign]
    msg = InboundMessage(wa_message_id="w", identity=IDENTITY, type="image", media_id="m1")
    with pytest.raises(CrmRechazo):
        await media.describe_item(ctx, msg)
    await ctx.crm.aclose()


async def test_a2_sin_propagar_no_intenta_el_handoff_de_emergencia(monkeypatch):
    ctx = make_ctx()

    async def run_turn(*a: Any, **k: Any) -> None:
        raise CrmRechazo(401)

    emergencias: list[Any] = []

    async def emergencia(*a: Any, **k: Any) -> None:
        emergencias.append(a)

    monkeypatch.setattr(turn, "run_turn", run_turn)
    monkeypatch.setattr(turn, "_handoff_de_emergencia", emergencia)
    await turn.handle_flush(ctx, IDENTITY, [_msg("hola")], "cv_1")
    assert emergencias == []
    with pytest.raises(CrmRechazo):
        await turn.handle_flush(ctx, IDENTITY, [_msg("hola")], "cv_1", propagate_errors=True)
    await ctx.crm.aclose()


# ──────────────────────────────────────────────────────────── A-7 ───


def _runtime(perfil: BusinessProfile | None = None, **kw: Any) -> tuple[Any, ToolRuntime]:
    ctx = make_ctx()
    return ctx, ToolRuntime(ctx, Conversation(id=1, wa_identity=IDENTITY), "cv_1", profile=perfil, **kw)


async def test_a7_no_ejecuta_una_herramienta_apagada_por_el_negocio(respx_mock):
    ruta = respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(return_value=httpx.Response(200, json={}))
    ctx, rt = _runtime(BusinessProfile(tools=frozenset({"ficha"})))
    r = await rt.execute("route_out", {})
    assert r["error"] == "herramienta_no_disponible"
    assert ruta.call_count == 0 and rt.routed_out is False
    # La red de seguridad nunca se apaga.
    assert (await rt.execute("handoff", {"reason": "x"}))["ok"] is True
    await ctx.crm.aclose()


async def test_a7_no_ejecuta_una_herramienta_fuera_del_rol(monkeypatch):
    from app import roles

    rol = roles.RolSpec(
        clave="solo_ficha", enfoques=roles.ROLES["calificador"].enfoques, herramientas=frozenset({"ficha"})
    )
    monkeypatch.setitem(roles.ROLES, "solo_ficha", rol)
    ctx, rt = _runtime(BusinessProfile(role="solo_ficha"))
    assert (await rt.execute("route_out", {}))["error"] == "herramienta_no_disponible"
    await ctx.crm.aclose()


async def test_a7_sin_agenda_no_se_reserva_aunque_el_modelo_lo_pida():
    ctx, rt = _runtime()
    ctx.agenda_enabled = False
    for nombre in ("propose_slots", "book_session", "reschedule_session"):
        assert (await rt.execute(nombre, {"start_utc": "2026-09-15T15:00:00Z"}))["error"] == "herramienta_no_disponible"
    await ctx.crm.aclose()


async def test_a7_mover_de_etapa_solo_si_el_contexto_lo_ofrece():
    ctx, rt = _runtime()
    assert (await rt.execute("move_stage", {"etapa": "Interesado"}))["error"] == "herramienta_no_disponible"
    await ctx.crm.aclose()


def test_a7_el_catalogo_recibe_el_contexto_y_move_stage_sale_del_registro(monkeypatch):
    from app.herramientas import builtin

    vistos: list[Any] = []
    original = builtin.esquema_de_ficha

    def espia(context=None):
        vistos.append(context)
        return original(context)

    monkeypatch.setattr(builtin, "esquema_de_ficha", espia)
    contexto = {"lead": {"nextStages": [{"name": "Interesado"}]}, "contact": {"customFields": {"talla": "M"}}}
    nombres = _nombres(tool_schemas(True, context=contexto))
    assert vistos == [contexto]
    assert nombres[-1] == "move_stage" and nombres.count("move_stage") == 1


def test_a7_constantes_derivadas_del_registro():
    from app.herramientas.builtin import REGISTRO
    from app.tools import AGENDA_TOOLS, CLAVE_DE_HERRAMIENTA

    assert CLAVE_DE_HERRAMIENTA == {s.nombre: s.clave for s in REGISTRO}
    assert AGENDA_TOOLS == frozenset({"propose_slots", "book_session", "reschedule_session"})


async def test_a7_el_turno_pasa_el_contexto_al_catalogo(respx_mock, monkeypatch):
    from app.herramientas import builtin

    vistos: list[Any] = []
    original = builtin.esquema_de_ficha
    monkeypatch.setattr(builtin, "esquema_de_ficha", lambda context=None: vistos.append(context) or original(context))
    ctx = make_ctx()
    mock_crm_basics(respx_mock)
    await turn.run_turn(ctx, IDENTITY, [_msg("hola")])
    assert vistos and vistos[0]["conversation"]["id"] == crm_context()["conversation"]["id"]
    await ctx.crm.aclose()


# ──────────────────────────────────────────────────────────── A-8 ───


async def _cerrada(ctx: Any) -> Conversation:
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(
        conv.id, crm_conversation_id="cv_test1", phase="cerrada", stalled_at=utcnow()
    )
    return conv


async def test_a8_en_cloud_el_contexto_de_la_puerta_1_5_se_reusa(respx_mock):
    ctx = make_ctx(make_settings(vocero_mode="cloud"))
    ctx.profile = _Perfil(BusinessProfile(stall_enabled=False))
    rutas = mock_crm_basics(respx_mock)
    await _cerrada(ctx)
    await turn.run_turn(ctx, IDENTITY, [_msg("gracias")])
    assert rutas["context"].call_count == 1  # antes: 2
    assert rutas["messages"].call_count == 1
    await ctx.crm.aclose()


async def test_a8_no_reabre_una_conversacion_en_handoff(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile())
    rutas = mock_crm_basics(respx_mock, ai_enabled=False)
    await _cerrada(ctx)
    await turn.run_turn(ctx, IDENTITY, [_msg("¿cuánto cuesta la limpieza?")])
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is not None and conv.phase == "cerrada"
    assert rutas["ficha"].call_count == 0  # la marca de cierre no se tocó
    await ctx.crm.aclose()


async def test_a8_no_reabre_con_la_ventana_cerrada(respx_mock):
    ctx = make_ctx()
    rutas = mock_crm_basics(respx_mock, window_open=False)
    await _cerrada(ctx)
    await turn.run_turn(ctx, IDENTITY, [_msg("¿cuánto cuesta?")])
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is not None
    assert rutas["ficha"].call_count == 0
    await ctx.crm.aclose()


async def test_a8_con_contenido_y_todo_abierto_si_reabre(respx_mock):
    ctx = make_ctx()
    rutas = mock_crm_basics(respx_mock)
    await _cerrada(ctx)
    await turn.run_turn(ctx, IDENTITY, [_msg("¿cuánto cuesta?")])
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is None and conv.phase != "cerrada"
    assert rutas["messages"].call_count == 1
    await ctx.crm.aclose()


async def test_a8_la_puerta_1_5_no_traga_el_rechazo():
    ctx = make_ctx(make_settings(vocero_mode="cloud"))

    async def rechaza(identity: str) -> dict:
        raise CrmRechazo(401)

    ctx.crm.get_context = rechaza  # type: ignore[method-assign]
    await _cerrada(ctx)
    with pytest.raises(CrmRechazo):
        await turn.run_turn(ctx, IDENTITY, [_msg("gracias")])
    await ctx.crm.aclose()
