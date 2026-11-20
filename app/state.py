"""Estado propio del bot: modelos, contrato de almacenamiento y fake en memoria.

El acceso a datos está abstraído en el protocolo `Store` para que los unit
tests corran con `MemoryStore` (sin Postgres). La implementación real con
asyncpg vive en `app/db.py` (`PgStore`).
"""
from __future__ import annotations

import asyncio
import json
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol
from contextlib import AbstractAsyncContextManager
from app.dispatch_store import DispatchJob, MemoryDispatchStore

if TYPE_CHECKING:
    from app.config import Settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Lo único que `update_conversation` puede tocar. Vive aquí y no en db.py para
# que MemoryStore rechace exactamente lo mismo que Postgres: si aceptara
# cualquier campo, un nombre mal escrito pasaría todas las pruebas y reventaría
# el turno en producción — la misma distancia entre pruebas y base de verdad
# que escondió el fallo del relay.
COLUMNAS_DE_CONVERSACION = frozenset(
    {
        "crm_conversation_id",
        "phase",
        "greeted",
        "media_notice_sent",
        "followup_due_at",
        "followup_sent",
        "last_inbound_at",
        "stalled_at",
        "stall_since_message_id",
    }
)


# ---------------------------------------------------------------- modelos ---


@dataclass
class Conversation:
    id: int
    wa_identity: str
    crm_conversation_id: str | None = None
    # De quién es esta conversación. Cadena vacía = instalación de un solo
    # negocio, que es lo que era todo antes del modo multi-organización.
    #
    # Importa más de lo que parece: la identidad SOLA dejó de identificar una
    # conversación en cuanto una Nea atiende a varios negocios, porque la
    # misma persona puede escribirle a dos.
    organization_id: str = ""
    organization_slug: str = ""
    phase: str = "descubrimiento"  # descubrimiento|insight|salida|agendando|cerrada
    greeted: bool = False
    media_notice_sent: bool = False
    followup_due_at: datetime | None = None
    followup_sent: bool = False
    last_inbound_at: datetime | None = None
    # Puesta cuando el agente cierra por conversación sin rumbo: mientras
    # viva, el relleno se contesta con silencio (ver app/stall.py).
    stalled_at: datetime | None = None
    # El candado cuenta solo los mensajes con id mayor a este: se mueve al
    # reabrir, para que el hilo viejo no vuelva a disparar el cierre.
    stall_since_message_id: int = 0


@dataclass
class BotMessage:
    id: int
    conversation_id: int
    role: str  # user | assistant
    content: str
    wa_message_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class OfferedSlot:
    conversation_id: int
    start_utc: datetime
    end_utc: datetime | None
    label: str
    offered_at: datetime = field(default_factory=utcnow)


# 073-A (D2) — Qué lleva una fila de `pending_send`: la respuesta que no salió
# (`content` = el texto) o un handoff que no se pudo registrar (`content` = el
# motivo). Ver migrations/010_reanudacion.sql.
KIND_MESSAGE = "message"
KIND_HANDOFF = "handoff"


def contenido_de_handoff(reason: str, summary: str | None = None) -> str:
    """Auditoría 073-B — Lo que guarda `pending_send.content` de un handoff.

    Sin resumen, el motivo a secas (lo de siempre: las filas viejas se leen
    igual). Con resumen, un JSON `{"reason", "summary"}` para no perderlo
    si el handoff cae a la cola.
    """
    if not summary:
        return reason
    return json.dumps({"reason": reason, "summary": summary[:500]}, ensure_ascii=False)


def leer_handoff(content: str | None) -> tuple[str, str | None]:
    """El inverso de `contenido_de_handoff`; texto plano = solo el motivo."""
    texto = (content or "").strip()
    if texto.startswith("{"):
        try:
            datos = json.loads(texto)
        except ValueError:
            datos = None
        if isinstance(datos, dict) and isinstance(datos.get("reason"), str):
            resumen = datos.get("summary")
            return datos["reason"] or "error", resumen if isinstance(resumen, str) and resumen else None
    return texto or "error", None


