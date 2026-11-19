"""Markdown → formato de WhatsApp, justo antes de enviar.

Por qué existe: el modelo escribe Markdown aunque el prompt le pida WhatsApp
(GLM sobre todo: precios en `**negritas**`, títulos con `##`, tablas, enlaces
`[texto](url)`), y WhatsApp no interpreta Markdown — el lead ve los asteriscos
dobles, los gatos y las barras tal cual. Pedírselo mejor al modelo no basta:
es justo la clase de cosa que sale bien nueve veces y la décima no.

Se arregla aquí, de forma determinista, y el texto que se guarda en el
historial es el ya convertido: si el modelo se viera a sí mismo escribiendo
Markdown, lo seguiría escribiendo.

Lo que NO se toca, a propósito:
- las URL y los correos (un `_` o un `*` dentro de un enlace es parte del
  enlace, y romperlo es peor que cualquier asterisco);
- el formato que ya es de WhatsApp: `*negrita*`, `_cursiva_`, `~tachado~`;
- las viñetas con `-` o `*` y las listas numeradas (WhatsApp las pinta);
- emojis y acentos.
"""
from __future__ import annotations

import re
from typing import Callable

# Marcadores de posición para lo protegido: caracteres de uso privado de
# Unicode, que no aparecen en un mensaje real y que ninguna regla toca.
_ABRE, _CIERRA = chr(0xE000), chr(0xE001)
_MARCA = re.compile(_ABRE + r"(\d+)" + _CIERRA)

# [texto](destino) — y ![alt](destino), que el modelo no debería mandar pero
# manda. El destino es cualquier cosa sin espacios: `https://…`, `wa.me/…`,
# `tel:…`, `mailto:…`.
_ENLACE_MD = re.compile(r"!?\[([^\]\n]*)\]\(\s*<?([^\s()<>]+)>?\s*\)")
# <https://…>, el autoenlace de Markdown.
_AUTOENLACE = re.compile(r"<((?:https?://|www\.)[^\s>]+)>", re.I)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'`" + _ABRE + _CIERRA + r"]+", re.I)
_CORREO = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Lo que suele quedar pegado al final de una URL sin ser parte de ella.
_COLA_URL = ".,;:!?¡¿*_~\"'"

_CERCO = re.compile(r"^[ \t]*```[^\n]*\n?", re.M)
_CODIGO_TRIPLE = re.compile(r"```([^`\n]+)```")
_CODIGO = re.compile(r"`([^`\n]+)`")

_SEPARADOR_TABLA = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_REGLA = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M)
_TITULO = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.M)

_NEGRITA_CURSIVA = re.compile(r"\*\*\*(?!\s)((?:(?!\*\*\*)[^\n])+?)(?<!\s)\*\*\*")
_NEGRITA = re.compile(r"\*\*(?!\s)((?:(?!\*\*)[^\n])+?)(?<!\s)\*\*")
_SUBRAYADO = re.compile(r"(?<!\w)__(?!\s)((?:(?!__)[^\n])+?)(?<!\s)__(?!\w)")
_TACHADO = re.compile(r"~~(?!\s)((?:(?!~~)[^\n])+?)(?<!\s)~~")
# Énfasis doble que envuelve SOLO un enlace: se quita (el enlace manda).
_ENFASIS_DE_ENLACE = re.compile(
    r"(\*\*\*|\*\*|__|~~)(" + _ABRE + r"\d+" + _CIERRA + r")\1"
)

_LINEAS_EN_BLANCO = re.compile(r"\n(?:[ \t]*\n){2,}")


