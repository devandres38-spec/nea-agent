"""Reintento diferido de envíos: lo que el turno no pudo entregar, sale después.

Loop asyncio cada 30 s sobre `pending_send` (encolado por app/turn.py cuando el
envío agota sus reintentos). Backoff exponencial con tope de 15 min; se
abandona si el CRM lo rechaza con 409 (ai_paused / window_closed) o al agotar
24 h — en ese caso se registra handoff `error` para que un humano vea la
conversación en el CRM (incidente 2026-08-03: antes la respuesta se descartaba
en silencio y el lead no recibía nada).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from app.crm import CrmConflict, CrmError, CrmRechazo
from app.multiorg import contexto_del_turno, ctx_de_organizacion
from app.observabilidad import contexto_de_log
from app.state import AppContext, PendingSend, utcnow

logger = logging.getLogger("nea.sender")

# Auditoría 071 (A-3): el lote cabe en su lease. Cada envío puede tardar hasta
# 15 s (timeout del CRM); con 50 filas y 120 s de lease, las del final vencían
# mientras esta réplica seguía y otra las volvía a reclamar y enviar.
LOTE = 10
LEASE_SECONDS = 180  # ≥ LOTE × 15 s + margen
# No se empieza un envío al que no le queda este margen de lease.
MARGEN_DEL_LEASE = timedelta(seconds=20)


class SenderWorker:
    INTERVAL = 30.0
    MAX_AGE = timedelta(hours=24)  # la ventana de WhatsApp ya habrá cerrado
    BACKOFF_BASE = 30.0
    BACKOFF_CAP = 900.0  # 15 min entre intentos, máximo

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.INTERVAL)
            try:
                await self.tick()
            except Exception:
                logger.exception("sender: fallo en el barrido")

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        # 071 (D6): reservados para este worker (lease), no solo leídos: con
        # dos réplicas, leer sin reservar entregaba dos veces la misma fila.
        reclamar = getattr(self._ctx.store, "claim_pending_sends", None)
        con_lease = callable(reclamar)
        pendientes = (
            await reclamar(now, limit=LOTE, lease_seconds=LEASE_SECONDS) if con_lease
            else await self._ctx.store.due_pending_sends(now)
        )
        inicio = time.monotonic()
        for item in pendientes:
            if con_lease:
                # A-3: el reloj de esta pasada (el `now` del reclamo + lo que
                # ya se tardó). Sin margen de lease, la fila puede ser ya de
                # otra réplica: se deja, y vuelve sola al vencer.
                reloj = now + timedelta(seconds=time.monotonic() - inicio)
                if reloj + MARGEN_DEL_LEASE >= item.next_retry_at:
                    logger.warning(
                        "sender: sin margen de lease para pending %d y siguientes — quedan para después",
                        item.id,
                    )
                    break
            # Con quién hay que hablar para ESTA fila. En una Nea de un solo
            # negocio es siempre el mismo; en una multi-organización, el de
            # la organización que dejó el envío pendiente.
            ctx = ctx_de_organizacion(
                self._ctx, item.organization_id, item.organization_slug
            )
            if ctx is None:
                logger.error(
                    "sender: pending %d sin organización conocida — no puedo "
                    "entregarlo sin arriesgarme a escribirle a otro negocio",
                    item.id,
                )
                await self._ctx.store.mark_pending_send_abandoned(item.id)
                continue
            if not self._ctx.settings.multi_org:
                # 071 (D3): lo que el envío le enseña al cliente (el despacho
                # que contesta) vive en una vista, no en el cliente de la app.
                ctx = contexto_del_turno(ctx)
            if now - item.created_at > self.MAX_AGE:
                logger.error(
                    "sender: pending %d agotó las 24 h sin entregar — abandonado + handoff",
                    item.id,
                )
                await self._ctx.store.mark_pending_send_abandoned(item.id)
                await self._handoff(ctx, item.crm_conversation_id)
                continue
            with contexto_de_log(
                org=item.organization_slug or item.organization_id or None,
                dispatch=item.dispatch_id or None,
                conversation=item.crm_conversation_id or None,
            ):
                await self._attempt(ctx, item, now)

    async def _attempt(
        self, ctx: AppContext, item: PendingSend, now: datetime
    ) -> None:
        # Qué despacho estaba contestando. El CRM lo exige para responder por
        # el cerebro, y aquí el turno que lo sabía ya no existe: sale de la
        # fila. Sin esto el reintento diferido se rechazaba con 422 hasta
        # agotar las 24 h — la cola entera existía para nada.
        registrar_despacho = getattr(ctx.crm, "registrar_despacho", None)
        if callable(registrar_despacho):
            registrar_despacho(item.crm_conversation_id, item.dispatch_id)
        try:
            await ctx.crm.send_message(item.crm_conversation_id, item.content)
        except CrmRechazo as exc:
            # 071 (D6, H7): el CRM ya no acepta la credencial de este negocio
            # (reasignado, pausado, rotado, suspendido). Terminal, como en la
            # cola del despacho (C-1 de la 070): ni reintento —cada intento
            # suma al límite del CRM— ni handoff, que tampoco tendría
            # credencial. El mensaje del cliente sigue en la bandeja del CRM.
            logger.warning(
                "sender: pending %d — el CRM rechazó la credencial (%s) de %s — abandonado, sin handoff",
                item.id,
                exc.status,
                item.organization_slug or item.organization_id or "este negocio",
            )
            await ctx.store.mark_pending_send_abandoned(item.id)
            return
        except CrmConflict as exc:
            # ai_paused / window_closed: ya no procede — silencio respetuoso.
            logger.info(
                "sender: pending %d rechazado por el CRM (%s) — abandonado",
                item.id,
                exc.code,
            )
            await ctx.store.mark_pending_send_abandoned(item.id)
            return
        except CrmError as exc:
            attempts = item.attempts + 1
            delay = min(self.BACKOFF_BASE * (2.0**item.attempts), self.BACKOFF_CAP)
            logger.warning(
                "sender: pending %d falló (intento %d): %s — reintento en %.0f s",
                item.id,
                attempts,
                exc,
                delay,
            )
            await ctx.store.reschedule_pending_send(
                item.id, attempts, now + timedelta(seconds=delay)
            )
            return
        await ctx.store.mark_pending_send_delivered(item.id)
        # Al historial local: Nea debe recordar lo que (por fin) dijo.
        await ctx.store.add_message(item.conversation_id, "assistant", item.content)
        logger.info("sender: pending %d entregado", item.id)

    async def _handoff(self, ctx: AppContext, crm_conv_id: str) -> None:
        try:
            await ctx.crm.post_handoff(crm_conv_id, "error")
        except CrmRechazo as exc:
            logger.warning("sender: sin credencial para el handoff (%s) — solo log", exc.status)
        except CrmError as exc:
            logger.error("sender: no pude registrar el handoff tras abandono: %s", exc)
