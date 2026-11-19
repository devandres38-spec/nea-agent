"""Guardarraíl de la confirmación de cita (vocero-crm 071, D17).

Antes, al reservar, la respuesta del modelo se TIRABA entera y salía una
plantilla con «tu» fijo: la voz del negocio desaparecía justo en el mensaje
más importante. Confiar a ciegas tampoco: el modelo ya confirmó fechas
equivocadas (incidentes anotados en `app/prompt.py`).

Tres salidas, sobre el texto del modelo normalizado (minúsculas, sin acentos,
`a. m.` → `am`, `10.30` → `10:30`, meses y días en inglés → español):

- **ok**: trae la fecha (día del mes + mes o día de la semana, o «hoy» /
  «mañana» cuando la cita es ese día), la hora en alguna forma (`10:30`,
  `10.30`, `10 am`, `10 de la mañana`) y el enlace si lo hay → sale tal cual.
- **pie**: le falta algo → se le añade debajo lo que falta (`📅 {label}`, el
  enlace, el aviso del enlace pendiente, los recordatorios).
- **contradicción**: en la misma oración que habla de la cita trae otra hora,
  otro día, «hoy» por «mañana», o habla del enlace de la videollamada sin darlo
  (o da otro) → la plantilla determinista de siempre, con el trato del negocio.

Los datos de la cita salen del instante reservado (`start_utc` + zona del
negocio) cuando el turno los tiene; la etiqueta es el respaldo.

R3: una hora en OTRA oración («abrimos a las 9:00») no es contradicción, ni
«15 minutos antes» en una cita del día 15, ni el enlace de Maps de una cita
presencial. En una cita MOVIDA solo cuenta lo que va tras «para el …» / «al …»
(lo de antes es la cita vieja).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)
_MESES_CORTOS = {m[:3]: m for m in MESES} | {"sept": "septiembre", "set": "septiembre"}
DIAS_SEMANA = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
_DIAS_CORTOS = {d[:3]: d for d in DIAS_SEMANA} | {"mie": "miercoles", "sab": "sabado"}

# M-4: si el cliente escribe en inglés (regla de idioma), el modelo contesta en
# inglés; se traduce a lo que entiende el guardarraíl.
_INGLES = {
    "january": "enero", "february": "febrero", "march": "marzo", "april": "abril",
    "may": "mayo", "june": "junio", "july": "julio", "august": "agosto",
    "september": "septiembre", "october": "octubre", "november": "noviembre",
    "december": "diciembre",
    "monday": "lunes", "tuesday": "martes", "wednesday": "miercoles", "thursday": "jueves",
    "friday": "viernes", "saturday": "sabado", "sunday": "domingo",
    "today": "hoy", "tomorrow": "manana", "appointment": "cita", "meeting": "cita",
}
_INGLES_RE = re.compile(r"\b(" + "|".join(_INGLES) + r")\b")

_SUFIJO = r"am|pm|de la manana|de la tarde|de la noche|del mediodia"
_URL = re.compile(r"https?://\S+")
_HORA_HM = re.compile(r"(?<![\d:.])(\d{1,2})\s*:\s*(\d{2})(?!\d)(?:\s*(" + _SUFIJO + r")\b)?")
_HORA_SOLA = re.compile(r"(?<![\d:.])(\d{1,2})\s*(" + _SUFIJO + r")\b")
_DIA_DE_MES = re.compile(r"(?<![\d:.$])(\d{1,2})\s+de\s+(" + "|".join(MESES) + r")\b")
_FRASES = re.compile(r"[^.!?\n]+")
_HABLA_DEL_ENLACE = re.compile(r"\b(enlace|link|liga|url|videollamada)\b")
_VIDEOLLAMADA = re.compile(
    r"(zoom\.us|meet\.google\.com|teams\.microsoft\.com|teams\.live\.com|whereby\.com|webex\.com|meet\.jit\.si)",
    re.I,
)
# Cita movida: lo que va tras el conector que introduce la fecha NUEVA.
_DESTINO = re.compile(
    r"\b(?:para|al|a)\s+(?:el\s+)?(?:dia\s+)?"
    r"(?=\d|hoy\b|manana\b|lunes|martes|miercoles|jueves|viernes|sabado|domingo)"
)


def normalizar(texto: str) -> str:
    sin = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sin if not unicodedata.combining(c)).lower()


def _preparar(texto: str) -> str:
    """Normaliza y deja las horas en una sola forma (C-1).

    Sin esto, el punto de `p.m.` o de `10.30` partía la oración en dos: la
    hora quedaba sin su `pm` (y pasaba contra una cita de la mañana) o el
    `11.30` desaparecía (y salía un pie con OTRA hora en el mismo mensaje).
    """
    t = normalizar(texto)
    t = _URL.sub(" ", t)  # las URL no son oraciones ni horas
    t = re.sub(r"(?<=\d)\s*a\.?\s*m\.?(?![a-z])", " am", t)
    t = re.sub(r"(?<=\d)\s*p\.?\s*m\.?(?![a-z])", " pm", t)
    t = re.sub(r"(?<![\d:.])(\d{1,2})\.(\d{2})(?!\d)", r"\1:\2", t)
    return _INGLES_RE.sub(lambda m: _INGLES[m.group(1)], t)


@dataclass(frozen=True)
class Cita:
    label: str
    dia: int | None
    mes: str | None
    semana: str | None
    relativo: str | None  # "hoy" | "manana"
    hora: int | None
    minuto: int | None
    # ¿Se sabe con certeza si la cita es hoy, mañana u otro día? (A-6)
    relativo_seguro: bool = False


def _a_24(h: int, sufijo: str | None) -> int:
    s = (sufijo or "").replace(" ", "")
    if s in ("pm", "delatarde", "delanoche") and h < 12:
        return h + 12
    if s in ("am", "delamanana") and h == 12:
        return 0
    return h


def _dia_re(dia: int) -> str:
    """El número del día, pero no «15 minutos», «15 h» ni «$15» (A-6)."""
    return rf"(?<![\d:.$]){dia}(?![\d:.])(?!\s*(?:min|minutos|hora|horas|hrs|h)\b)"


def cita_de(datos: dict[str, Any]) -> Cita:
    """La cita desde el instante reservado (A-5); si no viene, desde la etiqueta."""
    label = str(datos.get("label") or "")
    inicio, zona = datos.get("start_utc"), datos.get("tz")
    if inicio and zona:
        try:
            dt = datetime.fromisoformat(str(inicio).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(ZoneInfo(str(zona)))
        except (ValueError, KeyError, OSError):
            dt = None
        if dt is not None:
            relativo, seguro = None, False
            if datos.get("hoy"):
                try:
                    faltan = (dt.date() - date.fromisoformat(str(datos["hoy"]))).days
                    relativo = {0: "hoy", 1: "manana"}.get(faltan)
                    seguro = True
                except ValueError:
                    pass
            if not seguro:
                relativo = leer_cita(label).relativo
            return Cita(
                label=label,
                dia=dt.day,
                mes=MESES[dt.month - 1],
                semana=DIAS_SEMANA[dt.weekday()],
                relativo=relativo,
                hora=dt.hour,
                minuto=dt.minute,
                relativo_seguro=seguro,
            )
    return leer_cita(label)


def leer_cita(label: str) -> Cita:
    """Los datos de la cita desde su etiqueta («mañana martes 15, 10:00 con Ana»)."""
    n = _preparar(label)
    # A-5: «con Julio», «con Abril» o «con Domingo» no son mes ni día.
    n = re.split(r"\s+con\s+", n, maxsplit=1)[0]
    iso = re.match(r"^\s*(\d{4})-(\d{2})-(\d{2})t(\d{2}):(\d{2})", n)
    if iso:
        y, mo, d, h, mi = (int(x) for x in iso.groups())
        try:
            f = date(y, mo, d)
        except ValueError:
            f = None
        if f is not None:
            return Cita(label, d, MESES[mo - 1], DIAS_SEMANA[f.weekday()], None, h, mi)
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
        # A-5: el mes corto solo tras el número del día («mar 7 ago»: «ago»).
        corto = re.search(
            r"(?<![\d:.])\d{1,2}\s+(?:de\s+)?(" + "|".join(_MESES_CORTOS) + r")\b", sin_hora
        )
        mes = _MESES_CORTOS[corto.group(1)] if corto else None
    semana = next((d for d in DIAS_SEMANA if re.search(rf"\b{d}\b", sin_hora)), None)
    if semana is None:
        # El día corto solo delante del número («mar 7»), nunca suelto.
        corto = re.search(r"\b(" + "|".join(_DIAS_CORTOS) + r")\b\.?\s+\d", sin_hora)
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
    """Horas escritas en la frase: (hora, minuto, ¿dijo am/pm o «de la …»?)."""
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
    if cita.dia is not None and re.search(_dia_re(cita.dia), t):
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


_VERBOS_DE_CITA = re.compile(
    r"\b(agend|reserv|apart|confirm|qued|te espero|le espero|los espero|esperamos|nos vemos|movi|cambi|program)"
)


def _habla_de_la_cita(cita: Cita, frase: str) -> bool:
    return bool(
        re.search(r"\bcita\b", frase)
        or _VERBOS_DE_CITA.search(frase)
        or (cita.mes and re.search(rf"\b{cita.mes}\b", frase))
        or (cita.semana and re.search(rf"\b{cita.semana}\b", frase))
        or (cita.relativo == "hoy" and re.search(r"\bhoy\b", frase))
        or (cita.relativo == "manana" and _manana_es_dia(frase))
        or (cita.dia is not None and re.search(_dia_re(cita.dia), frase))
        or any(_misma_hora(cita, h, mi, s) for h, mi, s in _horas_en(frase))
    )


def _contradice(cita: Cita, t: str, tipo: str | None = None) -> bool:
    if tipo == "reschedule":
        # A-6: «moví tu cita del martes 15 a las 10:00 PARA EL jueves 17…»:
        # lo de antes del conector es la cita vieja, no una contradicción.
        destinos = list(_DESTINO.finditer(t))
        if destinos:
            t = t[destinos[-1].start():]
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
            if any(d != cita.semana and re.search(rf"\b{d}\b", frase) for d in DIAS_SEMANA):
                return True
        # A-6: «hoy» por «mañana» (o al revés), y cualquiera de los dos para
        # una cita que no es ni hoy ni mañana (si se sabe).
        dice_hoy = bool(re.search(r"\bhoy\b", frase))
        dice_manana = _manana_es_dia(frase)
        if cita.relativo == "hoy" and dice_manana:
            return True
        if cita.relativo == "manana" and dice_hoy:
            return True
        if cita.relativo is None and cita.relativo_seguro and (dice_hoy or dice_manana):
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
    # B-4: un `tipo` desconocido es una cita nueva, no un KeyError.
    parts = [x.get(str(datos.get("tipo") or "book"), x["book"]).format(label=label)]
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
    t = _preparar(texto or "")
    if not t.strip() or not label:
        return "contradiccion", []
    cita = cita_de(datos)
    urls = _URL.findall(texto)
    otra_videollamada = any(_VIDEOLLAMADA.search(u) for u in urls if u != url)
    if _contradice(cita, t, datos.get("tipo")):
        return "contradiccion", []
    if url and url not in texto and (_HABLA_DEL_ENLACE.search(t) or otra_videollamada):
        # Habla del enlace sin darlo («te lo mando después») o da otro.
        return "contradiccion", []
    if not url and urls and (_HABLA_DEL_ENLACE.search(t) or otra_videollamada):
        # A-6: un enlace de videollamada que el CRM no dio. El de Maps o la
        # web del negocio en una cita presencial no lo es.
        return "contradiccion", []
    falta: list[str] = []
    if not (_menciona_la_fecha(cita, t) and _menciona_la_hora(cita, t)):
        falta.append("fecha")
    if url and url not in texto:
        falta.append("enlace")
    if not url and datos.get("link_pending") and not _HABLA_DEL_ENLACE.search(t):
        falta.append("pendiente")
    if datos.get("reminder_consent") and not re.search(r"recordatorio|reminder", t):
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
