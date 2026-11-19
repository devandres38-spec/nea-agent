"""Arma el chasis: secciones + rol + capacidades del turno (071, D14)."""
from __future__ import annotations

from typing import Any

from app.chasis import secciones as s
from app.roles import ATENDER_Y_AGENDAR, enfoque_de, rol_de

CANCELAR_CON_EL_EQUIPO = "→ Si quiere CANCELAR: handoff — esa la decide el equipo."
CANCELAR_CON_HERRAMIENTAS = (
    "→ Si quiere CANCELAR: usa list_bookings y cancel_session cuando estén "
    "disponibles; confirma la cita elegida y nunca inventes que se canceló."
)

VOZ_TU = (
    '- Español neutro de negocios, de "tú", frases cortas, cero corporativo. '
    "Si el perfil del negocio define un tono, ese tono manda."
)


def voz_del_trato(profile: Any) -> str:
    return VOZ_TU


def armar_chasis(profile: Any, medio: str, a_quien: str, *, agenda_v2: bool = False) -> str:
    """El texto del chasis para este negocio, canal y CRM.

    La línea de cancelar depende de que el CRM sepa cancelar (agenda v2) o del
    enfoque cloud, que siempre la tuvo: la herramienta solo existe con la
    agenda v2 y la línea dice «cuando estén disponibles».
    """
    nombre_enfoque = enfoque_de(profile)
    enfoque = rol_de(getattr(profile, "role", None)).enfoque(nombre_enfoque)
    name = profile.agent_name
    cancelar = (
        CANCELAR_CON_HERRAMIENTAS
        if agenda_v2 or nombre_enfoque == ATENDER_Y_AGENDAR
        else CANCELAR_CON_EL_EQUIPO
    )
    texto = "\n\n".join(
        [
            s.identidad(name, medio, a_quien, enfoque),
            s.voz(medio, voz_del_trato(profile)),
            s.conversacion(enfoque),
            s.agendar(cancelar),
            s.salida(),
            s.handoff(),
            s.blindaje(name, medio),
            s.herramientas(),
            s.nunca(name),
            s.multimedia(),
        ]
    )
    if enfoque.prioridad:
        texto += "\n" + enfoque.prioridad
    return texto
