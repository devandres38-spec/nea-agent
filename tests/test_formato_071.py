"""vocero-crm 071 (D18) — Formato por canal.

WhatsApp recibe el formato de WhatsApp (como siempre); Instagram, Messenger y
TikTok, texto plano. `texto_plano` es gemelo de `plainSocialText` del CRM: los
vectores viven en un JSON idéntico en los dos repos.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.formato import a_texto_plano, a_whatsapp, formatear_para, texto_plano

VECTORES = json.loads(
    (Path(__file__).parent / "fixtures" / "texto_plano_vectores.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("par", VECTORES["pares"], ids=lambda p: p["entrada"][:30])
def test_gemelo_de_plain_social_text(par):
    assert texto_plano(par["entrada"]) == par["salida"]


@pytest.mark.parametrize("texto", VECTORES["sinCambio"])
def test_no_toca_lo_que_no_es_formato(texto):
    assert texto_plano(texto) == texto


@pytest.mark.parametrize("texto", VECTORES["idempotentes"])
def test_es_idempotente(texto):
    una = texto_plano(texto)
    assert texto_plano(una) == una


MODELO = "## Precios\n**Consulta:** $800\n- _Limpieza_ incluida\n[Agenda](https://negocio.mx/agenda_2026)"


@pytest.mark.parametrize("canal", [None, "", "whatsapp", "WhatsApp"])
def test_whatsapp_y_sin_canal_como_siempre(canal):
    assert formatear_para(MODELO, canal) == a_whatsapp(MODELO)


@pytest.mark.parametrize("canal", ["instagram", "messenger", "tiktok"])
def test_en_las_redes_sale_sin_marcas(canal):
    sale = formatear_para(MODELO, canal)
    assert sale == a_texto_plano(MODELO)
    assert "*" not in sale and "#" not in sale and "_Limpieza_" not in sale
    assert "https://negocio.mx/agenda_2026" in sale  # la URL intacta
    assert "Consulta: $800" in sale


def test_a_texto_plano_es_idempotente():
    una = a_texto_plano(MODELO)
    assert a_texto_plano(una) == una


async def test_el_turno_de_instagram_guarda_y_manda_texto_plano(respx_mock):
    import asyncio

    import httpx

    from app.llm import LlmReply
    from app.main import create_app
    from tests.conftest import IDENTITY, FakeLLM, despachar, make_ctx, mock_crm_basics

    llm = FakeLLM([LlmReply(content="*Hola* 👋 ¿Buscas **limpieza** o _ortodoncia_?", tool_calls=[])])
    ctx = make_ctx(llm=llm)
    routes = mock_crm_basics(respx_mock)
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot.test") as c:
        await despachar(c, ctx, text="hola", wamid="wamid.ig1", channel="instagram", esperar=True)
        await asyncio.sleep(0.1)
    enviado = json.loads(routes["messages"].calls[-1].request.content)["text"]
    assert enviado == "Hola 👋 ¿Buscas limpieza o ortodoncia?"
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    guardado = [m.content for m in await ctx.store.recent_messages(conv.id, 5) if m.role == "assistant"]
    assert guardado == [enviado]
    await ctx.crm.aclose()


def test_corrige_respuesta_no_dice_whatsapp():
    from app.turn import CORRIGE_RESPUESTA

    assert "WhatsApp" not in CORRIGE_RESPUESTA