@dataclass
class PendingSend:
    """Respuesta generada cuyo envío al CRM agotó los reintentos del turno.
    Se persiste para reintento diferido — jamás se descarta (incidente 2026-08-03)."""

    id: int
    conversation_id: int
    crm_conversation_id: str
    content: str
    attempts: int
    created_at: datetime
    next_retry_at: datetime
    delivered_at: datetime | None = None
    abandoned_at: datetime | None = None
    # Para que el reintento diferido sepa con qué credencial hablarle al CRM
    # cuando despierte. Van al final por la regla de los dataclass: un campo
    # con valor por defecto no puede preceder a uno sin él.
    organization_id: str = ""
    organization_slug: str = ""
    # Y qué despacho estaba contestando: el CRM lo exige al responder por el
    # cerebro, y cuando el worker despierta el turno ya no existe para dárselo.
    dispatch_id: str = ""
    # 073-A (D2) — `message` o `handoff` (KIND_*).
    kind: str = KIND_MESSAGE


@dataclass
class InboundMessage:
    """Un mensaje del cliente, como lo trae el despacho del CRM."""

    wa_message_id: str | None
    identity: str
    type: str
    text: str | None = None
    referral_headline: str | None = None
    profile_name: str | None = None
    # Multimedia (spec 002): lo mínimo para procesar el contenido.
    media_id: str | None = None
    media_mime: str | None = None
    media_filename: str | None = None
    media_caption: str | None = None
    media_voice: bool = False
    location: dict[str, Any] | None = None
    contact_names: list[str] = field(default_factory=list)
    # vocero-crm 073-C (D5): los teléfonos de las tarjetas (vCard), en el
    # orden de los nombres; y el mensaje que el lead citó al responder.
    contact_phones: list[str] = field(default_factory=list)
    reply_to: dict[str, Any] | None = None
    # 031 — Por dónde escribe: whatsapp, instagram, messenger, tiktok… Lo
    # dice el despacho (`conversation.channel`); None si no lo dijo.
    channel: str | None = None
    # 033 — Una conversación de prueba del Laboratorio (`conversation.test` del
    # despacho). La respuesta la guarda el CRM sin enviarla; aquí no pasa por
    # ALLOWED_WA_IDS (la identidad es sintética) ni programa seguimiento.
    prueba: bool = False


# --------------------------------------------------------------- contrato ---


