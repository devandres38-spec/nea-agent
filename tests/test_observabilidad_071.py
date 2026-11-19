"""vocero-crm 071 (D8–D11) — Logs, /health, docs y ensayo acotado."""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from app.main import create_app
from app.observabilidad import (
    EnmascararDigitos,
    FormatoJson,
    contexto_de_log,
    enmascarar,
)
from tests.conftest import make_ctx, make_settings


# ───────────────────────────────────────────────────────────── D8: logs ───


def _registro(msg: str, args: tuple = (), nombre: str = "nea.turn") -> logging.LogRecord:
    return logging.LogRecord(nombre, logging.INFO, __file__, 1, msg, args, None)


def test_enmascara_telefonos_e_identidades_en_mensaje_y_argumentos():
    r = _registro("turno de %s: handoff en %s", ("573001112233", "cv_9"))
    EnmascararDigitos().filter(r)
    assert r.getMessage() == "turno de ***2233: handoff en cv_9"
    r = _registro("allowlist: 525550001111 fuera — bsuid:US.13491208655302741918")
    EnmascararDigitos().filter(r)
    assert "525550001111" not in r.getMessage() and "13491208655302741918" not in r.getMessage()
    # Lo corto (ids de pendiente, años) se queda.
    assert enmascarar("pending 42 en 2026") == "pending 42 en 2026"


def test_el_enmascarado_no_rompe_el_formato_de_accesos_de_uvicorn():
    from uvicorn.logging import AccessFormatter

    r = _registro('%s - "%s %s HTTP/%s" %d', ("10.0.0.1:5000", "GET", "/health", "1.1", 200), "uvicorn.access")
    EnmascararDigitos().filter(r)
    AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(r)


def test_json_lleva_el_contexto_del_turno_y_nada_fuera_de_el():
    formato = FormatoJson()
    with contexto_de_log(org="clinica-norte", dispatch="dsp_1", conversation="cv_1"):
        r = _registro("turno de %s", ("573001112233",))
        EnmascararDigitos().filter(r)
        linea = json.loads(formato.format(r))
    assert linea["org"] == "clinica-norte"
    assert linea["dispatch"] == "dsp_1" and linea["conversation"] == "cv_1"
    assert linea["msg"] == "turno de ***2233" and linea["level"] == "INFO"
    fuera = json.loads(formato.format(_registro("arranque")))
    assert "org" not in fuera and "dispatch" not in fuera


async def test_las_tareas_del_turno_heredan_el_contexto():
    from app.observabilidad import contexto_actual

    async def dentro() -> dict:
        await asyncio.sleep(0)
        return contexto_actual()

    with contexto_de_log(org="a", dispatch="d"):
        tarea = asyncio.create_task(dentro())
    assert await tarea == {"org": "a", "dispatch": "d"}


def test_el_formato_por_defecto_depende_del_modo():
    assert make_settings().formato_de_logs == "text"
    assert make_settings(vocero_mode="cloud").formato_de_logs == "json"
    assert make_settings(vocero_mode="cloud", log_format="text").formato_de_logs == "text"


# ───────────────────────────────────────────────────────── D9: /health ───


async def _get(ctx, ruta: str) -> httpx.Response:
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot.test") as c:
        resp = await c.get(ruta)
    await ctx.crm.aclose()
    return resp


async def test_health_ensena_las_colas_y_sigue_siendo_chico():
    ctx = make_ctx()
    await ctx.store.enqueue_dispatch("org_a", {
        "dispatchId": "d1", "organization": {"id": "org_a"}, "conversation": {"id": "cv_1"},
        "contact": {"identity": "1"}, "messages": [{"id": "m", "type": "text", "text": "x"}],
    })
    resp = await _get(ctx, "/health")
    assert resp.status_code == 200
    assert resp.json()["queues"]["inboxPending"] == 1
    assert len(resp.content) < 1024  # el CRM lee como mucho 4 KB (salud.ts)


async def test_si_las_colas_fallan_health_sigue_en_200():
    ctx = make_ctx()

    async def rota() -> dict:
        raise RuntimeError("consulta lenta")

    ctx.store.queue_metrics = rota  # type: ignore[method-assign]
    resp = await _get(ctx, "/health")
    assert resp.status_code == 200 and resp.json()["queues"] is None


async def test_si_las_colas_tardan_no_retienen_health():
    ctx = make_ctx()

    async def lenta() -> dict:
        await asyncio.sleep(5)
        return {}

    ctx.store.queue_metrics = lenta  # type: ignore[method-assign]
    resp = await asyncio.wait_for(_get(ctx, "/health"), timeout=3)
    assert resp.json()["queues"] is None


# ─────────────────────────────────────────────────────────── D10: docs ───


@pytest.mark.parametrize("ruta", ["/docs", "/redoc", "/openapi.json"])
async def test_sin_documentacion_publica_por_defecto(ruta, monkeypatch):
    monkeypatch.delenv("NEA_DOCS", raising=False)
    assert (await _get(make_ctx(), ruta)).status_code == 404


async def test_nea_docs_la_enciende(monkeypatch):
    monkeypatch.setenv("NEA_DOCS", "1")
    assert (await _get(make_ctx(), "/openapi.json")).status_code == 200
