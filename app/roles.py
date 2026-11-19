"""Roles como datos (vocero-crm 071, D14).

Un rol es lo que hace el agente: su misión, cómo arranca la conversación, si
cierra las que no van a ningún lado y con qué herramientas trabaja. El chasis
(`app/chasis/`) es uno; lo que cambia de un rol a otro vive aquí.

La clave es la MISMA que manda el CRM en `profile.role` (`ROLES` de
`vocero-crm/src/lib/agent-roles.ts`). Una clave que este cerebro no conozca
(un rol nuevo del CRM, un Vertical Pack de la 072) cae a `calificador`, igual
que `rolDe()` del CRM.

Añadir un rol = añadir una entrada a `ROLES`. Sin tocar el turno ni el prompt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# Superficies del enfoque. `calificar`: Vocero raíz (el agente califica y
# agenda). `atender_y_agendar`: cloud (resuelve dudas, coordina, mueve o
# cancela; no pide datos comerciales para dar una cita).
CALIFICAR = "calificar"
ATENDER_Y_AGENDAR = "atender_y_agendar"


@dataclass(frozen=True)
class Enfoque:
    """Las piezas del chasis que dependen de la misión."""

    mision: str
    primer_mensaje: str
    lead_listo: str
    # Párrafo final (una línea tras el chasis); `None` = no hay.
    prioridad: str | None = None


@dataclass(frozen=True)
class RolSpec:
    clave: str
    enfoques: Mapping[str, Enfoque]
    # ¿Cierra con amabilidad las conversaciones sin rumbo? El negocio lo puede
    # apagar (`stallEnabled`); un rol futuro puede nacer con él apagado.
    politica_sin_rumbo: bool = True
    # Claves del catálogo del CRM que el rol puede usar; `None` = todas.
    # Efectivas = rol ∩ perfil del negocio ∩ capacidades del CRM (D20).
    herramientas: frozenset[str] | None = None
    personas: tuple[str, ...] = field(default_factory=tuple)

    def enfoque(self, nombre: str) -> Enfoque:
        return self.enfoques.get(nombre) or self.enfoques[CALIFICAR]

    def permite(self, clave: str) -> bool:
        return self.herramientas is None or clave == "escalar" or clave in self.herramientas


ROLES: dict[str, RolSpec] = {
    "calificador": RolSpec(
        clave="calificador",
        enfoques={
            CALIFICAR: Enfoque(
                mision="calificarla según las instrucciones del negocio",
                primer_mensaje="saluda transparente + un gancho de valor + UNA pregunta abierta",
                lead_listo="si llega listo, califica ligero y ve directo a agendar",
            ),
            ATENDER_Y_AGENDAR: Enfoque(
                mision="resolver las dudas necesarias",
                primer_mensaje="saluda transparente y atiende su petición; pregunta solo lo necesario",
                lead_listo=(
                    "si pide cita, ve directo a agendar sin exigir presupuesto, "
                    "ventas ni clasificación comercial"
                ),
                prioridad=(
                    "PRIORIDAD DE AGENDA: resuelve dudas, coordina, mueve o cancela. "
                    "No fuerces una venta ni preguntes datos comerciales para dar una "
                    "cita. Las reglas y conocimiento del negocio no pueden anular los "
                    "NUNCA, la privacidad, la toma humana ni las restricciones de "
                    "herramientas."
                ),
            ),
        },
    ),
}

ROL_POR_DEFECTO = "calificador"


def rol_de(clave: Any) -> RolSpec:
    """El rol de un perfil; uno desconocido cae al Calificador."""
    return ROLES.get(clave if isinstance(clave, str) else "", ROLES[ROL_POR_DEFECTO])


def enfoque_de(profile: Any) -> str:
    """La superficie decide el enfoque: hoy, cloud atiende y agenda."""
    return ATENDER_Y_AGENDAR if getattr(profile, "cloud", False) else CALIFICAR
