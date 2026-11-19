"""Persistencia real del bot: pool asyncpg + migraciones SQL idempotentes.

Implementa el protocolo `Store` de app/state.py contra Postgres.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg
from app.dispatch_store import PgDispatchStore

from app.state import (
    COLUMNAS_DE_CONVERSACION,
    BotMessage,
    Conversation,
    OfferedSlot,
    PendingSend,
)

logger = logging.getLogger("nea.db")

# La misma lista que usa MemoryStore (ver app/state.py).
_CONV_COLUMNS = COLUMNAS_DE_CONVERSACION


def _conv_from_row(row: asyncpg.Record) -> Conversation:
    return Conversation(
        id=row["id"],
        wa_identity=row["wa_identity"],
        crm_conversation_id=row["crm_conversation_id"],
        phase=row["phase"],
        greeted=row["greeted"],
        media_notice_sent=row["media_notice_sent"],
        followup_due_at=row["followup_due_at"],
        followup_sent=row["followup_sent"],
        last_inbound_at=row["last_inbound_at"],
        stalled_at=row["stalled_at"],
        stall_since_message_id=row["stall_since_message_id"],
        organization_id=row["organization_id"],
        organization_slug=row["organization_slug"],
    )


def _pending_send_desde_fila(row: Any) -> PendingSend:
    """Una fila de `pending_send` → su dataclass.

    Existe como función, y no suelto dentro de la consulta, por el fallo que
    arregla: el mapeo iba campo a campo y se quedó SIN LEER las columnas de
    organización que 004 había añadido. La fila las guardaba bien; al releerla
    volvían vacías, así que el SenderWorker no sabía de quién era el envío y no
    entregaba ninguno. La cola de "jamás se descarta" estaba muerta.

    Las pruebas no lo vieron porque `MemoryStore` devuelve el objeto que
    guardó: ahí no hay mapeo que equivocar. El único sitio donde este error
    existe es el que las pruebas no tocan.
    """
    return PendingSend(
        id=row["id"],
        conversation_id=row["conversation_id"],
        crm_conversation_id=row["crm_conversation_id"],
        content=row["content"],
        attempts=row["attempts"],
        created_at=row["created_at"],
        next_retry_at=row["next_retry_at"],
        delivered_at=row["delivered_at"],
        abandoned_at=row["abandoned_at"],
        organization_id=row["organization_id"],
        organization_slug=row["organization_slug"],
        dispatch_id=row["dispatch_id"],
    )


class PgStore(PgDispatchStore):
    """Store respaldado por Postgres (asyncpg)."""

    def __init__(self, dsn: str, schema: str = "", pool_max: int = 5) -> None:
        self._dsn = dsn
        # 071 (D2) — `Settings.pool_max`: un turno por consumidor del
        # despacho más los workers de fondo. El reclamo ya no retiene una
        # conexión durante el turno, así que no hace falta más.
        self._pool_max = max(2, pool_max)
        # 031 — Vacío = `public`, como siempre. Con nombre, las tablas de Nea
        # viven en ese esquema y la base se puede compartir con el CRM. El
        # nombre ya llega validado por `Settings` (minúsculas, dígitos y `_`).
        self._schema = schema
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PgStore sin conectar — llama connect() primero")
        return self._pool

    async def connect(self) -> None:
        if not self._schema:
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=2, max_size=self._pool_max
            )
            return
        # El esquema tiene que existir ANTES de abrir el pool: con un
        # `search_path` que apunta a un esquema que no existe, Postgres acepta
        # la conexión y el primer CREATE TABLE falla con «no schema has been
        # selected».
        conn = await asyncpg.connect(self._dsn)
        try:
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
        finally:
            await conn.close()
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=2,
            max_size=self._pool_max,
            server_settings={"search_path": self._schema},
        )
        logger.info("tablas de Nea en el esquema %s", self._schema)

    async def migrate(self, migrations_dir: Path) -> None:
        """Aplica todas las migraciones en orden. Son idempotentes: re-correr es seguro."""
        files = sorted(migrations_dir.glob("*.sql"))
        async with self.pool.acquire() as conn:
            # 071 (H9) — Con varias réplicas arrancando a la vez, dos
            # `CREATE INDEX IF NOT EXISTS` pueden chocar. Una a la vez; el
            # candado de sesión se suelta solo si el proceso muere.
            await conn.execute("SELECT pg_advisory_lock(hashtextextended('nea:migrate', 0))")
            try:
                for path in files:
                    logger.info("migración: aplicando %s", path.name)
                    await conn.execute(path.read_text(encoding="utf-8"))
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtextextended('nea:migrate', 0))")

    # -------------------------------------------------------------- dedup ---

    async def mark_processed(self, wa_message_id: str) -> bool:
        row = await self.pool.fetchrow(
            """
            INSERT INTO processed_message (wa_message_id) VALUES ($1)
            ON CONFLICT DO NOTHING
            RETURNING wa_message_id
            """,
            wa_message_id,
        )
        return row is not None

    async def already_processed(self, marcas: list[str]) -> set[str]:
        if not marcas:
            return set()
        rows = await self.pool.fetch(
            "SELECT wa_message_id FROM processed_message"
            " WHERE wa_message_id = ANY($1::text[])",
            marcas,
        )
        return {r["wa_message_id"] for r in rows}

    # ----------------------------------------------------- conversaciones ---

    async def get_or_create_conversation(
        self,
        wa_identity: str,
        organization_id: str = "",
        organization_slug: str = "",
    ) -> Conversation:
        # El conflicto se resuelve contra (organization_id, wa_identity): la
        # identidad sola dejó de identificar una conversación en cuanto una
        # Nea atiende a varios negocios. Ver migrations/004_multiorg.sql.
        #
        # El slug se refresca en cada choque porque puede cambiar (un miembro
        # renombra su subdominio) mientras el id no; guardarlo desactualizado
        # haría que el worker de fondo hablara con una cabecera vieja.
        row = await self.pool.fetchrow(
            """
            INSERT INTO bot_conversation
              (wa_identity, organization_id, organization_slug)
            VALUES ($1, $2, $3)
            ON CONFLICT (organization_id, wa_identity) DO UPDATE
              SET updated_at = now(), organization_slug = EXCLUDED.organization_slug
            RETURNING *
            """,
            wa_identity,
            organization_id,
            organization_slug,
        )
        assert row is not None
        return _conv_from_row(row)

    async def update_conversation(self, conversation_id: int, **fields: Any) -> None:
        unknown = set(fields) - _CONV_COLUMNS
        if unknown:
            raise ValueError(f"columnas desconocidas en update_conversation: {unknown}")
        if not fields:
            return
        sets = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(fields))
        await self.pool.execute(
            f"UPDATE bot_conversation SET {sets}, updated_at = now() WHERE id = $1",
            conversation_id,
            *fields.values(),
        )

    async def reset_conversation(self, conversation_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM bot_message WHERE conversation_id = $1",
                    conversation_id,
                )
                await conn.execute(
                    "DELETE FROM offered_slots WHERE conversation_id = $1",
                    conversation_id,
                )
                await conn.execute(
                    """
                    UPDATE bot_conversation
                    SET phase = 'descubrimiento', greeted = FALSE,
                        media_notice_sent = FALSE, followup_due_at = NULL,
                        followup_sent = FALSE, stalled_at = NULL,
                        updated_at = now()
                    WHERE id = $1
                    """,
                    conversation_id,
                )

    async def forget_conversation(
        self, organization_id: str, wa_identity: str, crm_conversation_id: str
    ) -> bool:
        # Una sola sentencia: `bot_message`, `offered_slots` y `pending_send`
        # cuelgan de la fila con ON DELETE CASCADE (001 y 002), así que no hay
        # orden que equivocar ni estado a medias si el proceso muere.
        row = await self.pool.fetchrow(
            """
            DELETE FROM bot_conversation
            WHERE organization_id = $1 AND wa_identity = $2
              AND (crm_conversation_id IS NULL OR crm_conversation_id = $3)
            RETURNING id
            """,
            organization_id,
            wa_identity,
            crm_conversation_id,
        )
        return row is not None

    # ----------------------------------------------------------- mensajes ---

    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        wa_message_id: str | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO bot_message (conversation_id, role, content, wa_message_id)
            VALUES ($1, $2, $3, $4)
            """,
            conversation_id,
            role,
            content,
            wa_message_id,
        )

    async def recent_messages(
        self, conversation_id: int, limit: int
    ) -> list[BotMessage]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM bot_message WHERE conversation_id = $1
            ORDER BY id DESC LIMIT $2
            """,
            conversation_id,
            limit,
        )
        return [
            BotMessage(
                id=r["id"],
                conversation_id=r["conversation_id"],
                role=r["role"],
                content=r["content"],
                wa_message_id=r["wa_message_id"],
                created_at=r["created_at"],
            )
            for r in reversed(rows)
        ]

    # -------------------------------------------------------------- slots ---

    async def replace_offered_slots(
        self, conversation_id: int, slots: list[OfferedSlot]
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM offered_slots WHERE conversation_id = $1",
                    conversation_id,
                )
                for slot in slots:
                    await conn.execute(
                        """
                        INSERT INTO offered_slots (conversation_id, start_utc, end_utc, label)
                        VALUES ($1, $2, $3, $4)
                        """,
                        conversation_id,
                        slot.start_utc,
                        slot.end_utc,
                        slot.label,
                    )

    async def get_offered_slots(self, conversation_id: int) -> list[OfferedSlot]:
        rows = await self.pool.fetch(
            "SELECT * FROM offered_slots WHERE conversation_id = $1 ORDER BY start_utc",
            conversation_id,
        )
        return [
            OfferedSlot(
                conversation_id=r["conversation_id"],
                start_utc=r["start_utc"],
                end_utc=r["end_utc"],
                label=r["label"],
                offered_at=r["offered_at"],
            )
            for r in rows
        ]

    async def clear_offered_slots(self, conversation_id: int) -> None:
        await self.pool.execute(
            "DELETE FROM offered_slots WHERE conversation_id = $1", conversation_id
        )

    # ------------------------------------------------- envíos pendientes ---

    async def enqueue_pending_send(
        self,
        conversation_id: int,
        crm_conversation_id: str,
        content: str,
        organization_id: str = "",
        organization_slug: str = "",
        dispatch_id: str = "",
    ) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO pending_send
              (conversation_id, crm_conversation_id, content,
               organization_id, organization_slug, dispatch_id)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
            """,
            conversation_id,
            crm_conversation_id,
            content,
            organization_id,
            organization_slug,
            dispatch_id,
        )
        assert row is not None
        return row["id"]

    async def due_pending_sends(self, now: datetime) -> list[PendingSend]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM pending_send
            WHERE delivered_at IS NULL AND abandoned_at IS NULL AND next_retry_at <= $1
            ORDER BY id
            LIMIT 50
            """,
            now,
        )
        return [_pending_send_desde_fila(r) for r in rows]

    async def claim_pending_sends(
        self, now: datetime, limit: int = 10, lease_seconds: int = 180
    ) -> list[PendingSend]:
        """071 (D6) — Los vencidos, RESERVADOS para este worker.

        Un `FOR UPDATE SKIP LOCKED` en un `SELECT` suelto no sirve aquí: el
        pool corre en autocommit y el candado muere con la sentencia, así que
        dos réplicas leerían las mismas filas. Se reservan empujando
        `next_retry_at` (un lease): la otra réplica ya no las ve vencidas, y si
        este proceso muere vuelven solas al vencer.

        Auditoría 071 (A-3): el lease cuenta desde el MISMO `now` con que se
        eligieron las filas (antes, `now()` de Postgres en el SET y el de
        Python en el WHERE), y el lote es chico para caber en él.
        """
        rows = await self.pool.fetch(
            """
            UPDATE pending_send SET next_retry_at = $1 + make_interval(secs => $3)
            WHERE id IN (
              SELECT id FROM pending_send
              WHERE delivered_at IS NULL AND abandoned_at IS NULL AND next_retry_at <= $1
              ORDER BY id
              FOR UPDATE SKIP LOCKED
              LIMIT $2)
            RETURNING *
            """,
            now,
            limit,
            float(lease_seconds),
        )
        return sorted((_pending_send_desde_fila(r) for r in rows), key=lambda p: p.id)

    async def purgar(
        self, dias: int, dias_envios: int, lote: int = 1000
    ) -> dict[str, int] | None:
        """071 (D7) — Borra lo viejo en lotes pequeños. None = otra réplica purga.

        Cada lote es su propia transacción y toma un candado de transacción:
        dos réplicas no purgan a la vez, y el despacho nunca espera más que lo
        que tarda un lote de `lote` filas.
        """
        consultas = {
            "dispatch_inbox": """DELETE FROM dispatch_inbox WHERE ctid IN (
                SELECT ctid FROM dispatch_inbox
                WHERE state = 'done' AND updated_at < now() - make_interval(days => $1)
                LIMIT $2)""",
            "processed_message": """DELETE FROM processed_message WHERE ctid IN (
                SELECT ctid FROM processed_message
                WHERE processed_at < now() - make_interval(days => $1)
                LIMIT $2)""",
            "pending_send": """DELETE FROM pending_send WHERE ctid IN (
                SELECT ctid FROM pending_send
                WHERE (delivered_at IS NOT NULL OR abandoned_at IS NOT NULL)
                  AND created_at < now() - make_interval(days => $1)
                LIMIT $2)""",
        }
        borradas = {tabla: 0 for tabla in consultas}
        for tabla, sql in consultas.items():
            antiguedad = dias_envios if tabla == "pending_send" else dias
            while True:
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        if not await conn.fetchval(
                            "SELECT pg_try_advisory_xact_lock(hashtextextended('nea:purga', 0))"
                        ):
                            return None
                        resultado = await conn.execute(sql, antiguedad, lote)
                n = int(resultado.split()[-1])
                borradas[tabla] += n
                if n < lote:
                    break
        return borradas

    async def mark_pending_send_delivered(self, pending_id: int) -> None:
        await self.pool.execute(
            "UPDATE pending_send SET delivered_at = now() WHERE id = $1", pending_id
        )

    async def mark_pending_send_abandoned(self, pending_id: int) -> None:
        await self.pool.execute(
            "UPDATE pending_send SET abandoned_at = now() WHERE id = $1", pending_id
        )

    async def reschedule_pending_send(
        self, pending_id: int, attempts: int, next_retry_at: datetime
    ) -> None:
        await self.pool.execute(
            "UPDATE pending_send SET attempts = $2, next_retry_at = $3 WHERE id = $1",
            pending_id,
            attempts,
            next_retry_at,
        )

    # -------------------------------------------------------- seguimiento ---

    async def due_followups(self, now: datetime) -> list[Conversation]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM bot_conversation
            WHERE followup_due_at IS NOT NULL
              AND followup_due_at <= $1
              AND followup_sent = FALSE
              AND phase <> 'cerrada'
            ORDER BY followup_due_at
            LIMIT 20
            """,
            now,
        )
        return [_conv_from_row(r) for r in rows]

    async def claim_followup(self, conversation_id: int) -> bool:
        # Se marca ANTES de enviar: a lo sumo un empujón, incluso con crash.
        row = await self.pool.fetchrow(
            """
            UPDATE bot_conversation SET followup_sent = TRUE, updated_at = now()
            WHERE id = $1 AND followup_sent = FALSE
            RETURNING id
            """,
            conversation_id,
        )
        return row is not None

    # --------------------------------------------------------------- misc ---

    async def ping(self) -> None:
        await self.pool.fetchval("SELECT 1")

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
