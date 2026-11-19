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
# 071 (D15) — Los ejemplos del guion van de «tú»: con otro trato se le dice
# al modelo que los adapte, en vez de reescribir cada ejemplo.
VOZ_USTED = (
    '- Español neutro de negocios, de "usted", cordial: frases cortas, cero '
    "corporativo. Si el perfil del negocio define un tono, ese tono manda. Los "
    'ejemplos de este guion van de "tú": dilos de "usted".'
)
VOZ_NEUTRO = (
    "- Español neutro de negocios, sin tutear ni ustedear: frases impersonales "
    'y cortas ("¿qué día queda mejor?"), cero corporativo. Si el perfil del '
    "negocio define un tono, ese tono manda. Los ejemplos de este guion van de "
    '"tú": dilos sin "tú" ni "usted".'
)
VOCES = {"tu": VOZ_TU, "usted": VOZ_USTED, "neutro": VOZ_NEUTRO}
# 071 (D15) — Para todos los negocios y tratos.
LINEA_IDIOMA = "- Si la persona escribe en otro idioma, contéstale en ese idioma."


def voz_del_trato(profile: Any) -> str:
    """La línea del trato (desconocido → tú) más la regla de idioma."""
    voz = VOCES.get(getattr(profile, "treatment", "tu"), VOZ_TU)
    return voz + "\n" + LINEA_IDIOMA


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
