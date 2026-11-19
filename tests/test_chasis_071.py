"""vocero-crm 071 (D14, D20) — Chasis por secciones, roles y registro de herramientas.

El texto del prompt lo congelan las doradas (`test_chasis_dorado.py`); aquí se
prueba la forma: que un rol sea una entrada de datos, que una clave desconocida
caiga al Calificador, que las herramientas se despachen por registro y que el
contexto de la 072 (claves nuevas) no cambie nada.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import roles
from app.chasis import armar_chasis
from app.herramientas.builtin import REGISTRO, esquema_de_ficha
from app.herramientas.registro import Capacidades, Registro, ToolSpec
from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.roles import ATENDER_Y_AGENDAR, CALIFICAR, Enfoque, RolSpec, rol_de
from app.state import Conversation
from app.tools import CLAVE_DE_HERRAMIENTA, ToolRuntime, solo_encendidas, tool_schemas
from tests.conftest import make_ctx

AHORA = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
BOGOTA = ZoneInfo("America/Bogota")


def _nombres(schemas):
    return [t["function"]["name"] for t in schemas]


# ───────────────────────────────────────────────────────────── roles ───


def test_el_registro_de_roles_usa_la_clave_del_crm():
    assert set(roles.ROLES) == {"calificador"}
    assert rol_de("calificador").clave == "calificador"


@pytest.mark.parametrize("clave", ["vendedor", "", None, 3, "Calificador"])
def test_una_clave_desconocida_cae_al_calificador(clave):
    assert rol_de(clave).clave == "calificador"


def test_un_rol_desconocido_arma_el_mismo_chasis_que_el_calificador():
    base = BusinessProfile(agent_name="Sofi")
    otro = BusinessProfile(agent_name="Sofi", role="rol_de_la_072")
    a_quien = "escriben al número del negocio"
    assert armar_chasis(otro, "WhatsApp", a_quien) == armar_chasis(base, "WhatsApp", a_quien)


def test_el_enfoque_lo_decide_la_superficie():
    assert roles.enfoque_de(BusinessProfile()) == CALIFICAR
    assert roles.enfoque_de(BusinessProfile(cloud=True)) == ATENDER_Y_AGENDAR


def test_un_rol_nuevo_es_una_entrada(monkeypatch):
    """Añadir un rol no toca el turno ni el prompt: solo `ROLES`."""
    enfoque = Enfoque(
        mision="orientar a cada persona hacia el producto que le sirve",
        primer_mensaje="saluda y pregunta qué busca",
        lead_listo="si ya sabe qué quiere, ve directo a cerrar",
    )
    monkeypatch.setitem(
        roles.ROLES,
        "orientador",
        RolSpec(clave="orientador", enfoques={CALIFICAR: enfoque}, herramientas=frozenset({"ficha"})),
    )
    perfil = BusinessProfile(agent_name="Sofi", role="orientador")
    texto = armar_chasis(perfil, "WhatsApp", "escriben al número del negocio")
    assert "orientar a cada persona hacia el producto que le sirve" in texto
    assert "calificarla según las instrucciones del negocio" not in texto
    # Herramientas efectivas = rol ∩ perfil ∩ capacidades (D20).
    assert _nombres(solo_encendidas(tool_schemas(True), perfil)) == ["update_ficha", "handoff"]


# ─────────────────────────────────── línea de cancelar (capacidad, D14) ───


def test_la_linea_de_cancelar_depende_de_lo_que_el_crm_sabe_hacer():
    perfil = BusinessProfile(agent_name="Sofi")
    a_quien = "escriben al número del negocio"
    sin_v2 = armar_chasis(perfil, "WhatsApp", a_quien)
    con_v2 = armar_chasis(perfil, "WhatsApp", a_quien, agenda_v2=True)
    assert "→ Si quiere CANCELAR: handoff — esa la decide el equipo." in sin_v2
    assert "usa list_bookings y cancel_session" in con_v2
    # Y el enfoque sigue siendo el de calificar: solo cambia la capacidad.
    assert "calificarla según las instrucciones del negocio" in con_v2


# ───────────────────────────────────────── registro de herramientas ───


def test_el_catalogo_sale_del_registro_en_el_orden_de_siempre():
    assert _nombres(tool_schemas(True)) == [
        "update_ficha", "propose_slots", "book_session", "reschedule_session", "route_out", "handoff",
    ]
    assert _nombres(tool_schemas(False)) == ["update_ficha", "route_out", "handoff"]
    assert _nombres(tool_schemas(True, agenda_v2=True, coordination=True))[-3:] == [
        "list_bookings", "cancel_session", "coordination_consent",
    ]


def test_cada_toolspec_tiene_la_clave_del_catalogo_del_crm():
    for spec in REGISTRO:
        assert CLAVE_DE_HERRAMIENTA[spec.nombre] == spec.clave
    assert set(REGISTRO.nombres()) == set(CLAVE_DE_HERRAMIENTA)


def test_no_se_registra_dos_veces_el_mismo_nombre():
    async def nada(rt, args):
        return {}

    spec = ToolSpec("x", "ficha", lambda caps: {}, nada)
    with pytest.raises(ValueError):
        Registro([spec, spec])


async def test_execute_despacha_por_registro_y_lo_desconocido_no_existe():
    ctx = make_ctx()
    rt = ToolRuntime(ctx, Conversation(id=1, wa_identity="1"), "cv_1")
    assert (await rt.execute("handoff", {"reason": "pidió humano"}))["ok"] is True
    assert rt.handoff_reason == "pidió humano"
    assert (await rt.execute("volar", {})) == {"ok": False, "error": "herramienta desconocida: volar"}
    # Sin agenda v2 en el CRM, cancelar no existe aunque el modelo la pida.
    assert (await rt.execute("cancel_session", {}))["error"] == "herramienta desconocida: cancel_session"
    await ctx.crm.aclose()


def test_esquema_de_ficha_es_el_punto_de_enchufe_de_la_072():
    esquema = esquema_de_ficha({"contact": {"customFields": {"talla": "M"}}})
    assert esquema["function"]["name"] == "update_ficha"
    assert "rubro" in esquema["function"]["parameters"]["properties"]
    caps = Capacidades(contexto={"tools": [{"name": "cotizar"}]})
    assert REGISTRO.get("update_ficha").esquema(caps) == esquema


# ─────────────────────────────── el contexto de la 072 no cambia nada ───


def test_claves_desconocidas_del_contexto_se_ignoran():
    base = {"contact": {"name": "Gerardo"}, "conversation": {"id": "cv_1"}}
    con_072 = {
        **base,
        "contact": {"name": "Gerardo", "customFields": {"talla": "M"}},
        "tools": [{"name": "cotizar", "description": "x", "parameters": {}, "clave": "cotizar"}],
        "algoNuevo": {"x": 1},
    }
    perfil = BusinessProfile(agent_name="Sofi")
    conv = Conversation(id=1, wa_identity="573001112233")
    uno = build_system_prompt(profile=perfil, context=base, conv=conv, now=AHORA, tz=BOGOTA)
    otro = build_system_prompt(profile=perfil, context=con_072, conv=conv, now=AHORA, tz=BOGOTA)
    assert uno == otro
    assert _nombres(tool_schemas(True, context=con_072)) == _nombres(tool_schemas(True))
