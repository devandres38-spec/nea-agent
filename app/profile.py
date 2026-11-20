"""Perfil del negocio: la capa que convierte a Nea en EL agente de UN negocio.

El chasis conductual (app/prompt.py) es genérico; todo lo que identifica al
negocio — nombre del agente, tono, instrucciones, reglas de escalado y
conocimiento aprobado — vive en un `BusinessProfile` que se resuelve así:

1. `GET /api/bot/profile` del CRM (agent profile + knowledge base que el
   dueño edita en la UI). Se cachea con TTL para que los cambios en la UI
   lleguen sin reiniciar el bot.
2. Fallback: un brief local en markdown (`BRIEF_PATH`) — para correr el bot
   sin CRM con perfil, o en desarrollo.
3. Último recurso: un perfil mínimo (el agente se presenta y agenda, pero
   avisa en logs que no tiene conocimiento del negocio).

Un fallo del CRM jamás tumba un turno: se sirve el último perfil conocido y,
si nunca hubo uno, el fallback.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.crm import CrmError
from app.datos import recursos_saneados

logger = logging.getLogger("nea.profile")

PROFILE_TTL_SECONDS = 300.0

# Render canónico del CRM para un knowledge base sin entradas (parte del
# contrato de GET /api/bot/profile): cuenta como "sin conocimiento".
EMPTY_KB_SENTINEL = "(knowledge base vacío)"

# 071 (D15) — Las claves de `TRATOS` del CRM (`src/lib/agent-roles.ts`).
TRATOS = ("tu", "usted", "neutro")


@dataclass(frozen=True)
class BusinessProfile:
    """Todo lo que el negocio configura del agente. Solo texto plano: el
    chasis decide dónde va cada pieza dentro del system prompt."""

    cloud: bool = False
    config_version: int = 1
    agent_name: str = "Asistente"  # 071 (D13): marca blanca
    tone: str | None = None
    instructions: str | None = None
    escalation_rules: str | None = None
    greeting: str | None = None
    kb_text: str | None = None
    resources: list[dict[str, str]] = field(default_factory=list)
    # 034 — El rol y las herramientas encendidas (claves del catálogo del CRM:
    # ficha, agendar, reprogramar, salida, escalar). `None` = todas, como un
    # CRM que todavía no las manda.
    role: str = "calificador"
    tools: frozenset[str] | None = None
    # 036 — Con qué piensa el negocio cuando Nea piensa por el CRM (bloque
    # `llm` del perfil): la ruta del proxy y el modelo de Ajustes → IA. `None`
    # = el CRM no tiene IA configurada, o no lo dice (un CRM anterior, el
    # brief local): Nea usa `LLM_MODEL`.
    llm_path: str | None = None
    llm_model: str | None = None
    # 073-A (D2) — La que fija la plataforma para este negocio (`llm.temperature`);
    # sin ella, LLM_TEMPERATURE de la instancia.
    llm_temperature: float | None = None
    # 071 (D5) — ¿Este negocio agenda? Lo dice el CRM en el contexto
    # (`agent.agendaEnabled`, módulo agenda del negocio). `None` = no lo dice
    # (un CRM anterior, `single`): manda la sonda, como siempre.
    agenda_enabled: bool | None = None
    # 071 (D15) — Cómo trata a los clientes: `tu`, `usted` o `neutro`. Un
    # valor desconocido (o un CRM que no lo manda) es `tu`, como siempre.
    treatment: str = "tu"
    # 071 (D16) — ¿Cierra con amabilidad las conversaciones sin rumbo? Apagado,
    # tampoco calla los rellenos tras un cierre anterior. Ausente = sí.
    stall_enabled: bool = True

    def permite(self, clave: str) -> bool:
        """¿Está encendida esta herramienta? `escalar` siempre lo está: es la
        red de seguridad (quien pide una persona, la hostilidad)."""
        return self.tools is None or clave == "escalar" or clave in self.tools

    @property
    def has_knowledge(self) -> bool:
        """¿Hay base de conocimiento (KB) de verdad?

        2.13.1 — Antes bastaban las `instructions`: un negocio con tres líneas
        de instrucciones y el KB vacío pasaba por «con conocimiento», el prompt
        callaba la advertencia y el modelo improvisaba precios y horarios.
        """
        return bool((self.kb_text or "").strip())

    @property
    def has_instructions(self) -> bool:
        return bool((self.instructions or "").strip())


def profile_from_payload(payload: dict[str, Any], default_name: str) -> BusinessProfile:
    """Construye el perfil desde la respuesta del CRM, tolerante a ausencias."""
    prof = payload.get("profile") or {}
    resources_raw = payload.get("resources") or []
    kb_text = payload.get("kb") or None
    if isinstance(kb_text, str) and kb_text.strip() == EMPTY_KB_SENTINEL:
        kb_text = None
    # vocero-crm 074 (D8) — Enlaces oficiales: solo https, saneados (app/datos.py).
    resources = recursos_saneados(resources_raw)
    return BusinessProfile(
        cloud=prof.get("cloud") is True,
        config_version=int(prof.get("configVersion") or 1),
        agent_name=str(prof.get("name") or default_name),
        tone=prof.get("tone") or None,
        instructions=prof.get("instructions") or None,
        escalation_rules=prof.get("escalationRules") or None,
        greeting=prof.get("greeting") or None,
        kb_text=kb_text,
        resources=resources,
        role=str(prof.get("role") or "calificador"),
        tools=_herramientas(prof.get("tools")),
        agenda_enabled=(
            prof["agendaEnabled"] if isinstance(prof.get("agendaEnabled"), bool) else None
        ),
        treatment=prof["treatment"] if prof.get("treatment") in TRATOS else "tu",
        stall_enabled=prof["stallEnabled"] if isinstance(prof.get("stallEnabled"), bool) else True,
        **_ia(payload.get("llm")),
    )


def temperatura_valida(valor: Any) -> float | None:
    """073-A — Una `temperature` del CRM, si es un número entre 0 y 2."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return float(valor) if 0 <= valor <= 2 else None


