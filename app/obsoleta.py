"""vocero-crm 073-C (D5) — ¿Ya llegó algo más nuevo de esta conversación?

El lead escribe «hola», Nea empieza a pensar (2–8 s con GLM) y el lead manda
«¿cuánto cuesta la limpieza?». El CRM entrega ese segundo despacho a la cola
durable mientras el primer turno sigue pensando. Antes Nea contestaba al
«hola» con un saludo y DESPUÉS a la pregunta: dos mensajes, el primero ya
viejo. Ahora, si el turno no ha tenido efectos (ni herramientas ni envíos) y
ya hay un despacho más nuevo en cola, la respuesta se descarta: el turno
siguiente ve los dos mensajes del lead (están en el historial) y contesta una
sola vez.

Como `app/efectos.py`, el chequeo viaja en un `ContextVar`: lo pone el worker
de la cola durable; fuera de ella (pruebas, ensayo, seguimiento) no hay
chequeo y nunca se descarta nada.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Awaitable, Callable, Iterator

logger = logging.getLogger("nea.obsoleta")

Chequeo = Callable[[], Awaitable[bool]]

_chequeo: ContextVar[Chequeo | None] = ContextVar("nea_despacho_mas_nuevo", default=None)


@contextmanager
def registrar_chequeo(chequeo: Chequeo) -> Iterator[None]:
    token = _chequeo.set(chequeo)
    try:
        yield
    finally:
        _chequeo.reset(token)


async def hay_despacho_mas_nuevo() -> bool:
    """¿Hay en cola un despacho más nuevo de esta misma conversación? Nunca lanza."""
    chequeo = _chequeo.get()
    if chequeo is None:
        return False
    try:
        return bool(await chequeo())
    except Exception as exc:  # ante la duda, se contesta (lo de siempre)
        logger.warning("no pude mirar si llegó un despacho más nuevo (%s) — contesto", exc)
        return False
