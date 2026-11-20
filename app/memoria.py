"""vocero-crm 073-C (D6, P-7) — Qué parte del historial ve el modelo.

Antes: los últimos 10 mensajes, sin noción del tiempo. Un lead que volvía a
los 3 días con «¿y entonces?» recibía una respuesta como si nunca se hubiera
ido, y con 10 mensajes el modelo olvidaba lo que el lead dijo al principio de
la misma conversación.

Ahora: hasta `HISTORY_WINDOW` (20) mensajes, recortados por un presupuesto de
caracteres (≈ tokens × 4) de lo más viejo hacia atrás, y un separador
«— N días después —» SOLO donde entre dos mensajes hay más de 6 horas. Los
separadores van como mensajes `system` cortos ENTRE los del historial: el
prefijo del prompt (chasis + negocio) no cambia y sigue siendo cacheable.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Sequence

from app.state import BotMessage

HUECO_MINIMO = timedelta(hours=6)
# ~3000 tokens de historial; un mensaje suelto enorme (un PDF pegado) no se
# come la ventana entera.
PRESUPUESTO_CARACTERES = 12_000


def ventana(historial: Sequence[BotMessage], maximo: int, presupuesto: int = PRESUPUESTO_CARACTERES) -> list[BotMessage]:
    """Los últimos `maximo` mensajes que caben en el presupuesto (siempre el último)."""
    elegidos: list[BotMessage] = []
    usado = 0
    for m in reversed(list(historial)[-maximo:] if maximo > 0 else []):
        largo = len(m.content or "")
        if elegidos and usado + largo > presupuesto:
            break
        elegidos.append(m)
        usado += largo
    return list(reversed(elegidos))


def separador(hueco: timedelta) -> str:
    dias = hueco.days
    if dias >= 2:
        return f"— {dias} días después —"
    if dias == 1:
        return "— 1 día después —"
    return f"— {int(hueco.total_seconds() // 3600)} horas después —"


def con_separadores(historial: Sequence[BotMessage]) -> list[dict[str, Any]]:
    """Los mensajes para el modelo, con un separador donde hubo un hueco largo."""
    out: list[dict[str, Any]] = []
    anterior = None
    for m in historial:
        if anterior is not None and m.created_at and anterior.created_at:
            hueco = m.created_at - anterior.created_at
            if hueco > HUECO_MINIMO:
                out.append({"role": "system", "content": separador(hueco)})
        out.append({"role": m.role, "content": m.content})
        anterior = m
    return out
