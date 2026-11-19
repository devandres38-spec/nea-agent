"""vocero-crm 070 — Contrato gemelo de Nea `multiorg` con `/api/brains/*`.

Los vectores de aquí están copiados byte a byte de
`tests/unit/cerebros-070-gemelo.test.ts` en vocero-crm:

- la credencial derivada (`vocero:cerebro:v1`, base64url sin relleno);
- el cuerpo del despacho en modo plataforma (con `expiresAt` al final) y su
  firma con el secreto del despliegue;
- el evento `conversation.deleted` y su firma;
- las rutas que llama `BrainsCrmClient`, que el CRM comprueba que existen.

Si un lado cambia sin el otro, una de las dos suites se pone roja en vez de
fallar en producción con un 401 o un 422 mudos.
"""
from __future__ import annotations

import json

import httpx
import respx

from app.crm_brains import RUTAS, RUTAS_POR_PREFIJO, BrainsCrmClient
from app.main import create_app
from app.multiorg import CrmSinOrganizacion, LlmSinOrganizacion, RegistroDeOrganizaciones, credencial_derivada
from app.state import AppContext, MemoryStore
from tests.conftest import firmar, make_settings

SECRETO = "un-secreto-de-despliegue-largo-y-aburrido"

CUERPO_MULTI = (
    '{"dispatchId":"dsp_gemelo_multi_0001","organization":{"id":"org_abc123","slug":"clinica-norte"},'
    '"conversation":{"id":"cv_gemelo","channel":"whatsapp","aiEnabled":true,"windowOpen":true,"windowExpiresAt":"2026-11-19T15:00:05.000Z"},'
    '"contact":{"id":"ct_gemelo","displayName":"Ana Pérez","identity":"573001112233"},'
    '"messages":[{"id":"msg_g1","at":"2026-11-18T15:00:05.000Z","type":"text","text":"hola, ¿tienen cita mañana?","mediaId":null}],'
    '"firstMessageAt":"2026-11-18T15:00:05.000Z","lastMessageAt":"2026-11-18T15:00:05.000Z","expiresAt":"2026-11-18T15:10:00.000Z"}'
).encode("utf-8")
FIRMA_MULTI = "sha256=5795926178909e5f3f532f5faa2de50c470326cea8c5d23952c6f976cbaed67e"

CUERPO_OLVIDO = (
    '{"type":"conversation.deleted","eventId":"olv_gemelo","organization":{"id":"org_abc123","slug":"clinica-norte"},'
    '"conversation":{"id":"cv_gemelo","channel":"whatsapp"},"contact":{"id":"ct_gemelo","identity":"573001112233"},'
    '"deletedAt":"2026-11-18T16:00:00.000Z"}'
).encode("utf-8")
FIRMA_OLVIDO = "sha256=4c7f3c9fad2a3efb8d18d293f7525c01bf193a15e119fa2fb3a5753d59b2843e"

# Las que el CRM comprueba en `RUTAS_DE_NEA` (cerebros-070-gemelo.test.ts).
RUTAS_DEL_CRM = {
    "/api/brains/context",
    "/api/brains/messages",
    "/api/brains/ficha",
    "/api/brains/stage",
    "/api/brains/handoff",
    "/api/brains/typing",
    "/api/brains/agenda/slots",
    "/api/brains/agenda/book",
    "/api/brains/agenda/bookings",
    "/api/brains/agenda/cancel",
    "/api/brains/agenda/followup",
    "/api/brains/media/",
    "/api/brains/llm/chat/completions",
}


def _ctx_multiorg() -> AppContext:
    settings = make_settings(vocero_mode="cloud", crm_organization="", crm_brain_secret=SECRETO, llm_api_key="")
    return AppContext(
        settings=settings,
        store=MemoryStore(),
        crm=CrmSinOrganizacion(),
        llm=LlmSinOrganizacion(),
        registro=RegistroDeOrganizaciones("http://crm.test", SECRETO),
    )


