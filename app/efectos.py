"""073-A (D3) — ¿El turno ya hizo algo que el lead (o el CRM) vea?

Si Nea muere a medio turno (redeploy, OOM), la cola durable recupera el
despacho. Repetir un turno que ya envió un mensaje o ejecutó una herramienta
sería contestarle dos veces al lead o reservar dos citas: eso termina en
handoff, como siempre. Pero uno que solo leyó y pensó se puede volver a
correr sin riesgo — y así un redeploy no le apaga la IA a nadie.

La marca es PESIMISTA: se escribe ANTES del efecto. Si Nea muere entre la
marca y el efecto, el turno pasa a una persona en vez de repetirse: el error
cae del lado seguro.

El marcador viaja en un `ContextVar` (como el contexto de los logs): el
turno no tiene que saber nada de la cola que lo está corriendo.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Awaitable, Callable, Iterator

logger = logging.getLogger("nea.efectos")

Marcador = Callable[[], Awaitable[None]]

_marcador: ContextVar[Marcador | None] = ContextVar("nea_marcador_de_efectos", default=None)


@contextmanager
def registrar_efectos(marcador: Marcador) -> Iterator[None]:
    """Lo que corra dentro (y las tareas que cree) marca con `marcador`."""
    marcado = False

    async def una_vez() -> None:
        nonlocal marcado
        if marcado:
            return
        marcado = True
        await marcador()

    token = _marcador.set(una_vez)
    try:
        yield
    finally:
        _marcador.reset(token)


async def marcar_efecto() -> None:
    """Anota que este turno va a tener un efecto. Nunca lanza.

    Fuera de la cola durable (pruebas, el ensayo, el seguimiento) no hay
    marcador y no hace nada.
    """
    marcador = _marcador.get()
    if marcador is None:
        return
    try:
        await marcador()
    except Exception as exc:  # la marca nunca le cuesta el efecto al turno
        logger.warning("no pude anotar el efecto del turno (%s) — sigo", exc)
