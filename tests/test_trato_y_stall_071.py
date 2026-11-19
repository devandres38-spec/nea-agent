"""vocero-crm 071 (D15, D16) — Trato del negocio, regla de idioma y `stallEnabled`."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.chasis.armar import LINEA_IDIOMA, VOZ_NEUTRO, VOZ_TU, VOZ_USTED
from app.crm_brains import _perfil_desde_contexto
from app.profile import BusinessProfile, profile_from_payload
from app.prompt import build_system_prompt
from app.state import Conversation, utcnow
from tests.conftest import IDENTITY, despachar, mock_crm_basics

AHORA = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
BOGOTA = ZoneInfo("America/Bogota")


def _prompt(perfil: BusinessProfile) -> str:
    return build_system_prompt(
        profile=perfil, context=None, conv=Conversation(id=1, wa_identity="1"), now=AHORA, tz=BOGOTA
    )


# ───────────────────────────────────────────────────────── perfil ───


@pytest.mark.parametrize(
    "valor,esperado",
    [("tu", "tu"), ("usted", "usted"), ("neutro", "neutro"), ("vos", "tu"), (None, "tu"), (3, "tu")],
)
def test_el_trato_se_lee_y_lo_desconocido_es_tu(valor, esperado):
    p = profile_from_payload({"profile": {"name": "Sofi", "treatment": valor}}, "Asistente")
    assert p.treatment == esperado


@pytest.mark.parametrize("valor,esperado", [(True, True), (False, False), (None, True), ("no", True)])
def test_stall_enabled_ausente_o_raro_es_true(valor, esperado):
    p = profile_from_payload({"profile": {"stallEnabled": valor}}, "Asistente")
    assert p.stall_enabled is esperado


def test_en_cloud_llegan_por_el_contexto():
    p = profile_from_payload(
        _perfil_desde_contexto({"agent": {"name": "Ana", "treatment": "usted", "stallEnabled": False}}),
        "Asistente",
    )
    assert (p.treatment, p.stall_enabled, p.cloud) == ("usted", False, True)


# ───────────────────────────────────────────────────────── prompt ───


@pytest.mark.parametrize(
    "trato,voz", [("tu", VOZ_TU), ("usted", VOZ_USTED), ("neutro", VOZ_NEUTRO), ("vos", VOZ_TU)]
)
def test_la_voz_sigue_el_trato_y_la_regla_de_idioma_va_siempre(trato, voz):
    texto = _prompt(BusinessProfile(agent_name="Sofi", treatment=trato))
    assert voz + "\n" + LINEA_IDIOMA in texto
    otras = {VOZ_TU, VOZ_USTED, VOZ_NEUTRO} - {voz}
    assert not any(o in texto for o in otras)


def test_usted_y_neutro_no_dejan_el_tuteo_como_regla():
    assert 'de "tú"' not in VOZ_USTED.split(". Los ejemplos")[0]
    assert "sin tutear ni ustedear" in VOZ_NEUTRO


# ─────────────────────────────────────────────── stallEnabled (D16) ───


class _Perfil:
    def __init__(self, perfil: BusinessProfile) -> None:
        self._p = perfil

    async def get(self) -> BusinessProfile:
        return self._p


async def _vacios(client, n: int = 4):
    for i, texto in enumerate(("ok", "va", "ajá", "ok")[:n]):
        await despachar(client, text=texto, wamid=f"wamid.s{i}")
        await asyncio.sleep(0.35)


async def test_con_stall_apagado_nunca_se_despide_por_falta_de_rumbo(ctx, client, respx_mock):
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi", stall_enabled=False))
    routes = mock_crm_basics(respx_mock)
    await _vacios(client)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is None
    assert routes["messages"].call_count == 4  # contestó todos
    sistemas = [m["content"] for c in ctx.llm.calls for m in c["messages"] if m["role"] == "system"]
    assert not any("UNA línea cálida de cierre" in s for s in sistemas)


async def test_con_stall_encendido_sigue_cerrando(ctx, client, respx_mock):
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi", stall_enabled=True))
    mock_crm_basics(respx_mock)
    await _vacios(client, 3)
    assert (await ctx.store.get_or_create_conversation(IDENTITY)).stalled_at is not None


async def test_con_stall_apagado_un_relleno_tras_un_cierre_viejo_se_contesta(ctx, client, respx_mock):
    """Sin esto, un negocio que lo apaga sigue viendo 24 h de silencio."""
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi", stall_enabled=False))
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(
        conv.id, crm_conversation_id="cv_test1", phase="cerrada", stalled_at=utcnow()
    )
    await despachar(client, text="gracias", wamid="wamid.g1")
    await asyncio.sleep(0.35)
    assert routes["messages"].call_count == 1
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is None and conv.phase == "descubrimiento"


async def test_con_stall_encendido_el_relleno_tras_el_cierre_calla(ctx, client, respx_mock):
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi"))
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(
        conv.id, crm_conversation_id="cv_test1", phase="cerrada", stalled_at=utcnow()
    )
    await despachar(client, text="gracias", wamid="wamid.g1")
    await asyncio.sleep(0.35)
    assert routes["messages"].call_count == 0