class Store(Protocol):
    """Contrato de persistencia del bot (Postgres real o memoria en tests)."""

    async def enqueue_dispatch(self, org: str, payload: dict[str, Any]) -> None: ...
    def claim_dispatch(self) -> AbstractAsyncContextManager[DispatchJob | None]: ...

    # dedup
    async def mark_processed(self, wa_message_id: str) -> bool:
        """True si el mensaje es nuevo (gana el INSERT); False si ya se procesó."""
        ...

    async def already_processed(self, marcas: list[str]) -> set[str]:
        """Cuáles de estas marcas ya existen. Solo consulta: no marca nada."""
        ...

    # conversaciones
    async def get_or_create_conversation(
        self,
        wa_identity: str,
        organization_id: str = "",
        organization_slug: str = "",
    ) -> Conversation: ...
    async def update_conversation(self, conversation_id: int, **fields: Any) -> None: ...
    async def reset_conversation(self, conversation_id: int) -> None:
        """Borra historial + slots y regresa la conversación a estado inicial
        (comando /reset de la línea de pruebas)."""
        ...
    async def forget_conversation(
        self, organization_id: str, wa_identity: str, crm_conversation_id: str
    ) -> bool:
        """Borra TODO lo que Nea guarda de esa conversación: la fila, su
        historial, sus horarios ofrecidos y sus envíos pendientes.

        Solo si la fila sigue siendo de `crm_conversation_id` (o todavía no
        sabe de cuál es). El aviso de borrado puede llegar tarde: si la persona
        ya volvió a escribir, la fila es de la conversación NUEVA y borrarla
        le quitaría la memoria a la que no se eliminó. True si borró algo."""
        ...

    # historial LLM
    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        wa_message_id: str | None = None,
    ) -> None: ...
    async def recent_messages(
        self, conversation_id: int, limit: int
    ) -> list[BotMessage]: ...

    # slots ofrecidos
    async def replace_offered_slots(
        self, conversation_id: int, slots: list[OfferedSlot]
    ) -> None: ...
    async def get_offered_slots(self, conversation_id: int) -> list[OfferedSlot]: ...
    async def clear_offered_slots(self, conversation_id: int) -> None: ...

    # cola de envíos pendientes (respuestas que no pudieron salir en el turno)
    async def enqueue_pending_send(
        self,
        conversation_id: int,
        crm_conversation_id: str,
        content: str,
        organization_id: str = "",
        organization_slug: str = "",
        dispatch_id: str = "",
        *,
        kind: str = KIND_MESSAGE,
    ) -> int: ...
    async def due_pending_sends(self, now: datetime) -> list[PendingSend]: ...
    async def claim_pending_sends(
        self, now: datetime, limit: int = 10, lease_seconds: int = 180
    ) -> list[PendingSend]:
        """Los vencidos, reservados (lease) para quien los reclama (071, D6)."""
        ...
    async def purgar(
        self, dias: int, dias_envios: int, lote: int = 1000
    ) -> dict[str, int] | None:
        """Retención (071, D7). None = otra réplica está purgando."""
        ...
    async def mark_pending_send_delivered(self, pending_id: int) -> None: ...
    async def mark_pending_send_abandoned(self, pending_id: int) -> None: ...
    async def reschedule_pending_send(
        self, pending_id: int, attempts: int, next_retry_at: datetime
    ) -> None: ...

    # seguimiento
    async def due_followups(self, now: datetime) -> list[Conversation]: ...
    async def claim_followup(self, conversation_id: int) -> bool:
        """Marca followup_sent=True atómicamente. True si ESTA llamada lo ganó."""
        ...

    async def ping(self) -> None: ...
    async def aclose(self) -> None: ...


# ------------------------------------------------------- fake en memoria ---