def a_whatsapp(texto: str) -> str:
    """El texto del modelo, listo para WhatsApp. Idempotente."""
    if not texto:
        return texto
    s = texto.replace("\r\n", "\n").replace("\r", "\n")
    protegidos: list[str] = []

    def proteger(valor: str) -> str:
        protegidos.append(valor)
        return f"{_ABRE}{len(protegidos) - 1}{_CIERRA}"

    s = _ENLACE_MD.sub(lambda m: _enlace(m.group(1), m.group(2), proteger), s)
    s = _AUTOENLACE.sub(lambda m: proteger(m.group(1)), s)
    s = _URL.sub(lambda m: _url_suelta(m.group(0), proteger), s)
    s = _CORREO.sub(lambda m: proteger(m.group(0)), s)

    # Código: se va el cerco, se queda el contenido.
    s = _CODIGO_TRIPLE.sub(r"\1", s)
    s = _CERCO.sub("", s)
    s = _CODIGO.sub(r"\1", s)
    s = s.replace("`", "")

    s = _tablas(s)
    s = _REGLA.sub("", s)

    s = _ENFASIS_DE_ENLACE.sub(r"\2", s)
    s = _NEGRITA_CURSIVA.sub(lambda m: _envolver(m.group(1), "*_", "_*"), s)
    s = _NEGRITA.sub(lambda m: _envolver(m.group(1), "*", "*"), s)
    s = _SUBRAYADO.sub(lambda m: _envolver(m.group(1), "_", "_"), s)
    s = _TACHADO.sub(lambda m: _envolver(m.group(1), "~", "~"), s)
    # Dobles huérfanos: una negrita partida en dos renglones, o sin cerrar.
    s = s.replace("**", "")

    s = _TITULO.sub(lambda m: _titulo(m.group(1)), s)

    s = _LINEAS_EN_BLANCO.sub("\n\n", s).strip()
    return _MARCA.sub(lambda m: protegidos[int(m.group(1))], s)


def _enlace(texto: str, destino: str, proteger: Callable[[str], str]) -> str:
    es_telefono = destino.lower().startswith("tel:")
    destino = re.sub(r"^(?:mailto|tel):", "", destino, flags=re.I)
    texto = texto.strip().rstrip(":").strip()
    # `[55 1234 5678](tel:+525512345678)`: el número ya está a la vista.
    digitos = re.sub(r"\D", "", texto)
    if es_telefono and len(digitos) >= 7 and re.sub(r"\D", "", destino).endswith(digitos):
        return texto
    marca = proteger(destino)
    if not texto or _sin_esquema(texto) == _sin_esquema(destino):
        return marca
    return f"{texto}: {marca}"


def _sin_esquema(url: str) -> str:
    u = re.sub(r"^https?://", "", url.strip().lower())
    return re.sub(r"^www\.", "", u).rstrip("/")


def _url_suelta(url: str, proteger: Callable[[str], str]) -> str:
    """Protege una URL, dejando fuera la puntuación que se le pegó."""
    cola = ""
    while url:
        ultimo = url[-1]
        sobra = (
            ultimo in _COLA_URL
            or (ultimo == ")" and url.count(")") > url.count("("))
            or (ultimo == "]" and url.count("]") > url.count("["))
        )
        if not sobra:
            break
        cola = ultimo + cola
        url = url[:-1]
    if url.lower() in ("", "http://", "https://", "www."):
        return url + cola
    return proteger(url) + cola


def _envolver(contenido: str, abre: str, cierra: str) -> str:
    # Un enlace no se envuelve: `*https://…*` puede dejar de ser enlace.
    if _ABRE in contenido:
        return contenido
    return f"{abre}{contenido}{cierra}"


def _titulo(texto: str) -> str:
    """Un título de Markdown pasa a renglón en negrita de WhatsApp."""
    texto = texto.strip()
    if "*" in texto or _ABRE in texto:
        return texto  # ya trae su propio formato o un enlace: tal cual
    return f"*{texto}*"


def _celdas(linea: str) -> list[str]:
    linea = linea.strip()
    if linea.startswith("|"):
        linea = linea[1:]
    if linea.endswith("|"):
        linea = linea[:-1]
    return [c.strip() for c in linea.split("|")]


def _tablas(s: str) -> str:
    """Tabla de Markdown → un renglón por fila.

    Con dos columnas, `Consulta: $800`. Con más, la primera celda nombra la
    fila y las demás van con su encabezado: `Básico — Precio: $500, Sesiones: 4`.
    Es lo que se lee bien en un teléfono; una tabla, no.
    """
    lineas = s.split("\n")
    salida: list[str] = []
    i = 0
    while i < len(lineas):
        if (
            i + 1 < len(lineas)
            and "|" in lineas[i]
            and "|" in lineas[i + 1]
            and "-" in lineas[i + 1]
            and _SEPARADOR_TABLA.match(lineas[i + 1])
        ):
            encabezado = _celdas(lineas[i])
            i += 2
            while i < len(lineas) and "|" in lineas[i] and lineas[i].strip():
                salida.append(_fila(encabezado, _celdas(lineas[i])))
                i += 1
            continue
        salida.append(lineas[i])
        i += 1
    return "\n".join(salida)