def _cliente(ctx: AppContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://bot.test")


def test_la_credencial_es_la_del_crm():
    assert credencial_derivada(SECRETO, "org_abc123") == "_BqRKryMUdexcuLj_3RG1ARbgqr_LhXRSLJYmrw5VJ0"


def test_las_firmas_gemelas_son_las_de_los_cuerpos_gemelos():
    assert firmar(CUERPO_MULTI, SECRETO) == FIRMA_MULTI
    assert firmar(CUERPO_OLVIDO, SECRETO) == FIRMA_OLVIDO


async def test_el_despacho_multi_del_crm_entra_en_una_nea_multiorg():
    """Firmado con el secreto del despliegue, se encola bajo SU organización."""
    ctx = _ctx_multiorg()
    async with _cliente(ctx) as client:
        resp = await client.post("/vocero/dispatch", content=CUERPO_MULTI, headers={"x-vocero-signature": FIRMA_MULTI})
    assert resp.status_code == 200
    assert [*ctx.store.dispatch_inbox] == [("org_abc123", "dsp_gemelo_multi_0001")]
    # `expiresAt` llega tal cual: el worker lo usa para pasar a humano un despacho vencido.
    assert "2026-11-18T15:10:00.000Z" in str(ctx.store.dispatch_inbox[("org_abc123", "dsp_gemelo_multi_0001")])


async def test_el_olvido_del_crm_entra_en_una_nea_multiorg():
    ctx = _ctx_multiorg()
    async with _cliente(ctx) as client:
        resp = await client.post("/vocero/dispatch", content=CUERPO_OLVIDO, headers={"x-vocero-signature": FIRMA_OLVIDO})
    assert resp.status_code == 200
    assert [*ctx.store.dispatch_inbox] == [("org_abc123", "olvido:olv_gemelo")]


async def test_con_otro_secreto_es_401():
    ctx = _ctx_multiorg()
    async with _cliente(ctx) as client:
        resp = await client.post(
            "/vocero/dispatch", content=CUERPO_MULTI, headers={"x-vocero-signature": firmar(CUERPO_MULTI, "otro-secreto")}
        )
    assert resp.status_code == 401
    assert not getattr(ctx.store, "dispatch_inbox", {})


def test_toda_ruta_que_llama_nea_existe_en_el_crm():
    """`RUTAS` y los prefijos solo pueden apuntar a rutas que el CRM publica."""
    destinos = set(RUTAS.values()) | set(RUTAS_POR_PREFIJO.values())
    destinos |= {
        "/api/brains/agenda/bookings",
        "/api/brains/agenda/cancel",
        "/api/brains/agenda/followup",
        "/api/brains/llm/chat/completions",
    }
    assert destinos <= RUTAS_DEL_CRM, destinos - RUTAS_DEL_CRM
    # Ninguna se queda en `/api/bot/*`: en un Vocero multitenant ese prefijo es 404.
    assert all(d.startswith("/api/brains/") for d in destinos)


@respx.mock
async def test_move_stage_sale_por_brains_en_cloud():
    """D3 — antes salía a `/api/bot/stage` (404 en multitenant)."""
    ruta = respx.post("http://crm.test/api/brains/stage").mock(
        return_value=httpx.Response(200, json={"ok": True, "changed": True, "lead": {"stageName": "Calificado"}})
    )
    cliente = BrainsCrmClient("http://crm.test", credencial_derivada(SECRETO, "org_abc123"), "clinica-norte")
    data = await cliente.move_stage("cv_gemelo", "Calificado")
    assert data["changed"] is True
    peticion = ruta.calls.last.request
    assert peticion.headers["authorization"] == f"Bearer {credencial_derivada(SECRETO, 'org_abc123')}"
    assert peticion.headers["x-vocero-organization"] == "clinica-norte"
    assert json.loads(peticion.content) == {"conversationId": "cv_gemelo", "stage": "Calificado"}
    await cliente.aclose()