class MemoryStore(MemoryDispatchStore):
    """Implementación en memoria del Store — solo para tests."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.processed: set[str] = set()
        self.conversations: dict[int, Conversation] = {}
        self._conv_by_identity: dict[tuple[str, str], int] = {}
        self.messages: list[BotMessage] = []
        self.offered: dict[int, list[OfferedSlot]] = {}
        self.pending_sends: dict[int, PendingSend] = {}

    async def mark_processed(self, wa_message_id: str) -> bool:
        if wa_message_id in self.processed:
            return False
        self.processed.add(wa_message_id)
        return True

    async def already_processed(self, marcas: list[str]) -> set[str]:
        return {m for m in marcas if m in self.processed}

    async def get_or_create_conversation(
        self,
        wa_identity: str,
        organization_id: str = "",
        organization_slug: str = "",
    ) -> Conversation:
        clave = (organization_id, wa_identity)
        cid = self._conv_by_identity.get(clave)
        if cid is not None:
            conv = self.conversations[cid]
            # Igual que el ON CONFLICT de PgStore: el slug se refresca, porque
            # un miembro puede renombrar su subdominio y el id no cambia.
            conv.organization_slug = organization_slug
            return conv
        cid = next(self._ids)
        conv = Conversation(
            id=cid,
            wa_identity=wa_identity,
            organization_id=organization_id,
            organization_slug=organization_slug,
        )
        self.conversations[cid] = conv
        self._conv_by_identity[clave] = cid
        return conv

    async def update_conversation(self, conversation_id: int, **fields: Any) -> None:
        desconocidas = set(fields) - COLUMNAS_DE_CONVERSACION
        if desconocidas:
            raise ValueError(
                f"columnas desconocidas en update_conversation: {desconocidas}"
            )
        conv = self.conversations[conversation_id]
        for key, value in fields.items():
            setattr(conv, key, value)

    async def reset_conversation(self, conversation_id: int) -> None:
        self.messages = [
            m for m in self.messages if m.conversation_id != conversation_id
        ]
        self.offered.pop(conversation_id, None)
        conv = self.conversations[conversation_id]
        conv.phase = "descubrimiento"
        conv.greeted = False
        conv.media_notice_sent = False
        conv.followup_due_at = None
        conv.followup_sent = False
        conv.stalled_at = None

    async def forget_conversation(
        self, organization_id: str, wa_identity: str, crm_conversation_id: str
    ) -> bool:
        clave = (organization_id, wa_identity)
        cid = self._conv_by_identity.get(clave)
        if cid is None:
            return False
        actual = self.conversations[cid].crm_conversation_id
        if actual is not None and actual != crm_conversation_id:
            return False
        del self._conv_by_identity[clave]
        del self.conversations[cid]
        self.messages = [m for m in self.messages if m.conversation_id != cid]
        self.offered.pop(cid, None)
        self.pending_sends = {
            pid: p for pid, p in self.pending_sends.items()
            if p.conversation_id != cid
        }
        return True

    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        wa_message_id: str | None = None,
    ) -> None:
        self.messages.append(
            BotMessage(
                id=next(self._ids),
                conversation_id=conversation_id,
                role=role,
                content=content,
                wa_message_id=wa_message_id,
            )
        )

    async def recent_messages(
        self, conversation_id: int, limit: int
    ) -> list[BotMessage]:
        msgs = [m for m in self.messages if m.conversation_id == conversation_id]
        return msgs[-limit:]

    async def replace_offered_slots(
        self, conversation_id: int, slots: list[OfferedSlot]
    ) -> None:
        self.offered[conversation_id] = list(slots)

    async def get_offered_slots(self, conversation_id: int) -> list[OfferedSlot]:
        return list(self.offered.get(conversation_id, []))

    async def clear_offered_slots(self, conversation_id: int) -> None:
        self.offered.pop(conversation_id, None)

    async def enqueue_pending_send(
        self,
        conversation_id: int,
        crm_conversation_id: str,
        content: str,
        organization_id: str = "",
        organization_slug: str = "",
        dispatch_id: str = "",
        *,
        kind: str = KIND_MESSAGE,
    ) -> int:
        pid = next(self._ids)
        now = utcnow()
        self.pending_sends[pid] = PendingSend(
            id=pid, conversation_id=conversation_id,
            crm_conversation_id=crm_conversation_id, content=content,
            attempts=0, created_at=now, next_retry_at=now,
            organization_id=organization_id,
            organization_slug=organization_slug,
            dispatch_id=dispatch_id,
            kind=kind,
        )
        return pid

    async def due_pending_sends(self, now: datetime) -> list[PendingSend]:
        return [
            p for p in sorted(self.pending_sends.values(), key=lambda p: p.id)
            if p.delivered_at is None and p.abandoned_at is None
            and p.next_retry_at <= now
        ]

    async def claim_pending_sends(
        self, now: datetime, limit: int = 10, lease_seconds: int = 180
    ) -> list[PendingSend]:
        vencidos = (await self.due_pending_sends(now))[:limit]
        for p in vencidos:
            p.next_retry_at = now + timedelta(seconds=lease_seconds)
        return vencidos

    async def purgar(
        self, dias: int, dias_envios: int, lote: int = 1000
    ) -> dict[str, int] | None:
        limite = utcnow() - timedelta(days=dias)
        limite_envios = utcnow() - timedelta(days=dias_envios)
        inbox = self._inbox()
        viejas = [k for k, r in inbox.items()
                  if r["state"] == "done" and r.get("done_at", utcnow()) < limite]
        for k in viejas:
            del inbox[k]
        cerrados = [pid for pid, p in self.pending_sends.items()
                    if (p.delivered_at or p.abandoned_at) and p.created_at < limite_envios]
        for pid in cerrados:
            del self.pending_sends[pid]
        # `processed` es un set sin fecha en memoria: nada que purgar aquí.
        return {"dispatch_inbox": len(viejas), "processed_message": 0, "pending_send": len(cerrados)}

    async def mark_pending_send_delivered(self, pending_id: int) -> None:
        self.pending_sends[pending_id].delivered_at = utcnow()

    async def mark_pending_send_abandoned(self, pending_id: int) -> None:
        self.pending_sends[pending_id].abandoned_at = utcnow()

    async def reschedule_pending_send(
        self, pending_id: int, attempts: int, next_retry_at: datetime
    ) -> None:
        item = self.pending_sends[pending_id]
        item.attempts = attempts
        item.next_retry_at = next_retry_at

    async def due_followups(self, now: datetime) -> list[Conversation]:
        return [
            c for c in self.conversations.values()
            if c.followup_due_at is not None
            and c.followup_due_at <= now
            and not c.followup_sent
            and c.phase != "cerrada"
        ]

    async def claim_followup(self, conversation_id: int) -> bool:
        conv = self.conversations[conversation_id]
        if conv.followup_sent:
            return False
        conv.followup_sent = True
        return True

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------- contexto ---


@dataclass
class AppContext:
    """Dependencias vivas de la app — inyectables en tests."""

    settings: "Settings"
    store: Store
    crm: Any  # CrmClient
    llm: Any  # OpenAiLlm o fake con .complete()
    profile: Any | None = None  # ProfileProvider; None en tests = perfil mínimo
    # Modo multi-organización: el registro de clientes por organización.
    # None fuera de ese modo — una Nea de un solo negocio no lo necesita, y
    # que sea None es lo que hace evidente en qué modo está corriendo.
    registro: Any | None = None  # RegistroDeOrganizaciones
    # (organization_id, slug) del turno en curso. La pone el despacho al armar
    # el contexto del turno; None fuera del modo multi-organización.
    organizacion: tuple[str, str] | None = None
    dispatch_wake: asyncio.Event = field(default_factory=asyncio.Event)
    # 073-A (D3) — Puesto al apagar (SIGTERM): los consumidores del despacho
    # dejan de reclamar y los turnos en curso terminan dentro del margen.
    apagando: asyncio.Event = field(default_factory=asyncio.Event)
    # 031 — Cuándo llegó el último webhook de Meta a Nea, que desde la 2.0 ya
    # no lo recibe (app/webhook_retirado.py). Lo enseña /health.
    webhook_meta_ultimo: datetime | None = None
    # ¿El CRM de esta instancia tiene motor de agenda? Vocero lo trae detrás de
    # una bandera de despliegue y viene apagado por defecto. Es lo que vale en
    # el turno en curso: el turno lo refresca al empezar desde `agenda_sonda`
    # (y una herramienta lo apaga si choca con el 404 de la bandera), para no
    # ofrecerle horarios a un lead contra un CRM que no puede agendarlos.
    agenda_enabled: bool = True
    # La respuesta del CRM con caducidad (app/agenda.py): encender o apagar
    # AGENDA allá llega sin reiniciar Nea. None = sin sonda (pruebas que fijan
    # `agenda_enabled` a mano).
    agenda_sonda: Any | None = None
    # Auditoría 071 (B-3): cupos de ensayo de esta app (`app/ensayo.py`).
    ensayo_cupos: asyncio.Semaphore | None = None
    # Un candado por identidad: los turnos de UNA conversación se serializan.
    # Sin esto, una ráfaga que llega mientras el turno anterior sigue en vuelo
    # abre un segundo turno con contexto viejo (se reservó una cita antes de
    # leer el mensaje que la corregía, y salieron dos respuestas encimadas).
    # Clave (organization_id, identidad) desde la 071.
    turn_locks: dict[tuple[str, str], asyncio.Lock] = field(default_factory=dict)
    # Cuántos turnos tienen tomado (o esperan) el candado de esa clave.
    turn_lock_users: dict[tuple[str, str], int] = field(default_factory=dict)
