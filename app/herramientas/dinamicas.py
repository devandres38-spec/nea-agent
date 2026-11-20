"""Herramientas de módulo del CRM (vocero-crm 072-B, D15).

El CRM anuncia en `context.moduleTools` las herramientas de los módulos que
ESE negocio tiene encendidos (contrato brains-072 §3) y las ejecuta en
`POST /api/brains/tools/{name}`. Aquí:

- `validar(context)` vuelve a comprobar cada una (doble candado con el CRM):
  nombre, prefijo del módulo, que no choque con una nativa ni con una
  reservada, tamaños y cantidad. Lo que no cumple se descarta con un
  `warning` y el turno sigue igual.
- `registro_del_turno(context, …)` arma un `Registro` NUEVO para ESTE turno:
  las nativas + las dinámicas válidas. El `REGISTRO` global nunca se muta
  (una Nea atiende a varios negocios a la vez; un registro compartido era
  justo el estado entre negocios que la 071 eliminó). Sin dinámicas, ES el
  `REGISTRO` (la misma instancia): el turno de un negocio sin módulos no
  cambia en nada.

Las dinámicas llevan la clave `modulo:<module>`. No pasan por `profile.tools`
(el catálogo fijo del dueño): el CRM ya filtró por módulo encendido, y el
hecho de que vinieran en el contexto de este turno es el permiso.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.herramientas.registro import Registro, ToolSpec

logger = logging.getLogger("nea.herramientas.dinamicas")

PATRON_NOMBRE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
PATRON_MODULO = re.compile(r"^[a-z][a-z0-9]{2,23}$")

# Contrato brains-072 §3: las nativas de Nea + `notas`. Además se compara
# contra el registro real (`REGISTRO.nombres()`), por si crece.
RESERVADOS = frozenset(
    {
        "update_ficha",
        "move_stage",
        "propose_slots",
        "book_session",
        "reschedule_session",
        "list_bookings",
        "cancel_session",
        "coordination_consent",
        "route_out",
        "handoff",
        "solicitar_baja",
        "notas",
    }
)

TOPE_HERRAMIENTAS = 8
TOPE_ESQUEMA_BYTES = 4096
TOPE_TOTAL_BYTES = 12288
TOPE_DESCRIPCION = 400

PREFIJO_CLAVE = "modulo:"


def es_clave_de_modulo(clave: str | None) -> bool:
    return bool(clave) and str(clave).startswith(PREFIJO_CLAVE)


def _bytes(valor: Any) -> int:
    return len(json.dumps(valor, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _motivo(item: Any, nativas: set[str], vistos: set[str]) -> str | None:
    """Por qué se descarta, o None si vale."""
    if not isinstance(item, dict):
        return "no_es_objeto"
    nombre, modulo = item.get("name"), item.get("module")
    if not isinstance(nombre, str) or not PATRON_NOMBRE.fullmatch(nombre):
        return "nombre_invalido"
    if not isinstance(modulo, str) or not PATRON_MODULO.fullmatch(modulo):
        return "modulo_invalido"
    if not nombre.startswith(f"{modulo}_"):
        return "sin_prefijo_del_modulo"
    if nombre in RESERVADOS or nombre in nativas:
        return "choca_con_nativa"
    if nombre in vistos:
        return "duplicada"
    descripcion = item.get("description")
    if not isinstance(descripcion, str) or not descripcion.strip() or len(descripcion) > TOPE_DESCRIPCION:
        return "descripcion_invalida"
    parametros = item.get("parameters")
    if not isinstance(parametros, dict) or parametros.get("type") != "object":
        return "parametros_invalidos"
    if _bytes(parametros) > TOPE_ESQUEMA_BYTES:
        return "esquema_grande"
    return None


def _spec(nombre: str, modulo: str, descripcion: str, parametros: dict[str, Any]) -> ToolSpec:
    esquema = {
        "type": "function",
        "function": {"name": nombre, "description": descripcion, "parameters": parametros},
    }

    async def ejecutar(rt: Any, args: dict[str, Any]) -> dict[str, Any]:
        return await rt._ctx.crm.call_tool(rt._crm_conv_id, nombre, args if isinstance(args, dict) else {})

    return ToolSpec(nombre, f"{PREFIJO_CLAVE}{modulo}", lambda _caps: esquema, ejecutar)


def validar(context: dict[str, Any] | None) -> list[ToolSpec]:
    """Las herramientas de módulo VÁLIDAS del contexto de este turno."""
    from app.herramientas.builtin import REGISTRO

    crudas = (context or {}).get("moduleTools")
    if crudas is None:
        return []
    if not isinstance(crudas, list):
        logger.warning("moduleTools no es una lista (%s): se ignora", type(crudas).__name__)
        return []
    nativas = set(REGISTRO.nombres())
    vistos: set[str] = set()
    total = 0
    out: list[ToolSpec] = []
    for item in crudas:
        motivo = _motivo(item, nativas, vistos)
        nombre = item.get("name") if isinstance(item, dict) else None
        if motivo is None and len(out) >= TOPE_HERRAMIENTAS:
            motivo = "mas_de_8"
        if motivo is None:
            peso = _bytes(item["parameters"])
            if total + peso > TOPE_TOTAL_BYTES:
                motivo = "total_grande"
        if motivo is not None:
            logger.warning("herramienta de módulo %r descartada: %s", nombre, motivo)
            continue
        vistos.add(item["name"])
        total += peso
        out.append(_spec(item["name"], item["module"], item["description"], item["parameters"]))
    return out


def sabe_llamar_herramientas(crm: Any) -> bool:
    """¿El CRM del turno ejecuta herramientas de módulo (`call_tool`)?

    Se pregunta a la CLASE, no a la instancia: el CRM del ensayo y el
    centinela de multiorg revientan en `__getattr__` a propósito (con
    `RuntimeError`, que `getattr(..., None)` no ataja).
    """
    return callable(getattr(type(crm), "call_tool", None))


def registro_del_turno(context: dict[str, Any] | None, *, puede_ejecutar: bool) -> Registro:
    """El registro de ESTE turno: nativas + dinámicas válidas (nunca muta el global).

    `puede_ejecutar`: el CRM del turno sabe llamar herramientas de módulo
    (`sabe_llamar_herramientas`: `BrainsCrmClient.call_tool`). En `single`
    o en el ensayo, no: no se ofrecen aunque el contexto las traiga.
    """
    from app.herramientas.builtin import REGISTRO

    if not puede_ejecutar:
        return REGISTRO
    dinamicas = validar(context)
    if not dinamicas:
        return REGISTRO
    return Registro([*REGISTRO, *dinamicas])


def tiene_dinamicas(registro: Registro) -> bool:
    return any(es_clave_de_modulo(spec.clave) for spec in registro)

