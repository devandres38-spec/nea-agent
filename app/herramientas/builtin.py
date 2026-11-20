"""Las herramientas de siempre, registradas como `ToolSpec` (071, D20).

Mismos esquemas, mismo orden y misma ejecución que antes del registro (las
pruebas de `tests/test_tools.py` no cambian). Los esquemas siguen viviendo en
`app/tools.py`; aquí solo se declara cuándo existe cada una y a qué método de
`ToolRuntime` va.

Se importa tarde desde `app/tools.py` (dentro de las funciones) porque este
módulo lee sus esquemas.
"""
from __future__ import annotations

import copy
from typing import Any

from app import tools as t
from app.herramientas.ficha import esquema_dinamico
from app.herramientas.registro import Capacidades, Registro, ToolSpec

COORDINATION_CONSENT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "coordination_consent",
        "description": (
            "Registra consentimiento EXPLÍCITO para un único recordatorio de "
            "coordinación tras 24 horas sin reservar. Nunca deduzcas "
            "consentimiento por pedir una cita. consent=false si rechaza "
            "seguimiento. Solo tras ofrecer horarios."
        ),
        "parameters": {
            "type": "object",
            "properties": {"consent": {"type": "boolean"}},
            "required": ["consent"],
        },
    },
}


def _de_tools(nombre: str) -> dict[str, Any]:
    for esquema in t.TOOL_SCHEMAS + t.AGENDA_V2_SCHEMAS:
        if esquema["function"]["name"] == nombre:
            return esquema
    raise KeyError(nombre)


def esquema_de_ficha(context: dict[str, Any] | None = None) -> dict[str, Any]:
    """El esquema de `update_ficha` (D20): el único lugar que lo decide.

    vocero-crm 072 (D11): con `fichaSchema` en el contexto, los campos del
    negocio + `notas` (`app/herramientas/ficha.py`); sin él, el B2B fijo de
    siempre, byte a byte.
    """
    base = _de_tools("update_ficha")
    return esquema_dinamico(base, context) or base


def _book_session(caps: Capacidades) -> dict[str, Any]:
    if not caps.agenda_v2:
        return _de_tools("book_session")
    esquema = copy.deepcopy(_de_tools("book_session"))
    params = esquema["function"]["parameters"]
    params["properties"]["recordatorios_aceptados"] = dict(t.RECORDATORIOS_ACEPTADOS)
    params["required"].append("recordatorios_aceptados")
    return esquema


def _reschedule_session(caps: Capacidades) -> dict[str, Any]:
    if not caps.agenda_v2:
        return _de_tools("reschedule_session")
    esquema = copy.deepcopy(_de_tools("reschedule_session"))
    params = esquema["function"]["parameters"]
    params["properties"]["selection_token"] = {
        "type": "string",
        "description": "Token de list_bookings de la cita elegida y confirmada por el cliente",
    }
    params["required"].append("selection_token")
    return esquema


def _con_agenda(caps: Capacidades) -> bool:
    return caps.agenda


def _con_agenda_v2(caps: Capacidades) -> bool:
    return caps.agenda and caps.agenda_v2


def _fijo(nombre: str):
    return lambda _caps: _de_tools(nombre)


async def _list_bookings(rt: Any, args: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "bookings": await rt._ctx.crm.list_bookings(rt._crm_conv_id)}


async def _cancel_session(rt: Any, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("confirmation") is not True or not args.get("selection_token"):
        return {"ok": False, "error": "confirmation_required"}
    return await rt._ctx.crm.cancel_booking(rt._crm_conv_id, str(args["selection_token"]), True)


async def _coordination_consent(rt: Any, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args.get("consent"), bool):
        return {"ok": False, "error": "explicit_consent_required"}
    return await rt._ctx.crm.set_coordination_consent(rt._crm_conv_id, args["consent"])


async def _route_out(rt: Any, args: dict[str, Any]) -> dict[str, Any]:
    return await rt._route_out()


async def _handoff(rt: Any, args: dict[str, Any]) -> dict[str, Any]:
    return rt._handoff(args)


async def _solicitar_baja(rt: Any, args: dict[str, Any]) -> dict[str, Any]:
    """073-B (D6) — El CRM registra la baja al recibir el handoff `opt_out`."""
    rt.baja = True
    rt.handoff_reason = "opt_out"
    rt.handoff_summary = None
    return {
        "ok": True,
        "nota": "La baja quedó registrada; el sistema le confirma al lead. No escribas nada más.",
    }


def _metodo(nombre: str):
    async def ejecutar(rt: Any, args: dict[str, Any]) -> dict[str, Any]:
        return await getattr(rt, nombre)(args)

    return ejecutar


REGISTRO = Registro(
    [
        ToolSpec(
            "update_ficha",
            "ficha",
            lambda caps: esquema_de_ficha(caps.contexto),
            _metodo("_update_ficha"),
        ),
        ToolSpec("propose_slots", "agendar", _fijo("propose_slots"), _metodo("_propose_slots"), ofrecer=_con_agenda),
        ToolSpec("book_session", "agendar", _book_session, _metodo("_book_session"), ofrecer=_con_agenda),
        ToolSpec(
            "reschedule_session",
            "reprogramar",
            _reschedule_session,
            _metodo("_reschedule_session"),
            ofrecer=_con_agenda,
        ),
        ToolSpec("route_out", "salida", _fijo("route_out"), _route_out),
        ToolSpec("handoff", "escalar", _fijo("handoff"), _handoff),
        ToolSpec("solicitar_baja", "escalar", lambda _caps: copy.deepcopy(t.SOLICITAR_BAJA_SCHEMA), _solicitar_baja),
        ToolSpec(
            "list_bookings",
            "agendar",
            _fijo("list_bookings"),
            _list_bookings,
            ofrecer=_con_agenda_v2,
            ejecutable=lambda caps: caps.agenda_v2,
        ),
        ToolSpec(
            "cancel_session",
            "agendar",
            _fijo("cancel_session"),
            _cancel_session,
            ofrecer=_con_agenda_v2,
            ejecutable=lambda caps: caps.agenda_v2,
        ),
        ToolSpec(
            "coordination_consent",
            "agendar",
            lambda _caps: COORDINATION_CONSENT_SCHEMA,
            _coordination_consent,
            ofrecer=lambda caps: _con_agenda_v2(caps) and caps.coordinacion,
            ejecutable=lambda caps: caps.coordinacion,
        ),
        # 038 — Solo existe cuando el contexto trae a qué etapas moverlo.
        ToolSpec(
            "move_stage",
            "etapa",
            lambda _caps: t.MOVE_STAGE_SCHEMA,
            _metodo("_move_stage"),
            ofrecer=lambda caps: bool(caps.etapas),
        ),
    ]
)
