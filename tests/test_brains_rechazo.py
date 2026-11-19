"""vocero-crm 070 — Auditoría (C-1, B-7): cómo responde Nea `multiorg` a los
códigos de `/api/brains/*` que no son de éxito.

C-1: un `401`/`403` del CRM (credencial reasignada, cerebro pausado, secreto
rotado, negocio suspendido) es TERMINAL. Antes, el despacho se quedaba en la
cola durable y se reintentaba cada 2 s —y el handoff también fallaba—, y cada
401 contaba en el límite por IP del CRM que comparten todos los negocios de la
Nea: un negocio reasignado acababa en `429` para todos. Ahora sale de la cola
sin handoff.

B-7: los códigos tipados que el CRM 2.10.0 devuelve al responder
(`409 cerebro_reasignado`, `422 fuera_de_ventana`, `403 organizacion_suspendida`)
y cómo los lee `BrainsCrmClient`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.crm import CrmConflict, CrmRechazo
from app.crm_brains import BrainsCrmClient
from app.dispatch_worker import drain_one
from app.multiorg import CrmSinOrganizacion, LlmSinOrganizacion, RegistroDeOrganizaciones, credencial_derivada
from app.state import AppContext, MemoryStore
from tests.conftest import make_settings

SECRETO = "un-secreto-de-despliegue-largo-y-aburrido"
CRM = "http://crm.test"


def _ctx() -> AppContext:
    settings = make_settings(
        vocero_mode="cloud", crm_organization="", crm_brain_secret=SECRETO, llm_api_key="", crm_base_url=CRM
    )
    return AppContext(
        settings=settings,
        store=MemoryStore(),
        crm=CrmSinOrganizacion(),
        llm=LlmSinOrganizacion(),
        registro=RegistroDeOrganizaciones(CRM, SECRETO),
    )


def _despacho(dispatch_id: str = "dsp_c1") -> dict:
    return {
        "dispatchId": dispatch_id,
        "organization": {"id": "org_abc123", "slug": "clinica-norte"},
        "conversation": {"id": "cv_1", "channel": "whatsapp", "aiEnabled": True, "windowOpen": True},
        "contact": {"id": "ct_1", "displayName": "Ana", "identity": "573001112233"},
        "messages": [{"id": "msg_1", "type": "text", "text": "hola"}],
        "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
    }


def _terminados(ctx: AppContext) -> bool:
    """Todos los jobs de la cola en memoria quedaron `done` (no se reintentan)."""
    filas = list(ctx.store.dispatch_inbox.values())
    return bool(filas) and all(f["state"] == "done" for f in filas)


def _cliente() -> BrainsCrmClient:
    return BrainsCrmClient(CRM, credencial_derivada(SECRETO, "org_abc123"), "clinica-norte")


# ------------------------------------------------------------------ C-1 ---


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_credencial_rechazada_saca_el_despacho_de_la_cola_sin_handoff(status: int):
    cuerpo = {"error": {"code": "organizacion_suspendida"}} if status == 403 else {"error": {"code": "unauthorized"}}
    contexto = respx.get(f"{CRM}/api/brains/context").mock(return_value=httpx.Response(status, json=cuerpo))
    handoff = respx.post(f"{CRM}/api/brains/handoff").mock(return_value=httpx.Response(status, json=cuerpo))
    ctx = _ctx()
    await ctx.store.enqueue_dispatch("org_abc123", _despacho())

    assert await drain_one(ctx) is True
    # Terminado: el siguiente barrido no encuentra nada que reintentar.
    assert _terminados(ctx)
    assert await drain_one(ctx) is False
    # Un solo intento contra el CRM y NINGÚN handoff (también fallaría y sumaría 401).
    assert contexto.call_count == 1
    assert handoff.call_count == 0
    await ctx.registro.aclose()


@respx.mock
async def test_un_despacho_vencido_con_credencial_rechazada_tampoco_se_queda():
    """El camino del vencido/recuperado llama a handoff: con 401 también termina."""
    handoff = respx.post(f"{CRM}/api/brains/handoff").mock(return_value=httpx.Response(401, json={}))
    ctx = _ctx()
    vencido = {**_despacho("dsp_vencido"), "expiresAt": "2020-01-01T00:00:00Z"}
    await ctx.store.enqueue_dispatch("org_abc123", vencido)
    assert await drain_one(ctx) is True
    assert _terminados(ctx)
    assert await drain_one(ctx) is False
    assert handoff.call_count == 1
    await ctx.registro.aclose()


@respx.mock
async def test_otro_fallo_del_crm_si_pasa_a_una_persona():
    """Un 500 no es un rechazo de credencial: se intenta el handoff como siempre."""
    respx.get(f"{CRM}/api/brains/context").mock(return_value=httpx.Response(500))
    handoff = respx.post(f"{CRM}/api/brains/handoff").mock(return_value=httpx.Response(200, json={"ok": True}))
    ctx = _ctx()
    await ctx.store.enqueue_dispatch("org_abc123", _despacho("dsp_500"))
    assert await drain_one(ctx) is True
    assert handoff.call_count == 1
    await ctx.registro.aclose()


@respx.mock
async def test_el_cliente_traduce_401_y_403_a_crm_rechazo():
    respx.post(f"{CRM}/api/brains/ficha").mock(return_value=httpx.Response(401, json={"error": {"code": "unauthorized"}}))
    respx.put(f"{CRM}/api/brains/ficha").mock(
        return_value=httpx.Response(403, json={"error": {"code": "organizacion_suspendida"}})
    )
    c = _cliente()
    with pytest.raises(CrmRechazo) as e:
        await c.put_ficha("cv_1", {"nombre": "Ana"})
    assert (e.value.status, e.value.code) == (403, "organizacion_suspendida")
    await c.aclose()


@respx.mock
async def test_la_sonda_con_credencial_rechazada_no_concluye():
    respx.get(f"{CRM}/api/brains/agenda/slots").mock(return_value=httpx.Response(401, json={}))
    c = _cliente()
    assert await c.sondear_agenda() is None
    await c.aclose()


# ------------------------------------------------------------------ B-7 ---


@respx.mock
async def test_409_cerebro_reasignado_es_un_conflicto_tipado():
    respx.post(f"{CRM}/api/brains/messages").mock(
        return_value=httpx.Response(409, json={"error": {"code": "cerebro_reasignado", "message": "otro cerebro"}})
    )
    c = _cliente()
    c.registrar_despacho("cv_1", "dsp_1")
    with pytest.raises(CrmConflict) as e:
        await c.send_message("cv_1", "hola")
    assert e.value.code == "cerebro_reasignado"
    await c.aclose()


@respx.mock
async def test_422_fuera_de_ventana_se_lee_como_ventana_cerrada():
    respx.post(f"{CRM}/api/brains/messages").mock(
        return_value=httpx.Response(422, json={"error": {"code": "fuera_de_ventana", "message": "ventana"}})
    )
    c = _cliente()
    c.registrar_despacho("cv_1", "dsp_1")
    with pytest.raises(CrmConflict) as e:
        await c.send_message("cv_1", "hola")
    assert e.value.code == "window_closed"
    await c.aclose()


@respx.mock
async def test_403_organizacion_suspendida_al_responder_es_terminal():
    respx.post(f"{CRM}/api/brains/messages").mock(
        return_value=httpx.Response(403, json={"error": {"code": "organizacion_suspendida"}})
    )
    c = _cliente()
    c.registrar_despacho("cv_1", "dsp_1")
    with pytest.raises(CrmRechazo) as e:
        await c.send_message("cv_1", "hola")
    assert e.value.status == 403
    await c.aclose()