def _fila(encabezado: list[str], celdas: list[str]) -> str:
    if not any(celdas):
        return ""
    if len(celdas) == 1:
        return celdas[0]
    if len(celdas) == 2 and len(encabezado) <= 2:
        return f"{celdas[0]}: {celdas[1]}" if celdas[0] else celdas[1]
    pares = []
    for j, valor in enumerate(celdas[1:], start=1):
        if not valor:
            continue
        titulo = encabezado[j] if j < len(encabezado) else ""
        pares.append(f"{titulo}: {valor}" if titulo else valor)
    if not celdas[0]:
        return ", ".join(pares)
    if not pares:
        return celdas[0]
    return f"{celdas[0]} — " + ", ".join(pares)


# ──────────────────────────────────────── 071 (D18): formato por canal ───
#
# WhatsApp pinta `*negrita*` y `_cursiva_`; Instagram, Messenger y TikTok
# enseñan los asteriscos tal cual. El CRM ya los quita al enviar
# (`plainSocialText`, vocero-crm spec 024), pero el historial de Nea y la
# detección de repetidos comparan el texto de Nea: aquí se deja igual de plano
# que lo que el lead lee. Gemelo de `src/lib/social-text.ts`, con los mismos
# vectores en las pruebas de los dos repos
# (`tests/fixtures/texto_plano_vectores.json`).

_SIMPLE = re.compile(
    r"(^|[\s(¿¡\"'«])([*_~`])(?=\S)([^\n]*?\S)\2(?=$|[\s.,!?;:)\"'»…])", re.M
)
_TP_ENLACE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_TP_URL = re.compile(r"https?://[^\s<>]+")
_TP_BLOQUE = re.compile(r"```[a-zA-Z0-9]*\n?([\s\S]*?)```")
_TP_TITULO = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_TP_VINETA = re.compile(r"^([ \t]*)[-*+][ \t]+(?=\S)", re.M)
_TP_DOBLES = (
    re.compile(r"\*\*(?=\S)([^\n]*?\S)\*\*"),
    re.compile(r"__(?=\S)([^\n]*?\S)__"),
    re.compile(r"~~(?=\S)([^\n]*?\S)~~"),
)
_TP_MARCA = re.compile("\x00(\\d+)\x00")


def texto_plano(texto: str) -> str:
    """Gemelo de `plainSocialText` del CRM: sin marcas, mismo contenido.

    Conservador como el original: una marca solo se quita si abre pegada a
    una palabra y cierra pegada a otra. Las URL quedan intactas. Idempotente.
    """
    urls: list[str] = []

    def guardar(url: str) -> str:
        urls.append(url)
        return f"\x00{len(urls) - 1}\x00"

    def enlace(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2)
        return guardar(url) if label.strip() == url else f"{label}: {guardar(url)}"

    out = _TP_ENLACE.sub(enlace, texto)
    out = _TP_URL.sub(lambda m: guardar(m.group(0)), out)
    out = _TP_BLOQUE.sub(lambda m: re.sub(r"\n$", "", m.group(1)), out)
    out = _TP_TITULO.sub(r"\1", out)
    out = _TP_VINETA.sub(r"\1• ", out)
    for doble in _TP_DOBLES:
        out = doble.sub(r"\1", out)
    for _ in range(4):
        siguiente = _SIMPLE.sub(r"\1\3", out)
        if siguiente == out:
            break
        out = siguiente
    return _TP_MARCA.sub(lambda m: urls[int(m.group(1))] if int(m.group(1)) < len(urls) else "", out)


def a_texto_plano(texto: str) -> str:
    """Markdown → WhatsApp (tablas, títulos, enlaces) → sin marcas."""
    if not texto:
        return texto
    return texto_plano(a_whatsapp(texto))


def formatear_para(texto: str, canal: str | None) -> str:
    """El texto como lo lee el lead en SU canal.

    WhatsApp (o sin canal, como el seguimiento de siempre): `a_whatsapp`. En
    las redes, texto plano.
    """
    if (canal or "").strip().lower() in ("", "whatsapp"):
        return a_whatsapp(texto)
    return a_texto_plano(texto)
