"""Auditoría 071 — regresiones de los hallazgos medios y bajos de nea-agent.

M-1 PII en excepciones y formateadores · M-2 `/health` con la base congelada ·
M-4 fallback de enfoque y línea de primer contacto del rol · B-1 pool HTTP ·
B-2 candados con esquema, purga parcial, `GREATEST` · B-3 cupos de ensayo en
el contexto y cierre independiente.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.observabilidad import EnmascararDigitos, FormatoJson, FormatoTexto
from tests.conftest import make_ctx

TEL = "573001112233"


def _registro(msg: str, args: tuple = (), exc_info: Any = None) -> logging.LogRecord:
    return logging.LogRecord("nea.turn", logging.WARNING, __file__, 1, msg, args, exc_info)


# ──────────────────────────────────────────────────────────── M-1 ───


def test_m1_una_excepcion_como_argumento_se_enmascara():
    r = _registro("envío falló: %s", (RuntimeError(f"409 para {TEL}"),))
    EnmascararDigitos().filter(r)
    assert TEL not in r.getMessage() and "***2233" in r.getMessage()


def test_m1_json_enmascara_aunque_no_haya_pasado_el_filtro():
    r = _registro("turno de %s", (TEL,))
    linea = json.loads(FormatoJson().format(r))
    assert linea["msg"] == "turno de ***2233"


def test_m1_texto_enmascara_el_traceback():
    try:
        raise ValueError(f"identidad {TEL} rara")
    except ValueError:
        r = _registro("reventó", exc_info=sys.exc_info())
    salida = FormatoTexto().format(r)
    assert TEL not in salida and "***2233" in salida


# ──────────────────────────────────────────────────────────── M-2 ───


async def test_m2_health_no_se_cuelga_con_la_base_congelada(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "PING_TIMEOUT_SECONDS", 0.2)
    ctx = make_ctx()

    async def congelada() -> None:
        await asyncio.sleep(30)

    ctx.store.ping = congelada  # type: ignore[method-assign]
    app = main.create_app(ctx=ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot.test") as c:
        resp = await asyncio.wait_for(c.get("/health"), timeout=3)
    await ctx.crm.aclose()
    assert resp.status_code == 503 and resp.json()["status"] == "degraded"


# ──────────────────────────────────────────────────────────── M-4 ───


def test_m4_un_rol_sin_enfoque_calificar_no_revienta():
    from app.roles import ATENDER_Y_AGENDAR, Enfoque, RolSpec

    unico = Enfoque(mision="m", primer_mensaje="p", lead_listo="l")
    rol = RolSpec(clave="nuevo", enfoques={ATENDER_Y_AGENDAR: unico})
    assert rol.enfoque("calificar") is unico


def test_m4_la_linea_de_primer_contacto_es_del_rol(monkeypatch):
    from app import roles
    from app.profile import BusinessProfile
    from app.prompt import build_system_prompt
    from app.state import Conversation

    base = roles.ROLES["calificador"].enfoques[roles.CALIFICAR]
    propio = roles.Enfoque(
        mision=base.mision, primer_mensaje=base.primer_mensaje, lead_listo=base.lead_listo,
        primer_contacto="saluda y pregunta qué producto busca",
    )
    monkeypatch.setitem(roles.ROLES, "vendedor", roles.RolSpec(clave="vendedor", enfoques={roles.CALIFICAR: propio}))
    conv = Conversation(id=1, wa_identity="1")
    texto = build_system_prompt(profile=BusinessProfile(role="vendedor"), context=None, conv=conv)
    assert "- Es el PRIMER contacto: saluda y pregunta qué producto busca." in texto
    defecto = build_system_prompt(profile=BusinessProfile(), context=None, conv=conv)
    assert "- Es el PRIMER contacto: saluda transparente, gancho + UNA pregunta." in defecto


# ──────────────────────────────────────────────────────────── B-1 ───


@pytest.mark.parametrize("concurrencia,esperado", [(1, 32), (8, 32), (12, 32), (20, 48), (40, 88)])
def test_b1_el_pool_http_crece_con_la_concurrencia(concurrencia, esperado):
    from app.multiorg import limites_http

    assert limites_http(concurrencia).max_connections == esperado


# ──────────────────────────────────────────────────────────── B-2 ───


class _Transaccion:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Conn:
    def __init__(self, candados: list[bool], borrados: list[int], falla_unlock: bool = False) -> None:
        self.candados, self.borrados, self.falla_unlock = candados, borrados, falla_unlock
        self.sql: list[tuple] = []

    def transaction(self):
        return _Transaccion()

    async def fetchval(self, sql: str, *args: Any):
        self.sql.append((sql, args))
        return self.candados.pop(0)

    async def execute(self, sql: str, *args: Any):
        self.sql.append((sql, args))
        if "pg_advisory_unlock" in sql and self.falla_unlock:
            raise ConnectionError("conexión caída")
        if sql.lstrip().startswith("DELETE"):
            return f"DELETE {self.borrados.pop(0)}"
        return "SELECT 1"


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    @asynccontextmanager
    async def _acq(self):
        yield self.conn

    def acquire(self):
        return self._acq()


def _store(conn: _Conn, esquema: str = ""):
    from app.db import PgStore

    s = PgStore("postgresql://x", schema=esquema) if esquema else PgStore("postgresql://x")
    s._pool = _Pool(conn)  # type: ignore[assignment]
    return s


async def test_b2_la_purga_informa_lo_borrado_si_otra_replica_toma_el_candado():
    conn = _Conn(candados=[True, False], borrados=[1000])
    resultado = await _store(conn).purgar(14, 30, lote=1000)
    assert resultado == {"dispatch_inbox": 1000, "processed_message": 0, "pending_send": 0}


async def test_b2_sin_candado_desde_el_principio_es_none():
    assert await _store(_Conn(candados=[False], borrados=[])).purgar(14, 30) is None


async def test_b2_las_llaves_llevan_el_esquema(tmp_path: Path):
    (tmp_path / "001.sql").write_text("SELECT 1;", encoding="utf-8")
    conn = _Conn(candados=[], borrados=[])
    await _store(conn, "nea_b").migrate(tmp_path)
    llaves = [args for sql, args in conn.sql if "advisory" in sql]
    assert llaves and all(a == ("nea:migrate:nea_b",) for a in llaves)
    assert _store(_Conn([], []))._llave("purga") == "nea:purga:public"


async def test_b2_soltar_el_candado_con_la_conexion_caida_no_revienta(tmp_path: Path):
    (tmp_path / "001.sql").write_text("SELECT 1;", encoding="utf-8")
    await _store(_Conn([], [], falla_unlock=True)).migrate(tmp_path)


def test_b2_la_antiguedad_nunca_es_negativa():
    import inspect

    from app.dispatch_store import PgDispatchStore

    assert "GREATEST(0," in inspect.getsource(PgDispatchStore.queue_metrics)


# ──────────────────────────────────────────────────────────── B-3 ───


def test_b3_los_cupos_viven_en_el_contexto_de_la_app():
    from app import ensayo

    uno, otro = make_ctx(), make_ctx()
    assert ensayo._cupos(uno) is ensayo._cupos(uno)
    assert ensayo._cupos(uno) is not ensayo._cupos(otro)
    assert uno.ensayo_cupos is ensayo._cupos(uno)
    assert not hasattr(ensayo, "_CUPOS")


async def test_b3_si_cerrar_el_crm_falla_el_llm_se_cierra_igual(monkeypatch):
    import respx

    from app import ensayo
    from tests.test_ensayo import CRM, _completion, _ctx, _payload, _post

    cerrados: list[str] = []

    async def aclose_que_falla(self) -> None:
        raise RuntimeError("socket roto")

    monkeypatch.setattr(ensayo.CrmDeEnsayo, "aclose", aclose_que_falla)
    from app.multiorg import RegistroDeOrganizaciones

    original = RegistroDeOrganizaciones.llm_de_ensayo

    def espia(self, *a: Any, **k: Any):
        llm = original(self, *a, **k)
        cerrar = llm._client.close

        async def close() -> None:
            cerrados.append("llm")
            await cerrar()

        llm._client.close = close
        return llm

    monkeypatch.setattr(RegistroDeOrganizaciones, "llm_de_ensayo", espia)
    ctx = _ctx()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as crm:
        crm.post(f"{CRM}/api/brains/llm/chat/completions").mock(return_value=_completion("¡Hola!"))
        try:
            await _post(ctx, _payload(["hola"]))
        except Exception:
            pass
        finally:
            await ctx.registro.aclose()
    assert cerrados == ["llm"]
