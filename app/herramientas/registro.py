"""El registro de herramientas (vocero-crm 071, D20): el punto de enchufe.

Una herramienta es un `ToolSpec`: su nombre para el modelo, la clave del
catálogo del CRM que la enciende o apaga (`profile.tools`), su esquema, cómo
se ejecuta y cuándo existe. El turno no sabe de nombres: pide los esquemas
que tocan y despacha por registro.

La 072 enchufará aquí un proveedor de herramientas dinámicas (`tools[]` del
contexto, ejecutadas por `POST /api/brains/tools/{name}`) sin tocar el turno,
el prompt ni `/context`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable


@dataclass(frozen=True)
class Capacidades:
    """Lo que el CRM de ESTE turno sabe hacer."""

    agenda: bool = True
    agenda_v2: bool = False
    coordinacion: bool = False
    contexto: dict[str, Any] | None = None
    # 038 — A qué etapas se puede mover al lead (`lead.nextStages`).
    etapas: tuple[str, ...] = ()


Esquema = Callable[[Capacidades], dict[str, Any]]
Ejecutar = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]


def _siempre(_: Capacidades) -> bool:
    return True


@dataclass(frozen=True)
class ToolSpec:
    nombre: str
    # Clave del catálogo del CRM (`ficha`, `agendar`, …). `escalar` no se apaga.
    clave: str
    esquema: Esquema
    ejecutar: Ejecutar
    # ¿Se le ofrece al modelo en este turno?
    ofrecer: Callable[[Capacidades], bool] = _siempre
    # ¿Se ejecuta si el modelo la pide? (una que el CRM no soporta, no)
    ejecutable: Callable[[Capacidades], bool] = _siempre


class Registro:
    """Herramientas en orden: el orden es el del catálogo que ve el modelo."""

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.registrar(spec)

    def registrar(self, spec: ToolSpec) -> None:
        if spec.nombre in self._specs:
            raise ValueError(f"herramienta duplicada: {spec.nombre}")
        self._specs[spec.nombre] = spec

    def get(self, nombre: str) -> ToolSpec | None:
        return self._specs.get(nombre)

    def __iter__(self):
        return iter(self._specs.values())

    def nombres(self) -> list[str]:
        return list(self._specs)

    def esquemas(self, caps: Capacidades) -> list[dict[str, Any]]:
        return [spec.esquema(caps) for spec in self if spec.ofrecer(caps)]

    def clave_de(self, nombre: str) -> str:
        """Clave del catálogo; una herramienta que no está aquí no se apaga."""
        spec = self.get(nombre)
        return spec.clave if spec else "escalar"
