"""Consumidores acotados del despacho (vocero-crm 071, D1–D2).

`DISPATCH_CONCURRENCY` tareas por proceso reclaman de la cola durable. La
cola serializa por conversación (app/dispatch_store.py); aquí solo se acota
cuántos turnos corren a la vez y cuántos puede ocupar un mismo negocio
(`DISPATCH_MAX_POR_ORG`), para que una ráfaga de uno —o su llave BYOK con
límite de tasa— no deje esperando a los demás.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from app.crm import CrmRechazo
from app.multiorg import organizacion_del_despacho
from app.observabilidad import contexto_de_log

logger = logging.getLogger("nea.dispatch.worker")


def _rechazo(payload: dict[str, Any], exc: CrmRechazo) -> None:
    """vocero-crm 070 (C-1): el CRM ya no acepta la credencial de esta
    organización. Terminal: se registra y el despacho sale de la cola.

    NO se intenta el handoff —tampoco tendría credencial— ni se reintenta:
    cada intento cuenta como un fallo en el límite por IP del CRM, que
    comparten todos los negocios de esta Nea, y un solo negocio reasignado
    acabaría dejando sin respuesta a los demás. El mensaje del cliente ya está
    en la bandeja del CRM desde antes del despacho: no se pierde.
    """
    org = organizacion_del_despacho(payload) or ("?", "?")
    logger.warning(
        "despacho %s de %s (%s): el CRM rechazó la credencial (%s) — fuera de la cola, sin handoff",
        payload.get("dispatchId"),
        org[1],
        org[0],
        exc.status,
    )


async def handoff(ctx: Any, payload: dict[str, Any]) -> None:
    client = ctx.crm
    if ctx.settings.multi_org:
        org = organizacion_del_despacho(payload)
        if org is None or ctx.registro is None:
            raise ValueError("missing dispatch organization")
        client = ctx.registro.cliente(*org)
    try:
        await client.post_handoff(payload["conversation"]["id"], "error")
    except CrmRechazo as exc:
        _rechazo(payload, exc)


def _al_tope(en_vuelo: Counter[str] | None, maximo: int) -> list[str]:
    if en_vuelo is None or maximo <= 0:
        return []
    return [org for org, n in en_vuelo.items() if n >= maximo]


async def drain_one(ctx: Any, en_vuelo: Counter[str] | None = None) -> bool:
    """Atiende UN despacho si hay alguno disponible. False = cola vacía.

    `en_vuelo` lo comparten los consumidores de este proceso: turnos que
    corren ahora, por organización.
    """
    maximo = int(getattr(ctx.settings, "dispatch_max_por_org", 0) or 0)
    async with ctx.store.claim_dispatch(_al_tope(en_vuelo, maximo)) as job:
        if job is None:
            return False
        if en_vuelo is not None:
            en_vuelo[job.organization_id] += 1
        org = organizacion_del_despacho(job.payload)
        try:
            # 071 (D8): cada línea del turno dice de qué negocio, despacho y
            # conversación es. La tarea del turno hereda este contexto.
            with contexto_de_log(
                org=(org[1] if org else job.organization_id) or None,
                dispatch=str(job.payload.get("dispatchId") or "") or None,
                conversation=job.conversation_id or None,
            ):
                return await _atender(ctx, job)
        finally:
            if en_vuelo is not None:
                en_vuelo[job.organization_id] -= 1
                if en_vuelo[job.organization_id] <= 0:
                    del en_vuelo[job.organization_id]


async def _atender(ctx: Any, job: Any) -> bool:
    """Corre el turno (o el olvido, o la recuperación) con su lease vivo."""
    from app.dispatch import EVENTO_OLVIDO, _procesar, olvidar

    async def execute() -> None:
        if job.payload.get("type") == EVENTO_OLVIDO:
            # Not a turn: nothing to hand off, and replaying it is safe.
            await olvidar(ctx, job.payload)
            return
        expires = job.payload.get("expiresAt")
        expired = bool(expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc))
        if job.recovery or expired:
            await handoff(ctx, job.payload)
        else:
            try:
                await _procesar(ctx, job.payload, durable=True)
            except CrmRechazo as exc:
                _rechazo(job.payload, exc)
            except Exception:
                logger.exception("dispatch failed; handing off without replay")
                await handoff(ctx, job.payload)

    task = asyncio.create_task(execute())
    try:
        async with asyncio.timeout(240):
            while not task.done():
                await asyncio.wait({task}, timeout=20)
                if not task.done():
                    await job.heartbeat()
            await task
        await job.finish()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return True


async def _consumidor(ctx: Any, en_vuelo: Counter[str]) -> None:
    while True:
        try:
            if await drain_one(ctx, en_vuelo):
                continue
        except Exception:
            logger.exception("dispatch remains durable for recovery")
        # El aviso es solo un atajo: si otro consumidor lo apaga antes, este
        # vuelve a mirar la cola a los 2 s de todos modos.
        ctx.dispatch_wake.clear()
        try:
            await asyncio.wait_for(ctx.dispatch_wake.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


async def run(ctx: Any) -> None:
    """`DISPATCH_CONCURRENCY` consumidores; se cancelan juntos al apagar."""
    n = max(1, int(getattr(ctx.settings, "dispatch_concurrency", 1) or 1))
    en_vuelo: Counter[str] = Counter()
    tareas = [
        asyncio.create_task(_consumidor(ctx, en_vuelo), name=f"dispatch-{i}")
        for i in range(n)
    ]
    logger.info("despacho: %d consumidores (máx. %s por negocio)", n,
                getattr(ctx.settings, "dispatch_max_por_org", "∞"))
    try:
        await asyncio.gather(*tareas)
    finally:
        for t in tareas:
            t.cancel()
        await asyncio.gather(*tareas, return_exceptions=True)
