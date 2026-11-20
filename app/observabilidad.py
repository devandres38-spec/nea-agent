"""Logs de producción (vocero-crm 071, D8): JSON, con contexto y sin PII de más.

- **Contexto**: `org`, `dispatch` y `conversation` viajan en `contextvars` y
  aparecen en cada línea que se escribe mientras dura el turno, el envío
  diferido o el ensayo. Son ids del CRM, no datos de personas.
- **JSON** (`LOG_FORMAT=json`, el default en cloud): una línea por evento,
  lista para buscar por negocio o por despacho. Con la librería estándar: sin
  dependencias nuevas (Principio II).
- **Enmascarado**: toda secuencia de 8 o más dígitos (teléfonos, identidades
  de Meta) sale como `***` + sus últimos 4. Los mensajes de siempre llevan la
  identidad del lead en el texto; el filtro la tapa sin reescribirlos.

El filtro `SinTokenPropio` (app/main.py) sigue corriendo como antes.
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator

_org: ContextVar[str | None] = ContextVar("nea_org", default=None)
_dispatch: ContextVar[str | None] = ContextVar("nea_dispatch", default=None)
_conversation: ContextVar[str | None] = ContextVar("nea_conversation", default=None)

_DIGITOS = re.compile(r"\d{8,}")


def enmascarar(texto: str) -> str:
    return _DIGITOS.sub(lambda m: "***" + m.group(0)[-4:], texto)


@contextmanager
def contexto_de_log(
    *, org: str | None = None, dispatch: str | None = None, conversation: str | None = None
) -> Iterator[None]:
    """Pone el contexto de las líneas que se escriban dentro del bloque.

    Las tareas de asyncio copian el contexto al crearse: lo que se cree dentro
    del bloque lo hereda.
    """
    tokens = [
        (var, var.set(valor))
        for var, valor in ((_org, org), (_dispatch, dispatch), (_conversation, conversation))
        if valor is not None
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


_DESPACHO_VALIDO = re.compile(r"^[\w-]{1,64}$")


def despacho_actual() -> str | None:
    """vocero-crm 074 (E8) — El `dispatchId` del turno en curso, si tiene forma
    de id (va en la cabecera `X-Vocero-Dispatch` hacia el proxy del CRM)."""
    valor = _dispatch.get()
    return valor if valor and _DESPACHO_VALIDO.match(valor) else None


def contexto_actual() -> dict[str, str]:
    return {
        clave: valor
        for clave, valor in (
            ("org", _org.get()),
            ("dispatch", _dispatch.get()),
            ("conversation", _conversation.get()),
        )
        if valor
    }


class EnmascararDigitos(logging.Filter):
    """Tapa teléfonos e identidades en el mensaje Y en sus argumentos.

    Dentro de `args` y no reemplazando el mensaje formateado, por lo mismo
    que `SinTokenPropio`: el formateador de acceso de uvicorn desempaca
    `record.args`, y dejarlos en None lo rompe.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = enmascarar(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_enmascarar_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _enmascarar_arg(v) for k, v in record.args.items()}
        return True


def _enmascarar_arg(valor: Any) -> Any:
    """Auditoría 071 (M-1): una excepción pasada como argumento
    (`logger.warning("falló: %s", exc)`) lleva el teléfono en su texto."""
    if isinstance(valor, str):
        return enmascarar(valor)
    if isinstance(valor, BaseException):
        return enmascarar(str(valor))
    return valor


class FormatoJson(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cuerpo: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            # M-1: lo que llegue sin pasar por el filtro (otro handler, un
            # objeto con PII en su __str__) se tapa igual al formatear.
            "msg": enmascarar(record.getMessage()),
            **contexto_actual(),
        }
        if record.exc_info:
            cuerpo["exc"] = enmascarar(self.formatException(record.exc_info))
        return json.dumps(cuerpo, ensure_ascii=False)


class FormatoTexto(logging.Formatter):
    """El formato de siempre, más el contexto al final si lo hay."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        linea = super().format(record)
        ctx = contexto_actual()
        if ctx:
            linea += " [" + " ".join(f"{k}={v}" for k, v in ctx.items()) + "]"
        # M-1: incluye el traceback (`exc_info`), que el filtro no ve.
        return enmascarar(linea)


_LOGGERS_PROPIOS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configurar_logs(formato: str) -> None:
    """Formato y filtro en todos los handlers (raíz y los de uvicorn)."""
    formateador: logging.Formatter = FormatoJson() if formato == "json" else FormatoTexto()
    loggers = [logging.getLogger()] + [logging.getLogger(n) for n in _LOGGERS_PROPIOS]
    for logger in loggers:
        for handler in logger.handlers:
            # En texto, los de uvicorn conservan su formato de siempre y solo
            # reciben el filtro; en JSON todo sale igual.
            if formato == "json" or logger.name == "root":
                handler.setFormatter(formateador)
            if not any(isinstance(f, EnmascararDigitos) for f in handler.filters):
                handler.addFilter(EnmascararDigitos())
