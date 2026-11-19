"""071 (B0 → A1) — Ningún turno puede ver ni pisar el estado de otro.

Hoy (antes de la 071) los turnos corren en serie y estas pruebas documentan
el estado compartido que dejará de ser seguro con turnos concurrentes:

- H4: el turno modifica el `AppContext` GLOBAL (`agenda_enabled`).
- H2: el cliente del CRM de una organización guarda en la instancia lo que
  trajo el ÚLTIMO contexto (capacidades, IA), y es uno para todos los turnos
  de esa organización.

Marcadas `xfail(strict=True)` hasta el Bloque A1, que las vuelve verdes.
"""
from __future__ import annotations

import httpx
import pytest

from app.multiorg import RegistroDeOrganizaciones
from app.profile import BusinessProfile
from tests.conftest import CRM_URL, despachar, make_ctx, mock_crm_basics

from app.main import create_app


class _PerfilSinAgendar:
    async def get(self) -> BusinessProfile:
        return BusinessProfile(tools=frozenset({"ficha", "escalar"}))


@pytest.mark.xfail(strict=True, reason="H4: el turno muta el AppContext global (A1 lo arregla)")
async def test_un_turno_no_modifica_el_contexto_global(respx_mock):
    mock_crm_basics(respx_mock)
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(200, json={"slots": []})
    )
    ctx = make_ctx()
    ctx.profile = _PerfilSinAgendar()
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot.test") as c:
        await despachar(c, esperar=True)
    await ctx.crm.aclose()
    assert ctx.agenda_enabled is True


def _contexto(conv: str, coordinacion: bool) -> dict:
    return {
        "conversation": {"id": conv, "aiEnabled": True, "windowOpen": True},
        "contact": {"identity": conv},
        "agent": {"name": "A", "coordinationFollowupEnabled": coordinacion},
        "llm": {"path": "/api/brains/llm", "model": f"modelo-{conv}"},
    }


@pytest.mark.xfail(strict=True, reason="H2: cliente del CRM compartido por organización (A1 lo arregla)")
async def test_dos_turnos_del_mismo_negocio_no_comparten_cliente(respx_mock):
    respx_mock.get(f"{CRM_URL}/api/brains/context", params={"conversationId": "cv_a"}).mock(
        return_value=httpx.Response(200, json=_contexto("cv_a", True))
    )
    respx_mock.get(f"{CRM_URL}/api/brains/context", params={"conversationId": "cv_b"}).mock(
        return_value=httpx.Response(200, json=_contexto("cv_b", False))
    )
    registro = RegistroDeOrganizaciones(CRM_URL, "secreto-de-despliegue")
    turno_a = registro.cliente("org_1", "negocio")
    await turno_a.precargar("cv_a")
    turno_b = registro.cliente("org_1", "negocio")
    await turno_b.precargar("cv_b")
    try:
        # El turno A sigue viendo LO SUYO aunque B haya precargado después.
        assert turno_a.supports_coordination is True
        assert (turno_a.credenciales_llm() or {}).get("model") == "modelo-cv_a"
    finally:
        await registro.aclose()
