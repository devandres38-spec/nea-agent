"""vocero-crm 071 (D5, D12, FR-010) — Agenda por negocio e imágenes honestas."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app import dispatch
from app.agenda import agenda_vigente
from app.llm import ImagenNoSoportada, LlmReply, OpenAiLlm
from app.multiorg import CrmSinOrganizacion, LlmSinOrganizacion, RegistroDeOrganizaciones
from app.profile import BusinessProfile, profile_from_payload
from app.crm_brains import _perfil_desde_contexto
from app.state import AppContext, MemoryStore
from app.stall import ALERTA as STALL_ALERT
from tests.conftest import CRM_URL, FakeLLM, despachar, make_ctx, make_settings, mock_crm_basics

from app.main import create_app


# ─────────────────────────────────────────────── D5: agenda por negocio ───


def test_el_perfil_lee_agenda_enabled_del_contexto():
    p = profile_from_payload(_perfil_desde_contexto({"agent": {"agendaEnabled": False}}), "A")
    assert p.agenda_enabled is False
    assert profile_from_payload(_perfil_desde_contexto({"agent": {}}), "A").agenda_enabled is None
    # Un valor que no es booleano no cuenta: manda la sonda.
    assert profile_from_payload({"profile": {"agendaEnabled": "sí"}}, "A").agenda_enabled is None


async def test_lo_que_dice_el_crm_manda_sin_sondear():
    class SondaQueNoDebeUsarse:
        def apagada_por_404(self) -> bool:
            return False

        async def vigente(self) -> bool:  # pragma: no cover
            raise AssertionError("con agendaEnabled no se sondea")

    ctx = make_ctx()
    ctx.agenda_sonda = SondaQueNoDebeUsarse()
    assert await agenda_vigente(ctx, BusinessProfile(agenda_enabled=False)) is False
    assert await agenda_vigente(ctx, BusinessProfile(agenda_enabled=True)) is True
    await ctx.crm.aclose()


async def test_un_404_de_la_bandera_manda_sobre_el_contexto_hasta_el_ttl():
    reg = RegistroDeOrganizaciones(CRM_URL, "s", agenda_ttl=60)
    sonda = reg.sonda("org_a", "a")
    sonda.marcar_apagada()
    ctx = make_ctx()
    ctx.agenda_sonda = sonda
    assert await agenda_vigente(ctx, BusinessProfile(agenda_enabled=True)) is False
    await reg.aclose()
    await ctx.crm.aclose()


async def test_el_404_de_un_negocio_no_apaga_la_agenda_de_otro():
    """H5: antes la sonda era una para toda la Nea multiorg."""
    reg = RegistroDeOrganizaciones(CRM_URL, "s", agenda_ttl=60)
    a, b = reg.sonda("org_a", "a"), reg.sonda("org_b", "b")
    assert a is not b and reg.sonda("org_a", "a") is a
    a.marcar_apagada()
    assert a.valor is False and b.valor is True
    await reg.aclose()


async def test_sin_agenda_enabled_la_sonda_pregunta_con_la_credencial_del_negocio(respx_mock):
    """Un CRM 2.10 (sin el campo): la sonda del negocio, con el id del negocio."""
    ruta = respx_mock.get(f"{CRM_URL}/api/brains/agenda/slots").mock(return_value=httpx.Response(404))
    reg = RegistroDeOrganizaciones(CRM_URL, "s", agenda_ttl=60)
    ctx = make_ctx()
    ctx.agenda_sonda = reg.sonda("org_a", "a")
    assert await agenda_vigente(ctx, BusinessProfile()) is False
    assert ruta.calls.last.request.headers["x-vocero-organization"] == "org_a"
    await reg.aclose()
    await ctx.crm.aclose()


def _contexto(conv: str, agenda: bool) -> dict[str, Any]:
    return {
        "contact": {"identity": f"id_{conv}", "name": conv, "ficha": {}},
        "conversation": {"id": conv, "aiEnabled": True, "windowOpen": True},
        "lead": {"stageName": "Nuevo"},
        "agent": {"name": "A", "agendaEnabled": agenda},
        "llm": {"path": "/api/brains/llm", "model": "m"},
    }


async def test_cada_negocio_ve_su_propia_agenda_en_el_turno(respx_mock, monkeypatch):
    for org, agenda in (("org_a", False), ("org_b", True)):
        respx_mock.get(f"{CRM_URL}/api/brains/context", params={"conversationId": f"cv_{org}"}).mock(
            return_value=httpx.Response(200, json=_contexto(f"cv_{org}", agenda)))
    respx_mock.post(f"{CRM_URL}/api/brains/typing").mock(return_value=httpx.Response(200, json={}))
    respx_mock.post(f"{CRM_URL}/api/brains/messages").mock(return_value=httpx.Response(201, json={"ok": True}))
    sondas = respx_mock.get(f"{CRM_URL}/api/brains/agenda/slots").mock(return_value=httpx.Response(200, json={}))
    modelo = FakeLLM()
    reg = RegistroDeOrganizaciones(CRM_URL, "s")
    monkeypatch.setattr(reg, "llm", lambda *a: modelo)
    ctx = AppContext(
        settings=make_settings(vocero_mode="cloud", crm_organization="", crm_brain_secret="s", llm_api_key=""),
        store=MemoryStore(), crm=CrmSinOrganizacion(), llm=LlmSinOrganizacion(), registro=reg,
    )
    try:
        for org in ("org_a", "org_b"):
            await dispatch._procesar(ctx, {
                "dispatchId": f"d_{org}", "organization": {"id": org, "slug": org},
                "conversation": {"id": f"cv_{org}"}, "contact": {"identity": f"id_cv_{org}"},
                "messages": [{"id": f"m_{org}", "type": "text", "text": "quiero una cita"}],
            }, durable=True)
    finally:
        await reg.aclose()

    sin_agenda, con_agenda = modelo.calls
    nombres = lambda c: {t["function"]["name"] for t in c["tools"] or []}  # noqa: E731
    assert "propose_slots" not in nombres(sin_agenda)
    assert "ESTE NEGOCIO NO AGENDA" in sin_agenda["messages"][0]["content"]
    assert "propose_slots" in nombres(con_agenda)
    assert sondas.call_count == 0  # con agendaEnabled no hay sondeo


# ─────────────────────────────────── FR-010: la imagen va al lead, no al sistema ───


async def test_la_imagen_se_adjunta_al_ultimo_mensaje_del_lead_aunque_haya_alerta(respx_mock):
    mock_crm_basics(respx_mock)
    ctx = make_ctx(make_settings(stall_max_turns=1))  # fuerza la ALERTA de cierre
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot.test") as c:
        await despachar(c, esperar=True, msg_type="image", text=None)
    await ctx.crm.aclose()

    mensajes = ctx.llm.calls[0]["messages"]
    assert mensajes[-1] == {"role": "system", "content": STALL_ALERT}
    con_imagen = [m for m in mensajes if isinstance(m["content"], list)]
    assert len(con_imagen) == 1 and con_imagen[0]["role"] == "user"
    assert any(p["type"] == "image_url" for p in con_imagen[0]["content"])


# ─────────────────────────────────────────── D12: modelos que no ven ───


ERROR_SIN_VISION = {"error": {"message": "No endpoints found that support image input", "code": 404}}
CON_IMAGEN = [{"role": "user", "content": [
    {"type": "text", "text": "mira"},
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
]}]


async def test_un_modelo_sin_vision_no_se_reintenta_y_se_recuerda(respx_mock):
    ruta = respx_mock.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(404, json=ERROR_SIN_VISION))
    llm = OpenAiLlm("k", "z-ai/glm", base_url="http://llm.test/v1")
    with pytest.raises(ImagenNoSoportada):
        await llm.complete(CON_IMAGEN)
    assert ruta.call_count == 1  # antes: 3 intentos
    with pytest.raises(ImagenNoSoportada):
        await llm.complete(CON_IMAGEN)
    assert ruta.call_count == 1  # pegajoso: ni se intenta
    await llm._client.close()


async def test_sin_vision_el_turno_contesta_con_honestidad_y_sin_handoff(respx_mock):
    rutas = mock_crm_basics(respx_mock)

    class ModeloCiego(FakeLLM):
        async def complete(self, messages, tools=None):
            self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools})
            if any(isinstance(m["content"], list) for m in messages):
                raise ImagenNoSoportada("No endpoints found that support image input")
            return LlmReply(content="No alcanzo a ver la foto por aquí, ¿me cuentas qué muestra?")

    ctx = make_ctx(llm=ModeloCiego())
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot.test") as c:
        await despachar(c, esperar=True, msg_type="image", text=None)
    await ctx.crm.aclose()

    assert len(ctx.llm.calls) == 2  # el intento con imagen y UNO sin ella
    segundo = ctx.llm.calls[1]["messages"]
    texto_del_lead = next(m["content"] for m in reversed(segundo) if m["role"] == "user")
    assert "puedes verla" not in texto_del_lead and "no puedes ver imágenes" in texto_del_lead
    assert rutas["messages"].call_count == 1
    assert rutas["handoff"].call_count == 0
