"""Aceptación durable; un turno empezado e interrumpido nunca se repite.

vocero-crm 071 (D1) — La unidad de serialización es la CONVERSACIÓN
`(organization_id, conversation_id)`, no la organización:

- Se reclama el despacho más viejo sin terminar de cada conversación, y solo
  si ninguno de esa conversación está `started` con lease vivo. Así un negocio
  con muchos clientes los atiende en paralelo, y dos mensajes del mismo
  cliente (o su `conversation.deleted`) jamás corren a la vez ni se adelantan.
- El reclamo es una sola sentencia `UPDATE … RETURNING` con
  `FOR UPDATE SKIP LOCKED`: no hay advisory lock de sesión ni conexión
  retenida durante el turno. Vale igual con una réplica que con varias.
- El lease (3 min, renovado cada 20 s por el worker) da un margen antes de
  recuperar; el *owner fencing* impide que una ejecución tardía pise la
  recuperación. Recuperar = handoff, nunca repetir el turno.

Antes (070) se tomaba `pg_try_advisory_lock` por organización y se retenía la
conexión todo el turno (hasta 240 s): un negocio contestaba de uno en uno.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable
from uuid import uuid4

LEASE = timedelta(minutes=3)


@dataclass
class DispatchJob:
    payload: dict[str, Any]
    recovery: bool
    finish: Callable[[], Awaitable[None]]
    heartbeat: Callable[[], Awaitable[None]]
    # De quién es: el worker cuenta los turnos en vuelo por organización.
    organization_id: str = ""
    conversation_id: str = ""


_CLAIM = """
WITH candidata AS (
  SELECT d.organization_id, d.dispatch_id, d.state AS previo
  FROM dispatch_inbox d
  WHERE d.state <> 'done' AND d.available_at <= now()
    AND NOT (d.organization_id = ANY($2::text[]))
    AND NOT EXISTS (
      SELECT 1 FROM dispatch_inbox o
      WHERE o.organization_id = d.organization_id
        AND o.conversation_id = d.conversation_id
        AND o.state <> 'done'
        AND (o.created_at, o.dispatch_id) < (d.created_at, d.dispatch_id))
    AND NOT EXISTS (
      SELECT 1 FROM dispatch_inbox s
      WHERE s.organization_id = d.organization_id
        AND s.conversation_id = d.conversation_id
        AND s.state = 'started' AND s.available_at > now())
  ORDER BY d.created_at, d.dispatch_id
  FOR UPDATE OF d SKIP LOCKED
  LIMIT 1)
UPDATE dispatch_inbox t
SET state = 'started', owner = $1,
    available_at = now() + interval '3 minutes', updated_at = now()
FROM candidata c
WHERE t.organization_id = c.organization_id AND t.dispatch_id = c.dispatch_id
RETURNING t.organization_id, t.dispatch_id, t.conversation_id, t.payload,
          (c.previo <> 'queued') AS recovery
