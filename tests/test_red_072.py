"""vocero-crm 072 (A0) — Red de seguridad antes del Motor Multi-Nicho.

Sin `fichaSchema` (o con uno vacío o nulo) el catálogo de herramientas y el
prompt son los de la 071, byte a byte. Los dorados B0 viven en
`tests/test_chasis_dorado.py`; aquí se comprueba que las formas «vacías» del
contexto nuevo no los mueven.
"""
from __future__ import annotations

import copy

import pytest

from app import tools as t
from app.herramientas.builtin import REGISTRO, esquema_de_ficha
from app.herramientas.registro import Capacidades
from app.prompt import build_system_prompt
from tests.test_chasis_dorado import AHORA, BOGOTA, CASOS

UPDATE_FICHA_071 = next(s for s in t.TOOL_SCHEMAS if s["function"]["name"] == "update_ficha")

VACIOS = [None, {}, {"fichaSchema": None}, {"fichaSchema": {"version": 1, "fields": []}}, {"fichaSchema": "basura"}]


@pytest.mark.parametrize("contexto", VACIOS)
def test_sin_esquema_update_ficha_es_el_de_siempre(contexto) -> None:
    assert esquema_de_ficha(contexto) == UPDATE_FICHA_071
    assert REGISTRO.get("update_ficha").esquema(Capacidades(contexto=contexto)) == UPDATE_FICHA_071


@pytest.mark.parametrize("contexto", VACIOS[1:])
def test_el_catalogo_completo_no_cambia(contexto) -> None:
    assert t.tool_schemas(True, context=contexto) == t.tool_schemas(True, context=None)


@pytest.mark.parametrize("nombre", sorted(CASOS))
@pytest.mark.parametrize("vacio", [None, {"version": 1, "fields": []}])
def test_los_dorados_b0_no_se_mueven_con_un_esquema_vacio(nombre: str, vacio) -> None:
    caso = dict(CASOS[nombre])
    base = build_system_prompt(now=AHORA, tz=BOGOTA, **caso)
    contexto = copy.deepcopy(caso.get("context")) or {}
    contexto["fichaSchema"] = vacio
    caso["context"] = contexto
    if CASOS[nombre].get("context") is None:
        # `single_minimo` no tiene contexto: un dict vacío ya es otra entrada.
        caso["context"] = {"fichaSchema": vacio}
        base = build_system_prompt(now=AHORA, tz=BOGOTA, **dict(CASOS[nombre], context={}))
    assert build_system_prompt(now=AHORA, tz=BOGOTA, **caso) == base