def _ia(valor: Any) -> dict[str, Any]:
    """El bloque `llm` del perfil (036): `{path, model}` o `null`.

    La ruta solo se acepta si es una ruta (`/…`): se pega detrás de
    `CRM_BASE_URL`, y otra cosa cambiaría el host al que viaja la llave.
    """
    if not isinstance(valor, dict):
        return {"llm_path": None, "llm_model": None, "llm_temperature": None}
    path = valor.get("path")
    model = valor.get("model")
    return {
        "llm_path": path if isinstance(path, str) and path.startswith("/") else None,
        "llm_model": (model.strip() or None) if isinstance(model, str) else None,
        "llm_temperature": temperatura_valida(valor.get("temperature")),
    }


def _herramientas(valor: Any) -> frozenset[str] | None:
    """Las claves que manda el CRM; sin lista (un CRM viejo), todas."""
    if not isinstance(valor, list):
        return None
    return frozenset(str(v) for v in valor if isinstance(v, str))


def profile_from_brief(path: Path, default_name: str) -> BusinessProfile | None:
    """Brief local: un markdown libre que se inyecta entero como instrucciones."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("no pude leer el brief local %s: %s", path, exc)
        return None
    if not text:
        return None
    return BusinessProfile(agent_name=default_name, instructions=text)


async def resolve_profile(ctx: Any) -> BusinessProfile:
    """Perfil vigente desde el AppContext; sin provider (tests) = mínimo."""
    if getattr(ctx, "profile", None) is None:
        return BusinessProfile(agent_name=getattr(ctx.settings, "agent_name", "Asistente"))
    return await ctx.profile.get()


class ProfileProvider:
    """Resuelve el BusinessProfile vigente con cache TTL y degradación."""

    def __init__(
        self,
        crm: Any,
        *,
        default_name: str = "Asistente",
        brief_path: str | None = None,
        ttl: float = PROFILE_TTL_SECONDS,
    ) -> None:
        self._crm = crm
        self._default_name = default_name
        self._brief_path = Path(brief_path) if brief_path else None
        self._ttl = ttl
        self._cached: BusinessProfile | None = None
        self._fetched_at: float = 0.0
        self._warned_minimal = False

    async def get(self) -> BusinessProfile:
        now = time.monotonic()
        if self._cached is not None and not self._cached.cloud and (now - self._fetched_at) < self._ttl:
            return self._cached

        payload = None
        try:
            payload = await self._crm.get_profile()
        except CrmError as exc:
            logger.warning("perfil: el CRM no respondió (%s) — uso el último conocido", exc)
        except AttributeError:
            payload = None  # cliente sin get_profile (tests viejos): fallback

        if payload is not None:
            self._cached = profile_from_payload(payload, self._default_name)
            self._fetched_at = now
            return self._cached

        # CRM sin perfil (404) o caído: último conocido > brief local > mínimo.
        if self._cached is not None:
            self._fetched_at = now  # no martillar al CRM caído en cada turno
            return self._cached
        if self._brief_path is not None:
            brief = profile_from_brief(self._brief_path, self._default_name)
            if brief is not None:
                self._cached = brief
                self._fetched_at = now
                logger.info("perfil: usando brief local %s", self._brief_path)
                return brief
        if not self._warned_minimal:
            logger.warning(
                "perfil: sin agent profile en el CRM ni BRIEF_PATH legible — "
                "el agente corre con el perfil mínimo (configura uno de los dos)"
            )
            self._warned_minimal = True
        self._cached = BusinessProfile(agent_name=self._default_name)
        self._fetched_at = now
        return self._cached
