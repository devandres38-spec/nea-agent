"""La ficha según el negocio (vocero-crm 072, D11).

Cuando el CRM manda `fichaSchema` en el contexto (los campos que el negocio
definió en Ajustes → Ficha), `update_ficha` ofrece EXACTAMENTE esos campos,
con su tipo, más `notas`. Sin esquema, el de siempre (B2B), byte a byte: las
pruebas doradas de la 071 no se mueven.

Todo lo que viene del contexto se valida a la defensiva: una clave rara, un
tipo desconocido o una descripción larga se descartan o se recortan, nunca
tumban el turno. El CRM ya valida al guardar; esto es el segundo candado.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

logger = logging.getLogger("nea.herramientas.ficha")

CLAVE_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
TIPOS = {"text", "number", "date", "select", "boolean"}
TOPE_CAMPOS = 20
TOPE_OPCIONES = 30
TOPE_DESCRIPCION = 200
# Cuántos datos pendientes se le recuerdan al modelo a la vez (D11).
TOPE_FALTANTES = 3
RESERVADAS = {"notas", "constructor", "prototype"}
# M-9 (auditoría 072-A) — Presupuesto del esquema dinámico (caracteres del JSON
# de propiedades, ~2k tokens): si el negocio lo excede, se usan los primeros
# campos que caben (en su orden) y se avisa en el log.
TOPE_ESQUEMA_CHARS = 8000
_SUFIJO_FECHA = " (AAAA-MM-DD)"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _linea(valor: Any, tope: int) -> str:
    """Una línea, sin caracteres de control y acotada."""
    if not isinstance(valor, str):
        return ""
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", valor)).strip()[:tope]


def campos_del_contexto(context: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Los campos válidos de `fichaSchema`, en orden; `[]` si no hay esquema."""
    esquema = (context or {}).get("fichaSchema") if isinstance(context, dict) else None
    if not isinstance(esquema, dict) or esquema.get("version") != 1:
        return []
    crudos = esquema.get("fields")
    if not isinstance(crudos, list):
        return []
    campos: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for c in crudos:
        if len(campos) >= TOPE_CAMPOS:
            logger.warning("fichaSchema con más de %d campos: se usan los primeros", TOPE_CAMPOS)
            break
        if not isinstance(c, dict):
            continue
        clave = c.get("key")
        tipo = c.get("type")
        if not isinstance(clave, str) or not CLAVE_RE.match(clave) or clave in RESERVADAS or clave in vistos:
            logger.warning("fichaSchema: clave descartada %r", clave)
            continue
        if tipo not in TIPOS:
            logger.warning("fichaSchema: tipo desconocido %r en %s, se descarta", tipo, clave)
            continue
        etiqueta = _linea(c.get("label"), 60) or clave
        campo: dict[str, Any] = {"key": clave, "label": etiqueta, "type": tipo, "required": c.get("required") is True}
        descripcion = _linea(c.get("description"), TOPE_DESCRIPCION)
        if descripcion:
            campo["description"] = descripcion
        if tipo == "select":
            # A-8 (auditoría 072-A) — `options` debe ser una LISTA: un int/bool
            # tumbaba el turno (TypeError) y un str se iteraba letra por letra.
            # Y sin repetidas: `"enum": ["A", "A"]` no es un JSON Schema válido.
            crudas = c.get("options")
            if not isinstance(crudas, list):
                crudas = []
            opciones = [_linea(o, 60) for o in crudas if isinstance(o, str)]
            opciones = list(dict.fromkeys(o for o in opciones if o))[:TOPE_OPCIONES]
            if not opciones:
                logger.warning("fichaSchema: lista %s sin opciones, se descarta", clave)
                continue
            campo["options"] = opciones
        vistos.add(clave)
        campos.append(campo)
    return campos


def _propiedad(campo: dict[str, Any]) -> dict[str, Any]:
    descripcion = campo["label"]
    if campo.get("description"):
        descripcion = f"{descripcion} — {campo['description']}"
    tipo = campo["type"]
    if tipo == "number":
        return {"type": "number", "description": descripcion[:TOPE_DESCRIPCION]}
    if tipo == "boolean":
        return {"type": "boolean", "description": descripcion[:TOPE_DESCRIPCION]}
    if tipo == "date":
        # A-8 — Se recorta la descripción, nunca el formato: el modelo necesita
        # ver «(AAAA-MM-DD)» aunque la etiqueta y la indicación sean largas.
        return {"type": "string", "description": f"{descripcion[:TOPE_DESCRIPCION]}{_SUFIJO_FECHA}"}
    if tipo == "select":
        return {"type": "string", "enum": list(campo["options"]), "description": descripcion[:TOPE_DESCRIPCION]}
    return {"type": "string", "description": descripcion[:TOPE_DESCRIPCION]}


def esquema_dinamico(base: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any] | None:
    """`update_ficha` con los campos del negocio + `notas`; `None` sin esquema."""
    campos = campos_del_contexto(context)
    if not campos:
        return None
    esquema = copy.deepcopy(base)
    funcion = esquema["function"]
    funcion["description"] = (
        "Guarda o actualiza la ficha del cliente en el CRM (merge: solo los "
        "campos que mandes). Llámala en cuanto descubras un dato nuevo. Usa "
        "estos campos, con su tipo; lo que no encaje en ninguno, en notas."
    )
    propiedades: dict[str, Any] = {}
    for c in campos:
        prop = _propiedad(c)
        if len(json.dumps({**propiedades, c["key"]: prop}, ensure_ascii=False)) > TOPE_ESQUEMA_CHARS:
            logger.warning(
                "fichaSchema excede %d caracteres: se usan los primeros %d campos", TOPE_ESQUEMA_CHARS, len(propiedades)
            )
            break
        propiedades[c["key"]] = prop
    propiedades["notas"] = {"type": "string", "description": "Otro dato útil que no encaja en los campos"}
    funcion["parameters"] = {"type": "object", "properties": propiedades}
    return esquema


def faltantes(context: dict[str, Any] | None, tope: int = TOPE_FALTANTES) -> list[dict[str, Any]]:
    """Los obligatorios todavía sin valor en `contact.ficha`, en orden (máx. `tope`)."""
    campos = campos_del_contexto(context)
    if not campos:
        return []
    ficha = ((context or {}).get("contact") or {}).get("ficha") or {}
    if not isinstance(ficha, dict):
        ficha = {}
    pendientes = [c for c in campos if c["required"] and ficha.get(c["key"]) in (None, "", [])]
    return pendientes[:tope]


def linea_datos_por_reunir(context: dict[str, Any] | None) -> str | None:
    """La línea del CONTEXTO ACTUAL; `None` si no falta nada (o no hay esquema)."""
    pendientes = faltantes(context)
    if not pendientes:
        return None
    # M-9 (auditoría 072-A) — Las etiquetas las escribe el negocio: van como
    # DATOS delimitados (una lista JSON), nunca como texto suelto del prompt.
    nombres = json.dumps([c["label"] for c in pendientes], ensure_ascii=False)
    return (
        "- Datos por reunir (uno a la vez, en este orden, sin repetir lo que ya "
        "te dijo; nunca condiciones una cita ni una cotización a ellos): "
        f"{nombres}."
    )
