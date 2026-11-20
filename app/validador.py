"""vocero-crm 073-C (D7) — Lo que el modelo NO puede mandarle al lead.

Determinista, después del turno y antes de enviar:

1. `<think>…</think>` (GLM-5.3-Flash y otros modelos de razonamiento a veces
   lo filtran al contenido): se LIMPIA. Si no queda nada, el turno se trata
   como sin texto.
2. Nombres de herramientas o marcas de llamada (`book_session`,
   `<tool_call>`, `function_call`): el lead vería las tripas → corrección.
3. Notas internas parciales: una línea entera entre paréntesis o corchetes
   que empieza como nota («(Nota: …», «[Registro actualizado…») → corrección.
   (La nota que es TODO el mensaje ya la atrapa `_respuesta_invalida`.)
4. Promesas sin la herramienta:
   - «tu cita quedó agendada» sin `book_session`/`reschedule_session` con
     éxito → corrección (no se le confirma a nadie una cita que no existe).
   - «te paso con un asesor» sin `handoff` → se CUMPLE: el turno registra el
     handoff (`modelo`). Retractar la promesa dejaría al lead esperando a
     alguien que nadie va a mandar; cumplirla solo cuesta una alerta.
   Ninguna de las dos cuenta en una pregunta («¿quieres que te agende…?»,
   «¿te paso con un asesor?») ni en una oferta condicional («si quieres…»).

La corrección es la de siempre: UNA vuelta más, SIN herramientas.
"""
from __future__ import annotations

import re
import unicodedata

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_SIN_CERRAR = re.compile(r"<think>.*\Z", re.S | re.I)
_CIERRE_SUELTO = re.compile(r"\A.*?</think>", re.S | re.I)

HERRAMIENTAS = (
    "update_ficha",
    "move_stage",
    "propose_slots",
    "book_session",
    "reschedule_session",
    "list_bookings",
    "cancel_session",
    "coordination_consent",
    "route_out",
    "solicitar_baja",
)
_NOMBRE_DE_HERRAMIENTA = re.compile(r"\b(" + "|".join(HERRAMIENTAS) + r")\b|\bhandoff\s*\(")
_MARCA_DE_LLAMADA = re.compile(r"</?\s*tool_call\s*>|<\|?\s*tool|\bfunction_call\b|\"arguments\"\s*:", re.I)
_NOTA_PARCIAL = re.compile(
    r"^\s*[(\[]\s*(nota|registro|interno|sistema|ficha|actualiz|conversaci[oó]n cerrada|handoff|herramienta)",
    re.I | re.M,
)


def _plano(texto: str) -> str:
    sin = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in sin if not unicodedata.combining(c))


# Parche final 073-C: con las tildes quitadas, «¿quieres que te agende…?»
# (subjuntivo) se leía como «te agendé» (pretérito). La cita se busca en el
# texto CON tildes, el pretérito las exige, y las preguntas no cuentan.
_PROMETE_CITA = re.compile(
    r"\b(?:"
    r"(?:tu|su|la) cita (?:ya )?(?:qued[óo]|est[áa]|queda) (?:agendada|confirmada|reservada|separada|programada)"
    r"|(?:ya )?(?:te|le) (?:agendé|reservé|separé|programé|aparté)(?!\w)"
    r"|(?:ya )?qued[óo] (?:agendad|reservad|confirmad|separad)[oa]"
    r"|(?:ya )?(?:estás|está|estas|esta) agendad[oa]"
    r")"
)
_PROMETE_HANDOFF = re.compile(
    r"\b(?:"
    r"(?:te|le|lo|la) (?:paso|comunico|transfiero|conecto|remito) con"
    r"|(?:un|una) (?:asesor|asesora|persona|agente humano|ejecutivo|ejecutiva) (?:te|le) (?:va a )?"
    r"(?:contacta|escribe|llama|atiende|contactara|escribira|llamara|atendera|escribir|atender)"
    r"|(?:el|nuestro) equipo (?:te|le) (?:va a )?"
    r"(?:contacta|escribe|llama|atiende|contactara|escribira|llamara|atendera|escribir|atender)"
    r"|ya (?:le )?avise al equipo"
    r")"
)
# Una oferta no es una promesa: «si quieres, te paso con un asesor».
_CONDICIONAL = re.compile(
    r"\bsi (?:quieres|quiere|prefieres|prefiere|necesitas|necesita|deseas|desea"
    r"|gustas|gusta|lo prefieres|lo necesitas|te parece|le parece)\b"
)
_FRASES = re.compile(r"(?<=[.!?…])\s+|\n+")


def _frases(texto: str) -> list[str]:
    return [f for f in _FRASES.split(texto) if f.strip()]


def _es_pregunta(frase: str) -> bool:
    return "¿" in frase or frase.rstrip().endswith("?")


def _promete_cita(texto: str) -> bool:
    return any(
        _PROMETE_CITA.search(unicodedata.normalize("NFC", f.lower()))
        for f in _frases(texto)
        if not _es_pregunta(f)
    )


def limpiar_pensamiento(texto: str | None) -> str | None:
    """Sin `<think>…</think>` (cerrado, sin cerrar, o solo el cierre)."""
    if texto is None:
        return None
    limpio = _THINK.sub("", texto)
    limpio = _THINK_SIN_CERRAR.sub("", limpio)
    if re.search(r"</think>", limpio, re.I):
        limpio = _CIERRE_SUELTO.sub("", limpio)
    return limpio.strip()


def promete_handoff(texto: str) -> bool:
    """Una promesa afirmativa de pasarlo a una persona. Las preguntas («¿te
    paso con un asesor?») y las ofertas condicionales no cuentan."""
    for frase in _frases(texto):
        if _es_pregunta(frase):
            continue
        plana = _plano(frase)
        if _CONDICIONAL.search(plana):
            continue
        if _PROMETE_HANDOFF.search(plana):
            return True
    return False


def revisar(texto: str | None, *, reservo: bool) -> str | None:
    """Por qué el texto no puede salir (la corrección lo cita), o None."""
    if not texto or not texto.strip():
        return None
    if _MARCA_DE_LLAMADA.search(texto) or _NOMBRE_DE_HERRAMIENTA.search(texto):
        return "nombraba una herramienta o una llamada interna; el lead no puede ver eso"
    if _NOTA_PARCIAL.search(texto):
        return "traía una nota interna entre paréntesis o corchetes"
    if not reservo and _promete_cita(texto):
        return (
            "daba la cita por agendada sin haberla reservado; no la confirmes "
            "hasta que la reserva haya salido bien"
        )
    return None
