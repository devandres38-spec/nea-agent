"""071 (B0 → A1) — Ningún turno puede ver ni pisar el estado de otro.

Antes de la 071 los turnos corrían en serie y había estado compartido que
dejaba de ser seguro con turnos concurrentes:

- H4: el turno modificaba el `AppContext` GLOBAL (`agenda_enabled`).
- H2: el cliente del CRM de una organización guardaba en la instancia lo que
  trajo el ÚLTIMO contexto (capacidades, IA), y era uno para todos los turnos
  de esa organización.

Nacieron como `xfail(strict=True)` en el Bloque B0; el A1 las volvió verdes.
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


async def test_turnos_concurrentes_del_mismo_negocio_no_se_cruzan(respx_mock, monkeypatch):
    """Dos conversaciones del mismo negocio, a la vez (el modelo tarda): cada
    respuesta sale con SU `dispatchId` y SU conversación, y cada turno lee SU
    contexto. Antes, el cliente compartido por organización mezclaba ambos."""
    import asyncio
    import json

    from app import dispatch
    from app.llm import LlmReply
    from app.multiorg import CrmSinOrganizacion, LlmSinOrganizacion
    from app.state import AppContext, MemoryStore
    from tests.conftest import make_settings

    def ctx_de(conv: str) -> dict:
        c = _contexto(conv, conv == "cv_a")
        c["contact"] = {"identity": f"id_{conv}", "name": conv.upper(), "ficha": {}}
        c["lead"] = {"stageName": "Nuevo"}
        return c

    respx_mock.get(f"{CRM_URL}/api/brains/context", params={"conversationId": "cv_a"}).mock(
        return_value=httpx.Response(200, json=ctx_de("cv_a")))
    respx_mock.get(f"{CRM_URL}/api/brains/context", params={"conversationId": "cv_b"}).mock(
        return_value=httpx.Response(200, json=ctx_de("cv_b")))
    respx_mock.post(f"{CRM_URL}/api/brains/typing").mock(return_value=httpx.Response(200, json={}))
    enviados = respx_mock.post(f"{CRM_URL}/api/brains/messages").mock(
        return_value=httpx.Response(201, json={"ok": True, "messageId": "m"}))

    class ModeloLento:
        def __init__(self, conv: str) -> None:
            self.conv = conv

        async def complete(self, messages, tools=None):
            await asyncio.sleep(0.05)
            # El prompt de ESTE turno nombra a SU lead, no al del otro.
            assert f"Nombre del lead: {self.conv.upper()}" in messages[0]["content"]
            return LlmReply(content=f"respuesta para {self.conv}")

    settings = make_settings(vocero_mode="cloud", crm_organization="", crm_brain_secret="s", llm_api_key="")
    registro = RegistroDeOrganizaciones(CRM_URL, "s")
    monkeypatch.setattr(registro, "llm", lambda org, slug, cfg, s: ModeloLento(cfg["model"].removeprefix("modelo-")))
    ctx = AppContext(settings=settings, store=MemoryStore(), crm=CrmSinOrganizacion(),
                     llm=LlmSinOrganizacion(), registro=registro)

    def despacho(conv: str) -> dict:
        return {"dispatchId": f"dsp_{conv}", "organization": {"id": "org_1", "slug": "n"},
                "conversation": {"id": conv}, "contact": {"identity": f"id_{conv}"},
                "messages": [{"id": f"m_{conv}", "type": "text", "text": "hola"}]}

    try:
        await asyncio.gather(dispatch._procesar(ctx, despacho("cv_a"), durable=True),
                             dispatch._procesar(ctx, despacho("cv_b"), durable=True))
    finally:
        await registro.aclose()

    cuerpos = sorted((json.loads(c.request.content) for c in enviados.calls), key=lambda b: b["conversationId"])
    assert cuerpos == [
        {"dispatchId": "dsp_cv_a", "conversationId": "cv_a", "text": "respuesta para cv_a"},
        {"dispatchId": "dsp_cv_b", "conversationId": "cv_b", "text": "respuesta para cv_b"},
    ]


async def test_nada_por_conversacion_se_acumula_en_el_contexto_global(respx_mock):
    """Fugas de memoria (071): tras muchos turnos, el cliente de la app no
    recuerda conversaciones y no quedan candados huérfanos."""
    mock_crm_basics(respx_mock)
    ctx = make_ctx()
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot.test") as c:
        for n in range(30):
            await despachar(c, esperar=True, frm=f"57300000{n:04d}", conv_id=f"cv_{n}", wamid=f"w{n}")
    await ctx.crm.aclose()
    assert ctx.crm._conversaciones == {}
    assert ctx.turn_locks == {} and ctx.turn_lock_users == {}
