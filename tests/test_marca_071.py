"""vocero-crm 071 (D13, T054) — Marca blanca: nada visible dice «Nea» ni «Vocero».

Visible = lo que lee el modelo (y por tanto puede repetirle al cliente) y lo
que responde una ruta HTTP. Los logs, los docstrings, los comentarios y los
identificadores del contrato (`/vocero/dispatch`, `x-vocero-signature`,
`X-Vocero-Organization`, `vocero:cerebro:v1`) se quedan: no los ve nadie de
fuera, o romperlos rompería la conexión con el CRM.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re

import httpx
import pytest

from app.main import create_app
from tests.conftest import make_ctx

MARCA = re.compile(r"\b(Nea|Vocero|vocero|NEA)\b")
# Identificadores del contrato con el CRM: no son marca visible.
CONTRATO = re.compile(
    r"/vocero/|x-vocero-|X-Vocero-|vocero:cerebro|VOCERO_|vocero-crm|vocero-cloud|nea:|nea\."
)

MODULOS = [
    "app.prompt",
    "app.chasis.secciones",
    "app.chasis.armar",
    "app.roles",
    "app.media",
    "app.tools",
    "app.herramientas.builtin",
    "app.turn",
    "app.confirmacion",
    "app.webhook_retirado",
    "app.formato",
    "app.stall",
    "app.hostility",
    "app.ensayo",
]


def _cadenas_visibles(modulo: str) -> list[tuple[int, str]]:
    arbol = ast.parse(inspect.getsource(importlib.import_module(modulo)))
    excluidas: set[int] = set()
    for nodo in ast.walk(arbol):
        # Docstrings.
        if isinstance(nodo, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if nodo.body and isinstance(nodo.body[0], ast.Expr) and isinstance(nodo.body[0].value, ast.Constant):
                excluidas.add(id(nodo.body[0].value))
        # Argumentos de logger.*(…) y getLogger(…).
        if isinstance(nodo, ast.Call):
            f = nodo.func
            nombre = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            dueño = getattr(getattr(f, "value", None), "id", "")
            if dueño == "logger" or nombre == "getLogger":
                for sub in ast.walk(nodo):
                    excluidas.add(id(sub))
    # La constante AVISO de webhook_retirado es un mensaje de log (va a logger.error).
    out = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Assign) and any(getattr(t, "id", "") == "AVISO" for t in nodo.targets):
            for sub in ast.walk(nodo):
                excluidas.add(id(sub))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and id(nodo) not in excluidas:
            out.append((nodo.lineno, nodo.value))
    return out


@pytest.mark.parametrize("modulo", MODULOS)
def test_ninguna_cadena_visible_lleva_la_marca_vieja(modulo):
    malas = [
        (linea, texto)
        for linea, texto in _cadenas_visibles(modulo)
        if MARCA.search(CONTRATO.sub("", texto))
    ]
    assert malas == [], f"{modulo}: {malas}"


def test_el_nombre_por_defecto_es_asistente():
    from app.config import Settings
    from app.profile import BusinessProfile

    assert Settings.model_fields["agent_name"].default == "Asistente"
    assert BusinessProfile().agent_name == "Asistente"


def test_los_marcadores_multimedia_no_tienen_genero():
    import app.media as media

    fuente = inspect.getsource(media)
    assert "honesta" not in fuente and "Sé honesto" not in fuente


async def _respuesta(ruta: str, metodo: str = "get") -> httpx.Response:
    ctx = make_ctx()
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot.test") as c:
        resp = await getattr(c, metodo)(ruta)
    await ctx.crm.aclose()
    return resp


@pytest.mark.parametrize(
    "ruta,metodo",
    [("/health", "get"), ("/webhook", "post"), ("/webhook/abc", "post"), ("/no-existe", "get")],
)
async def test_las_respuestas_http_no_llevan_la_marca(ruta, metodo):
    resp = await _respuesta(ruta, metodo)
    assert not MARCA.search(resp.text), resp.text


async def test_el_webhook_retirado_responde_solo_el_codigo():
    resp = await _respuesta("/webhook", "post")
    assert resp.status_code == 503
    assert resp.json() == {"error": "webhook_retirado"}


def test_el_titulo_de_la_api_es_de_aliantis():
    assert create_app(ctx=make_ctx()).title == "Aliantis — cerebro conversacional"
