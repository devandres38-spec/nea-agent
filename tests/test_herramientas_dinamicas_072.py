"""vocero-crm 072-B (B3, D15) — Herramientas de módulo del CRM en Nea.

- `validar`: colisión con nativa o reservada, patrón, prefijo, tamaños,
  más de 8, duplicadas y basura; cada descarte deja un `warning`.
- Registro POR TURNO: sin herramientas es el mismo `REGISTRO`; con ellas, uno
  nuevo, sin mutar el global ni filtrarse entre dos negocios concurrentes.
- `clave_de` de un nombre desconocido ya no vale «escalar».
- `BrainsCrmClient.call_tool`: mapeo de 200/404/409/413/422/5xx/504/red, y
  401/403 como `CrmRechazo` terminal (también a través de `execute`).
- Prompt: una línea en CONTEXTO ACTUAL solo con herramientas (dorado nuevo
  `072b_cloud_con_herramientas.txt`; los B0 no cambian).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx
import pytest

from app.crm import CrmClient, CrmRechazo
from app.crm_brains import NOTA_EN_CURSO, NOTA_ENVIADO, TIMEOUT_HERRAMIENTA_S, BrainsCrmClient
from app.ensayo import CrmDeEnsayo
from app.herramientas.builtin import REGISTRO
from app.herramientas.dinamicas import (
    RESERVADOS,
    TOPE_HERRAMIENTAS,
    registro_del_turno,
    sabe_llamar_herramientas,
    tiene_dinamicas,
    validar,
)
from app.llm import LlmReply, ToolCall
from app.multiorg import CrmSinOrganizacion
from app.profile import BusinessProfile
from app.prompt import LINEA_HERRAMIENTAS_PROPIAS, build_system_prompt
from app.tools import ToolRuntime, solo_encendidas, tool_schemas
from app.turn import _tool_loop
from tests.conftest import CRM_URL, FakeLLM, IDENTITY, make_ctx
from tests.test_chasis_dorado import AHORA, BOGOTA, CASOS, DORADOS

NATIVAS = list(REGISTRO.nombres())


def _tool(name: str = "diagnostico_eco", module: str = "diagnostico", **cambios: Any) -> dict[str, Any]:
    return {
        "name": name,
        "module": module,
        "effect": "lectura",
        "description": "Devuelve el texto recibido. Solo para pruebas internas.",
        "parameters": {
            "type": "object",
            "properties": {"texto": {"type": "string", "maxLength": 200}},
            "required": ["texto"],
            "additionalProperties": False,
        },
        **cambios,
    }


def _ctx(*tools: dict[str, Any]) -> dict[str, Any]:
    return {"contact": {"name": "Ana", "ficha": {}}, "moduleTools": list(tools)}


def _nombres(specs) -> list[str]:
    return [s.nombre for s in specs]


# ───────────────────────────────────────────────────────── validar ───


def test_las_validas_entran_con_clave_modulo():
    specs = validar(_ctx(_tool(), _tool("diagnostico_marca", effect="escritura")))
    assert _nombres(specs) == ["diagnostico_eco", "diagnostico_marca"]
    assert {s.clave for s in specs} == {"modulo:diagnostico"}
    esquema = specs[0].esquema(None)
    assert esquema == {
        "type": "function",
        "function": {
            "name": "diagnostico_eco",
            "description": "Devuelve el texto recibido. Solo para pruebas internas.",
            "parameters": _tool()["parameters"],
        },
    }


@pytest.mark.parametrize(
    "item",
    [
        _tool("update_ficha", "update"),  # nativa con «prefijo» válido
        _tool("route_out", "route"),  # reservada
        _tool("move_stage", "move"),
        _tool("otro_eco"),  # sin el prefijo de SU módulo
        _tool("Diagnostico_eco"),  # patrón
        _tool("diagnostico_eco", "Diag"),  # módulo inválido
        _tool("di"),  # corto
        _tool(description=""),
        _tool(description="x" * 401),
        _tool(parameters={"type": "array"}),
        _tool(parameters="no"),
        _tool(parameters={"type": "object", "description": "x" * 5000}),  # >4 KB
        "no es un objeto",
        None,
    ],
)
def test_lo_invalido_se_descarta_con_warning_y_lo_demas_sigue(item, caplog):
    with caplog.at_level(logging.WARNING, logger="nea.herramientas.dinamicas"):
        specs = validar(_ctx(item, _tool("diagnostico_marca")))
    assert _nombres(specs) == ["diagnostico_marca"]
    assert "descartada" in caplog.text


def test_todas_las_nativas_y_reservadas_estan_vetadas():
    assert {"notas", *NATIVAS} <= RESERVADOS | set(NATIVAS)
    for nombre in NATIVAS:
        modulo = nombre.split("_")[0]
        if len(modulo) >= 3:
            assert validar(_ctx(_tool(nombre, modulo))) == [], nombre


def test_duplicadas_mas_de_8_y_total_de_12_kb():
    assert _nombres(validar(_ctx(_tool(), _tool()))) == ["diagnostico_eco"]
    muchas = [_tool(f"diagnostico_h{i}") for i in range(12)]
    assert len(validar(_ctx(*muchas))) == TOPE_HERRAMIENTAS
    pesada = {"type": "object", "description": "x" * 3500}
    grandes = [_tool(f"diagnostico_g{i}", parameters=pesada) for i in range(5)]
    # 3 caben en 12 KB; la 4.ª y la 5.ª no.
    assert len(validar(_ctx(*grandes))) == 3


def test_moduletools_que_no_es_lista_no_tumba_nada(caplog):
    with caplog.at_level(logging.WARNING, logger="nea.herramientas.dinamicas"):
        assert validar({"moduleTools": {"name": "x"}}) == []
    assert validar({}) == []
    assert validar(None) == []


# ──────────────────────────────────────────────── registro por turno ───


def test_sin_herramientas_el_registro_del_turno_ES_el_global():
    assert registro_del_turno({}, puede_ejecutar=True) is REGISTRO
    assert registro_del_turno(_ctx(), puede_ejecutar=True) is REGISTRO
    assert registro_del_turno(_ctx(_tool("update_ficha", "update")), puede_ejecutar=True) is REGISTRO
    # Un CRM que no ejecuta herramientas (single, ensayo): tampoco se ofrecen.
    assert registro_del_turno(_ctx(_tool()), puede_ejecutar=False) is REGISTRO


def test_con_herramientas_es_uno_nuevo_y_el_global_no_cambia():
    reg = registro_del_turno(_ctx(_tool()), puede_ejecutar=True)
    assert reg is not REGISTRO
    assert reg.nombres() == [*NATIVAS, "diagnostico_eco"]
    assert REGISTRO.nombres() == NATIVAS
    assert REGISTRO.get("diagnostico_eco") is None
    assert tiene_dinamicas(reg) and not tiene_dinamicas(REGISTRO)


def test_quien_sabe_llamar_herramientas():
    assert sabe_llamar_herramientas(BrainsCrmClient(CRM_URL, "s", "org_1"))
    assert not sabe_llamar_herramientas(CrmClient(CRM_URL, "k"))
    # Estos dos revientan en `__getattr__` a propósito: se pregunta a la clase.
    assert not sabe_llamar_herramientas(CrmSinOrganizacion())
    assert not sabe_llamar_herramientas(CrmDeEnsayo({}, BrainsCrmClient(CRM_URL, "s", "o"), []))


def test_clave_de_un_nombre_desconocido_ya_no_es_escalar():
    assert REGISTRO.clave_de("inventada") is None
    assert REGISTRO.clave_de("handoff") == "escalar"
    falso = {"type": "function", "function": {"name": "inventada", "parameters": {"type": "object"}}}
    perfil = BusinessProfile(tools=frozenset({"escalar"}))
    assert solo_encendidas([falso, *tool_schemas(False)], perfil) == [
        s for s in tool_schemas(False) if s["function"]["name"] == "handoff"
    ]


def test_las_de_modulo_no_dependen_del_catalogo_del_dueno():
    reg = registro_del_turno(_ctx(_tool()), puede_ejecutar=True)
    perfil = BusinessProfile(tools=frozenset({"escalar"}))  # sin ficha, sin agenda
    ofrecidas = solo_encendidas(tool_schemas(False, registro=reg), perfil, reg)
    assert [s["function"]["name"] for s in ofrecidas] == ["handoff", "diagnostico_eco"]
    # Con el registro global, la de módulo no existe para el filtro.
    assert "diagnostico_eco" not in [
        s["function"]["name"] for s in solo_encendidas(tool_schemas(False, registro=reg), perfil)
    ]


# ─────────────────────────────────────────── ejecución y aislamiento ───


class CrmFalso(BrainsCrmClient):
    """Un `BrainsCrmClient` que anota las llamadas en vez de salir a la red."""

    def __init__(self, org: str, respuesta: Any = None) -> None:
        super().__init__(CRM_URL, "s", org)
        self.org = org
        self.llamadas: list[tuple[str, str, dict[str, Any]]] = []
        self.respuesta = respuesta

    async def call_tool(self, conversation_id, name, args):  # type: ignore[override]
        if isinstance(self.respuesta, Exception):
            raise self.respuesta
        self.llamadas.append((conversation_id, name, args))
        await asyncio.sleep(0)
        return self.respuesta or {"ok": True, "result": {"org": self.org, "eco": args.get("texto")}, "estado": "hecho"}


async def _runtime(org: str, context: dict[str, Any], respuesta: Any = None) -> tuple[ToolRuntime, CrmFalso]:
    ctx = make_ctx()
    crm = CrmFalso(org, respuesta)
    ctx.crm = crm
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    return ToolRuntime(ctx, conv, f"cv_{org}", context=context), crm


async def test_execute_lleva_la_llamada_al_crm_del_turno():
    rt, crm = await _runtime("org_A", _ctx(_tool()))
    r = await rt.execute("diagnostico_eco", {"texto": "hola"})
    assert r == {"ok": True, "result": {"org": "org_A", "eco": "hola"}, "estado": "hecho"}
    assert crm.llamadas == [("cv_org_A", "diagnostico_eco", {"texto": "hola"})]


async def test_dos_negocios_a_la_vez_no_se_mezclan():
    async def turno(org: str, tools: list[dict[str, Any]]):
        rt, crm = await _runtime(org, _ctx(*tools))
        await asyncio.sleep(0)
        ofrecidas = [s["function"]["name"] for s in tool_schemas(False, registro=rt.registro)]
        r = await rt.execute("diagnostico_eco", {"texto": org})
        return ofrecidas, r, crm.llamadas

    (of_a, r_a, ll_a), (of_b, r_b, ll_b) = await asyncio.gather(turno("org_A", [_tool()]), turno("org_B", []))
    assert "diagnostico_eco" in of_a
    assert "diagnostico_eco" not in of_b
    assert r_a["result"]["org"] == "org_A"
    # B no la tiene: ni se le ofrece ni se ejecuta, ni sale nada a su CRM.
    assert r_b["ok"] is False and ll_b == []
    assert ll_a == [("cv_org_A", "diagnostico_eco", {"texto": "org_A"})]
    assert REGISTRO.nombres() == NATIVAS


async def test_crm_rechazo_sube_por_execute_y_no_llega_al_modelo():
    rt, _ = await _runtime("org_A", _ctx(_tool()), respuesta=CrmRechazo(401))
    with pytest.raises(CrmRechazo):
        await rt.execute("diagnostico_eco", {"texto": "x"})


async def test_crm_rechazo_tambien_en_una_nativa(monkeypatch):
    rt, crm = await _runtime("org_A", {"contact": {"ficha": {}}})

    async def rechazo(*_a, **_k):
        raise CrmRechazo(403, "organizacion_suspendida")

    monkeypatch.setattr(crm, "put_ficha", rechazo)
    with pytest.raises(CrmRechazo):
        await rt.execute("update_ficha", {"nombre": "Ana"})


async def test_el_turno_ofrece_y_ejecuta_la_herramienta_de_modulo():
    rt, crm = await _runtime("org_A", _ctx(_tool()))
    llm = FakeLLM(
        [
            LlmReply(content=None, tool_calls=[ToolCall(id="t1", name="diagnostico_eco", arguments={"texto": "hola"})]),
            LlmReply(content="Listo."),
        ]
    )
    rt._ctx.llm = llm
    texto = await _tool_loop(rt._ctx, [{"role": "user", "content": "prueba"}], rt)
    assert texto == "Listo."
    ofrecidas = [s["function"]["name"] for s in llm.calls[0]["tools"]]
    assert "diagnostico_eco" in ofrecidas
    resultado = llm.calls[1]["messages"][-1]
    assert resultado["role"] == "tool" and json.loads(resultado["content"])["result"]["eco"] == "hola"
    assert crm.llamadas == [("cv_org_A", "diagnostico_eco", {"texto": "hola"})]


# ─────────────────────────────────────────────────────── call_tool ───


def _cliente() -> BrainsCrmClient:
    c = BrainsCrmClient(CRM_URL, "secreto", "org_1")
    c.registrar_despacho("cv_1", "dsp_9")
    return c


async def test_call_tool_200_devuelve_el_cuerpo_y_manda_el_despacho(respx_mock):
    ruta = respx_mock.post(f"{CRM_URL}/api/brains/tools/diagnostico_eco").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"eco": "x"}, "estado": "hecho"})
    )
    c = _cliente()
    assert await c.call_tool("cv_1", "diagnostico_eco", {"texto": "x"}) == {"ok": True, "result": {"eco": "x"}, "estado": "hecho"}
    enviado = json.loads(ruta.calls[0].request.content)
    assert enviado == {"conversationId": "cv_1", "dispatchId": "dsp_9", "args": {"texto": "x"}}
    assert ruta.calls[0].request.headers["x-vocero-organization"] == "org_1"
    await c.aclose()


async def test_call_tool_sin_despacho_no_llama_al_crm(respx_mock):
    """Auditoría 072-B (A-1): el CRM exige un despacho real; sin uno, ni se intenta."""
    ruta = respx_mock.post(f"{CRM_URL}/api/brains/tools/diagnostico_eco").mock(return_value=httpx.Response(200, json={"ok": True}))
    c = BrainsCrmClient(CRM_URL, "secreto", "org_1")
    r = await c.call_tool("cv_1", "diagnostico_eco", {})
    assert r["ok"] is False and r["error"] == "herramienta_no_disponible"
    assert not ruta.called
    await c.aclose()


@pytest.mark.parametrize(
    "status,codigo,esperado",
    [
        (422, "despacho_desconocido", {"ok": False, "error": "herramienta_no_disponible", "detalle": "despacho_desconocido"}),
        (409, "cerebro_reasignado", {"ok": False, "error": "cerebro_reasignado"}),
        (409, "ai_paused", {"ok": False, "error": "ai_paused"}),
    ],
)
async def test_call_tool_mapea_los_codigos_del_despacho(respx_mock, status, codigo, esperado):
    respx_mock.post(f"{CRM_URL}/api/brains/tools/diagnostico_eco").mock(
        return_value=httpx.Response(status, json={"error": {"code": codigo, "message": "m"}})
    )
    c = _cliente()
    assert await c.call_tool("cv_1", "diagnostico_eco", {}) == esperado
    await c.aclose()


async def test_m3_un_200_de_mas_de_4_kb_no_llega_al_modelo(respx_mock, caplog):
    respx_mock.post(f"{CRM_URL}/api/brains/tools/diagnostico_eco").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"x": "y" * 5000}, "estado": "hecho"})
    )
    c = _cliente()
    with caplog.at_level(logging.WARNING, logger="nea.crm.brains"):
        r = await c.call_tool("cv_1", "diagnostico_eco", {})
    assert r["ok"] is False and r["error"] == "resultado_grande"
    assert "no repitas" in r["detalle"]
    assert "bytes" in caplog.text
    await c.aclose()


@pytest.mark.parametrize(
    "cuerpo,nota",
    [
        ({"ok": True, "result": {}, "estado": "en_curso"}, NOTA_EN_CURSO),
        ({"ok": True, "result": {"enviadoAlCliente": True}, "estado": "hecho"}, NOTA_ENVIADO),
        ({"ok": True, "result": {"enviadoAlCliente": True}, "estado": "en_curso"}, f"{NOTA_EN_CURSO}; {NOTA_ENVIADO}"),
    ],
)
async def test_m3_guia_para_en_curso_y_enviado_al_cliente(respx_mock, cuerpo, nota):
    respx_mock.post(f"{CRM_URL}/api/brains/tools/diagnostico_eco").mock(return_value=httpx.Response(200, json=cuerpo))
    c = _cliente()
    assert await c.call_tool("cv_1", "diagnostico_eco", {}) == {**cuerpo, "nota": nota}
    await c.aclose()


async def test_m3_un_resultado_normal_no_lleva_nota(respx_mock):
    cuerpo = {"ok": True, "result": {"enviadoAlCliente": False}, "estado": "hecho"}
    respx_mock.post(f"{CRM_URL}/api/brains/tools/diagnostico_eco").mock(return_value=httpx.Response(200, json=cuerpo))
    c = _cliente()
    assert await c.call_tool("cv_1", "diagnostico_eco", {}) == cuerpo
    await c.aclose()


@pytest.mark.parametrize(
    "status,cuerpo,esperado",
    [
        (404, {"error": {"code": "tool_not_found"}}, {"ok": False, "error": "herramienta_no_disponible"}),
        (409, {"error": {"code": "ai_paused"}}, {"ok": False, "error": "ai_paused"}),
        (
            422,
            {"error": {"code": "invalid_args", "message": "m", "details": {"path": ["texto"]}}},
            {"ok": False, "error": "argumentos_invalidos", "detalle": {"path": ["texto"]}},
        ),
        (413, {"error": {"code": "payload_too_large", "message": "grande"}}, {"ok": False, "error": "argumentos_invalidos", "detalle": "grande"}),
        (504, {"error": {"code": "tool_timeout"}}, None),
        (500, {"error": {"code": "internal"}}, None),
    ],
)
async def test_call_tool_mapea_los_errores_del_contrato(respx_mock, status, cuerpo, esperado):
    respx_mock.post(f"{CRM_URL}/api/brains/tools/diagnostico_eco").mock(return_value=httpx.Response(status, json=cuerpo))
    c = _cliente()
    r = await c.call_tool("cv_1", "diagnostico_eco", {})
    if esperado is None:
        assert r["ok"] is False and r["error"] == "crm_error"
    else:
        assert r == esperado
    await c.aclose()


async def test_call_tool_red_caida_o_10_s_es_crm_error(respx_mock):
    respx_mock.post(f"{CRM_URL}/api/brains/tools/diagnostico_eco").mock(side_effect=httpx.ReadTimeout("lento"))
    c = _cliente()
    r = await c.call_tool("cv_1", "diagnostico_eco", {})
    assert r["error"] == "crm_error"
    assert TIMEOUT_HERRAMIENTA_S == 10.0
    await c.aclose()


async def test_call_tool_pide_10_s_de_espera(monkeypatch):
    c = _cliente()
    vistos: dict[str, Any] = {}

    async def request(method, url, **kwargs):
        vistos.update(kwargs, url=url)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, f"{CRM_URL}{url}"))

    monkeypatch.setattr(c._http, "request", request)
    await c.call_tool("cv_1", "diagnostico_eco", {})
    assert vistos["timeout"] == 10.0 and vistos["url"] == "/api/brains/tools/diagnostico_eco"
    await c.aclose()


@pytest.mark.parametrize("status", [401, 403])
async def test_call_tool_401_403_es_crm_rechazo_terminal(respx_mock, status):
    respx_mock.post(f"{CRM_URL}/api/brains/tools/diagnostico_eco").mock(
        return_value=httpx.Response(status, json={"error": {"code": "organizacion_suspendida"}})
    )
    c = _cliente()
    with pytest.raises(CrmRechazo):
        await c.call_tool("cv_1", "diagnostico_eco", {})
    await c.aclose()


# ─────────────────────────────────────────────────────────── prompt ───


def test_la_linea_solo_aparece_con_herramientas_propias():
    sin = build_system_prompt(now=AHORA, tz=BOGOTA, **CASOS["cloud_agenda_v2"])
    con = build_system_prompt(now=AHORA, tz=BOGOTA, herramientas_propias=True, **CASOS["cloud_agenda_v2"])
    assert LINEA_HERRAMIENTAS_PROPIAS not in sin
    assert con.replace(LINEA_HERRAMIENTAS_PROPIAS + "\n", "") == sin
    assert "nunca las nombres al cliente" in LINEA_HERRAMIENTAS_PROPIAS


def test_dorado_cloud_con_herramientas():
    """Dorado propio de la 072-B (regenerar solo con un cambio aprobado en spec)."""
    prompt = build_system_prompt(now=AHORA, tz=BOGOTA, herramientas_propias=True, **CASOS["cloud_agenda_v2"])
    archivo = DORADOS / "072b_cloud_con_herramientas.txt"
    if os.environ.get("NEA_ACTUALIZAR_DORADOS") == "1":
        archivo.write_bytes(prompt.encode("utf-8"))
    assert prompt == archivo.read_bytes().decode("utf-8")


def test_el_perfil_por_defecto_no_cambia_por_las_herramientas():
    # Sanidad: los casos B0 no traen `moduleTools`, así que su turno usa REGISTRO.
    for caso in CASOS.values():
        assert registro_del_turno(caso.get("context"), puede_ejecutar=True) is REGISTRO
