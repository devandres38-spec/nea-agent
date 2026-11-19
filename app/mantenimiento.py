"""Retención (vocero-crm 071, D7): lo viejo se borra, en lotes, cada hora.

Qué y por qué:

- `dispatch_inbox` terminados: su `payload` guarda los mensajes del cliente
  TAL CUAL. Pasada la ventana en que el CRM puede reintentar un despacho (24 h)
  ya no sirven para nada, y conservarlos es guardar datos personales de más.
- `processed_message`: las marcas de deduplicación. Mismo razonamiento.
- `pending_send` entregados o abandonados: ya no se van a reintentar.

`RETENTION_DAYS` (14, mínimo 2) para los dos primeros y
`PENDING_RETENTION_DAYS` (30) para el tercero. La memoria de la conversación
(`bot_message`) NO se toca: se borra con el olvido que manda el CRM.

`relay_queue` (Nea 1.x) tampoco: las migraciones 001 y 007 la recrean en cada
arranque. Vacía, no cuesta nada.
"""
from __future__ import annotations

import asyncio
import logging

from app.state import AppContext

logger = logging.getLogger("nea.mantenimiento")


class MantenimientoWorker:
    INTERVAL = 3600.0
    # Antes del primer barrido: que el arranque (migraciones, despacho) vaya primero.
    PRIMERA_ESPERA = 120.0

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def run(self) -> None:
        await asyncio.sleep(self.PRIMERA_ESPERA)
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("mantenimiento: fallo en la purga — se reintenta en una hora")
            await asyncio.sleep(self.INTERVAL)

    async def tick(self) -> dict[str, int] | None:
        purgar = getattr(self._ctx.store, "purgar", None)
        if not callable(purgar):
            return None
        s = self._ctx.settings
        borradas = await purgar(s.retention_days, s.pending_retention_days)
        if borradas is None:
            logger.info("mantenimiento: otra réplica está purgando — esta se salta el turno")
        elif any(borradas.values()):
            logger.info(
                "mantenimiento: purga — %s",
                ", ".join(f"{tabla} {n}" for tabla, n in borradas.items()),
            )
        return borradas
