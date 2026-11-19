"""Guardarraíl de la confirmación de cita (vocero-crm 071, D17).

Antes, al reservar, la respuesta del modelo se TIRABA entera y salía una
plantilla con «tu» fijo: la voz del negocio desaparecía justo en el mensaje
más importante. Confiar a ciegas tampoco: el modelo ya confirmó fechas
equivocadas (incidentes anotados en `app/prompt.py`).

Tres salidas, sobre el texto del modelo normalizado (minúsculas, sin acentos):

- **ok**: trae la fecha (día del mes + mes o día de la semana, o «hoy» /
  «mañana» cuando la cita es ese día), la hora en alguna forma (`10:30`,
  `10.30`, `10 am`, `10 de la mañana`) y el enlace si lo hay → sale tal cual.
- **pie**: le falta algo → se le añade debajo lo que falta (`📅 {label}`, el
  enlace, el aviso del enlace pendiente, los recordatorios).
- **contradicción**: en la misma oración que habla de la cita trae otra hora u
  otro día, o habla del enlace sin darlo (o da uno que no existe) → la
  plantilla determinista de siempre, con el trato del negocio.

R3: una hora en OTRA oración («abrimos a las 9:00») no es contradicción.
Sirve para la cita nueva (`book_session`) y la movida (`reschedule_session`).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)
_MESES_CORTOS = {m[:3]: m for m in MESES} | {"sept": "septiembre", "set": "septiembre"}
DIAS_SEMANA = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
_DIAS_CORTOS = {d[:3]: d for d in DIAS_SEMANA} | {"mie": "miercoles", "sab": "sabado"}

_URL = re.compile(r"https?://\S+")
_HORA_HM = re.compile(r"(?<![\d:.])(\d{1,2})\s*[:.]\s*(\d{2})(?![\d])(?:\s*(a\.?\s*m\.?|p\.?\s*m\.?))?")
_HORA_SOLA = re.compile(
    r"(?<![\d:.])(\d{1,2})\s*(a\.?\s*m\.?|p\.?\s*m\.?|de la manana|de la tarde|de la noche|del mediodia)(?![a-z])"
)
_DIA_DE_MES = re.compile(r"(?<![\d:.])(\d{1,2})\s+de\s+(" + "|".join(MESES) + r")\b")
_FRASES = re.compile(r"[^.!?\n]+")
_HABLA_DEL_ENLACE = re.compile(r"\b(enlace|link|liga|url)\b")


def normalizar(texto: str) -> str:
    sin = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sin if not unicodedata.combining(c)).lower()


@dataclass(frozen=True)
class Cita:
    label: str
    dia: int | None
    mes: str | None
    semana: str | None
    relativo: str | None  # "hoy" | "manana"
    hora: int | None
    minuto: int | None


def _a_24(h: int, sufijo: str | None) -> int:
    s = (sufijo or "").replace(".", "").replace(" ", "")
    if s in ("pm", "delatarde", "delanoche") and h < 12:
        return h + 12
    if s in ("am", "delamanana") and h == 12:
        return 0
    return h


def leer_cita(label: str) -> Cita:
    """Los datos de la cita desde su etiqueta («mañana martes 15, 10:00 con Ana»)."""
    n = normalizar(label)
    hora = minuto = None
    m = _HORA_HM.search(n)
    if m:
        hora, minuto = _a_24(int(m.group(1)), m.group(3)), int(m.group(2))
        sin_hora = n[: m.start()] + " " + n[m.end():]
    else:
        sin_hora = n
    dia_m = re.search(r"(?<![\d:.])(\d{1,2})(?![\d:.])", sin_hora)
    mes = next((x for x in MESES if re.search(rf"\b{x}\b", sin_hora)), None)
    if mes is None:
        corto = re.search(r"\b(" + "|".join(_MESES_CORTOS) + r")\b", sin_hora)
        mes = _MESES_CORTOS[corto.group(1)] if corto else None
    semana = next((d for d in DIAS_SEMANA if re.search(rf"\b{d}\b", sin_hora)), None)
    if semana is None:
        corto = re.search(r"\b(" + "|".join(_DIAS_CORTOS) + r")\b", sin_hora)
        semana = _DIAS_CORTOS[corto.group(1)] if corto else None
    relativo = "hoy" if re.search(r"\bhoy\b", sin_hora) else (
        "manana" if re.search(r"^\s*manana\b", sin_hora) else None
    )
    return Cita(
        label=label,
        dia=int(dia_m.group(1)) if dia_m else None,
        mes=mes,
        semana=semana,
        relativo=relativo,
        hora=hora,
        minuto=minuto,
    )


def _horas_en(frase: str) -> list[tuple[int, int, bool]]:
    """Horas escritas en la frase: (hora, minuto, ¿dijo am/pm?)."""
    out = [
        (_a_24(int(m.group(1)), m.group(3)), int(m.group(2)), bool(m.group(3)))
        for m in _HORA_HM.finditer(frase)
    ]
    out += [(_a_24(int(m.group(1)), m.group(2)), 0, True) for m in _HORA_SOLA.finditer(frase)]
    return out


def _misma_hora(cita: Cita, h: int, mi: int, con_sufijo: bool) -> bool:
    if cita.hora is None or mi != (cita.minuto or 0):
        return False
    if con_sufijo or h > 12:
        return h == cita.hora
    # «10:00» a secas vale por las 10 o las 22.
    return h % 12 == cita.hora % 12


def _manana_es_dia(texto: str) -> bool:
    """«mañana» como día, no «de la mañana» / «por la mañana»."""
    return bool(re.search(r"(?<!la )(?<!las )\bmanana\b", texto))


def _menciona_la_fecha(cita: Cita, t: str) -> bool:
    if cita.relativo == "hoy" and re.search(r"\bhoy\b", t):
        return True
    if cita.relativo == "manana" and _manana_es_dia(t):
        return True
    if cita.dia is not None and re.search(rf"(?<![\d:.]){cita.dia}(?![\d:.])", t):
        if (cita.mes and re.search(rf"\b{cita.mes}\b", t)) or (
            cita.semana and re.search(rf"\b{cita.semana}\b", t)
        ):
            return True
    return False


def _menciona_la_hora(cita: Cita, t: str) -> bool:
    if cita.hora is None:
        return True
    if any(_misma_hora(cita, h, mi, suf) for h, mi, suf in _horas_en(t)):
        return True
    h12 = cita.hora % 12 or 12
    if cita.minuto == 30 and re.search(rf"(?<![\d:.]){h12} y media\b", t):
        return True
    if cita.minuto == 0 and re.search(rf"\ba las {h12}\b(?![:.\d])", t):
        return True
    return False


def _habla_de_la_cita(cita: Cita, frase: str) -> bool:
    return bool(
        re.search(r"\bcita\b", frase)
        or (cita.mes and re.search(rf"\b{cita.mes}\b", frase))
        or (cita.semana and re.search(rf"\b{cita.semana}\b", frase))
        or (cita.relativo == "hoy" and re.search(r"\bhoy\b", frase))
        or (cita.relativo == "manana" and _manana_es_dia(frase))
        or (cita.dia is not None and re.search(rf"(?<![\d:.]){cita.dia}(?![\d:.])", frase))
    )


def _contradice(cita: Cita, t: str) -> bool:
    for frase in _FRASES.findall(t):
        if not _habla_de_la_cita(cita, frase):
            continue
        for h, mi, suf in _horas_en(frase):
            if not _misma_hora(cita, h, mi, suf):
                return True
        for d, mes in _DIA_DE_MES.findall(frase):
            if (int(d), mes) != (cita.dia, cita.mes or mes):
                return True
        if cita.semana:
            otros = [d for d in DIAS_SEMANA if d != cita.semana and re.search(rf"\b{d}\b", frase)]
            if otros:
                return True
    return False


# ─────────────────────────────────────────────── textos por trato ───

_TEXTOS = {
    "tu": {
        "book": "Listo, tu cita quedó confirmada para {label}.",
        "reschedule": "Listo, tu cita quedó movida para {label}.",
        "pendiente": "El enlace de la videollamada te llegará por aquí en un momento.",
        "recordatorios": "También quedaron activados los recordatorios que autorizaste.",
    },
    "usted": {
        "book": "Listo, su cita quedó confirmada para {label}.",
        "reschedule": "Listo, su cita quedó movida para {label}.",
        "pendiente": "El enlace de la videollamada le llegará por aquí en un momento.",
        "recordatorios": "También quedaron activados los recordatorios que autorizó.",
    },
    "neutro": {
        "book": "Listo: la cita quedó confirmada para {label}.",
        "reschedule": "Listo: la cita quedó movida para {label}.",
        "pendiente": "El enlace de la videollamada llegará por aquí en un momento.",
        "recordatorios": "También quedaron activados los recordatorios autorizados.",
    },
}
ENLACE = "Enlace de la reunión: {url}"


def _textos(trato: str | None) -> dict[str, str]:
    return _TEXTOS.get(trato or "tu", _TEXTOS["tu"])


def plantilla(datos: dict[str, Any], trato: str | None = "tu") -> str:
    """La confirmación determinista de siempre, con el trato del negocio."""
    x = _textos(trato)
    label = datos.get("label") or "el horario acordado"
    parts = [x[datos.get("tipo") or "book"].format(label=label)]
    if datos.get("meeting_url"):
        parts.append(ENLACE.format(url=datos["meeting_url"]))
    elif datos.get("link_pending"):
        parts.append(x["pendiente"])
    if datos.get("reminder_consent"):
        parts.append(x["recordatorios"])
    return "\n\n".join(parts)


def evaluar(texto: str, datos: dict[str, Any]) -> str:
    """`ok`, `pie` o `contradiccion` (para pruebas y logs)."""
    return _decidir(texto, datos)[0]


def _decidir(texto: str, datos: dict[str, Any]) -> tuple[str, list[str]]:
    label = str(datos.get("label") or "")
    url = datos.get("meeting_url")
    t = normalizar(texto or "")
    if not t.strip() or not label:
        return "contradiccion", []
    cita = leer_cita(label)
    urls = _URL.findall(texto)
    if _contradice(cita, t):
        return "contradiccion", []
    if url and url not in texto and (_HABLA_DEL_ENLACE.search(t) or urls):
        # Habla del enlace sin darlo («te lo mando después») o da otro.
        return "contradiccion", []
    if not url and urls:
        return "contradiccion", []  # un enlace que el CRM no dio
    falta: list[str] = []
    if not (_menciona_la_fecha(cita, t) and _menciona_la_hora(cita, t)):
        falta.append("fecha")
    if url and url not in texto:
        falta.append("enlace")
    if not url and datos.get("link_pending") and not _HABLA_DEL_ENLACE.search(t):
        falta.append("pendiente")
    if datos.get("reminder_consent") and "recordatorio" not in t:
        falta.append("recordatorios")
    return ("pie" if falta else "ok"), falta


def confirmar(texto: str, datos: dict[str, Any], trato: str | None = "tu") -> str:
    """El mensaje que sale tras reservar o mover una cita."""
    salida, falta = _decidir(texto, datos)
    if salida == "contradiccion":
        return plantilla(datos, trato)
    if salida == "ok":
        return texto
    x = _textos(trato)
    pie: list[str] = []
    if "fecha" in falta:
        pie.append(f"📅 {datos['label']}")
    if "enlace" in falta:
        pie.append(ENLACE.format(url=datos["meeting_url"]))
    if "pendiente" in falta:
        pie.append(x["pendiente"])
    if "recordatorios" in falta:
        pie.append(x["recordatorios"])
    return texto.rstrip() + "\n\n" + "\n".join(pie)