"""


class PgDispatchStore:
    pool: Any

    async def enqueue_dispatch(self, org: str, payload: dict[str, Any]) -> None:
        await self.pool.execute("""INSERT INTO dispatch_inbox
          (organization_id,dispatch_id,conversation_id,payload) VALUES ($1,$2,$3,$4::jsonb)
          ON CONFLICT DO NOTHING""", org, payload["dispatchId"], payload["conversation"]["id"], json.dumps(payload))

    async def forget_dispatches(self, org: str, conversation_id: str, keep: str) -> None:
        """The payload column holds the lead's messages verbatim. `keep` is the
        row being processed right now: finish() must still find it."""
        await self.pool.execute("""DELETE FROM dispatch_inbox
          WHERE organization_id=$1 AND conversation_id=$2 AND dispatch_id<>$3""", org, conversation_id, keep)

    @asynccontextmanager
    async def claim_dispatch(
        self, excluir: Iterable[str] = ()
    ) -> AsyncIterator[DispatchJob | None]:
        """El siguiente despacho que se puede atender, o None.

        `excluir`: organizaciones que ya están en su tope de turnos en esta
        réplica (`DISPATCH_MAX_POR_ORG`).
        """
        owner = uuid4().hex
        row = await self.pool.fetchrow(_CLAIM, owner, list(excluir))
        if row is None:
            yield None
            return
        org, dispatch_id = row["organization_id"], row["dispatch_id"]

        async def finish() -> None:
            result = await self.pool.execute("""UPDATE dispatch_inbox SET state='done',updated_at=now()
              WHERE organization_id=$1 AND dispatch_id=$2 AND owner=$3 AND state='started'""", org, dispatch_id, owner)
            if result != "UPDATE 1":
                raise RuntimeError("dispatch ownership lost")

        async def heartbeat() -> None:
            result = await self.pool.execute("""UPDATE dispatch_inbox SET available_at=now()+interval '3 minutes',updated_at=now()
              WHERE organization_id=$1 AND dispatch_id=$2 AND owner=$3 AND state='started'""", org, dispatch_id, owner)
            if result != "UPDATE 1":
                raise RuntimeError("dispatch ownership lost")

        yield DispatchJob(
            json.loads(row["payload"]),
            bool(row["recovery"]),
            finish,
            heartbeat,
            organization_id=org,
            conversation_id=row["conversation_id"],
        )


    async def queue_metrics(self) -> dict[str, int]:
        """071 (D9) — Para `/health`: cuántos esperan, cuántos corren y la
        antigüedad del más viejo sin terminar. Usa los índices parciales."""
        row = await self.pool.fetchrow("""
          SELECT count(*) FILTER (WHERE state <> 'started') AS pending,
                 count(*) FILTER (WHERE state = 'started') AS started,
                 GREATEST(0, COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0))::bigint AS oldest
          FROM dispatch_inbox WHERE state <> 'done'""")
        sends = await self.pool.fetchval("""SELECT count(*) FROM pending_send
          WHERE delivered_at IS NULL AND abandoned_at IS NULL""")
        return {
            "inboxPending": int(row["pending"]),
            "inboxStarted": int(row["started"]),
            "pendingSends": int(sends),
            "oldestInboxSeconds": int(row["oldest"]),
        }


class MemoryDispatchStore:
    """La misma semántica que Postgres, en memoria (solo pruebas)."""

    def _inbox(self) -> dict[Any, Any]:
        if not hasattr(self, "dispatch_inbox"):
            self.dispatch_inbox: dict[Any, Any] = {}
            self.dispatch_lock = asyncio.Lock()
            self._dispatch_seq = 0
        return self.dispatch_inbox

    async def enqueue_dispatch(self, org: str, payload: dict[str, Any]) -> None:
        inbox = self._inbox()
        if (org, payload["dispatchId"]) in inbox:
            return
        self._dispatch_seq += 1
        inbox[(org, payload["dispatchId"])] = {
            "payload": json.loads(json.dumps(payload)), "state": "queued",
            "available_at": datetime.now(timezone.utc), "owner": None,
            "conversation_id": payload["conversation"]["id"], "seq": self._dispatch_seq,
        }

    async def forget_dispatches(self, org: str, conversation_id: str, keep: str) -> None:
        inbox = self._inbox()
        for key in [k for k, row in inbox.items() if k[0] == org and k[1] != keep
                    and row["conversation_id"] == conversation_id]:
            del inbox[key]

    @asynccontextmanager
    async def claim_dispatch(
        self, excluir: Iterable[str] = ()
    ) -> AsyncIterator[DispatchJob | None]:
        inbox = self._inbox()
        excluidas = set(excluir)
        async with self.dispatch_lock:
            ahora = datetime.now(timezone.utc)
            vivas = sorted(
                ((k, r) for k, r in inbox.items() if r["state"] != "done"),
                key=lambda kr: kr[1]["seq"],
            )
            elegida = None
            vistas: set[tuple[str, str]] = set()
            for key, row in vivas:
                conv = (key[0], row["conversation_id"])
                # Solo el más viejo sin terminar de cada conversación.
                if conv in vistas:
                    continue
                vistas.add(conv)
                if key[0] in excluidas or row["available_at"] > ahora:
                    continue
                elegida = (key, row)
                break
            if elegida is None:
                job = None
            else:
                key, row = elegida
                recovery = row["state"] != "queued"
                owner = uuid4().hex
                row.update(state="started", owner=owner, available_at=ahora + LEASE)

                async def finish(row: dict[str, Any] = row, owner: str = owner) -> None:
                    if row.get("owner") != owner or row["state"] != "started":
                        raise RuntimeError("dispatch ownership lost")
                    row["state"] = "done"
                    row["done_at"] = datetime.now(timezone.utc)

                async def heartbeat(row: dict[str, Any] = row, owner: str = owner) -> None:
                    if row.get("owner") != owner or row["state"] != "started":
                        raise RuntimeError("dispatch ownership lost")
                    row["available_at"] = datetime.now(timezone.utc) + LEASE

                job = DispatchJob(
                    row["payload"], recovery, finish, heartbeat,
                    organization_id=key[0], conversation_id=row["conversation_id"],
                )
        yield job

    async def queue_metrics(self) -> dict[str, int]:
        filas = [r for r in self._inbox().values() if r["state"] != "done"]
        pendientes = [
            p for p in getattr(self, "pending_sends", {}).values()
            if p.delivered_at is None and p.abandoned_at is None
        ]
        return {
            "inboxPending": sum(1 for r in filas if r["state"] != "started"),
            "inboxStarted": sum(1 for r in filas if r["state"] == "started"),
            "pendingSends": len(pendientes),
            "oldestInboxSeconds": 0,
        }
