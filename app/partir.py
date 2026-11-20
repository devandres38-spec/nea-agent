"""vocero-crm 073-C (D5) — Una respuesta larga en varios mensajes que SÍ salen.

Cada canal tiene su tope: WhatsApp 4096 caracteres, Messenger 2000 e
Instagram 1000 (TikTok, igual de corto que Instagram). Una respuesta más
larga la rechazaba el CRM (`text_too_long`, 422) y el lead se quedaba sin
nada. Se parte por párrafos; un párrafo que no cabe, por oraciones; una
oración que no cabe, por palabras. Nunca se corta una URL.
"""
from __future__ import annotations

import re

LIMITES = {
    "whatsapp": 4096,
    "messenger": 2000,
    "instagram": 1000,
    "tiktok": 1000,
}
# Margen: el CRM mide después de su propia limpieza (texto plano en redes).
MARGEN = 40

_ORACIONES = re.compile(r"(?<=[.!?…])\s+")


def limite_de(canal: str | None) -> int:
    return LIMITES.get((canal or "whatsapp").lower(), LIMITES["whatsapp"]) - MARGEN


def _por_palabras(texto: str, tope: int) -> list[str]:
    partes: list[str] = []
    actual = ""
    for palabra in texto.split(" "):
        candidato = f"{actual} {palabra}" if actual else palabra
        if len(candidato) <= tope:
            actual = candidato
            continue
        if actual:
            partes.append(actual)
        # Una palabra (o URL) más larga que el tope va sola: el CRM decidirá.
        actual = palabra
    if actual:
        partes.append(actual)
    return partes


def _trozos(parrafo: str, tope: int) -> list[str]:
    if len(parrafo) <= tope:
        return [parrafo]
    salida: list[str] = []
    for oracion in _ORACIONES.split(parrafo):
        salida.extend([oracion] if len(oracion) <= tope else _por_palabras(oracion, tope))
    return salida


def partir(texto: str, canal: str | None) -> list[str]:
    """Las partes, en orden, cada una dentro del tope del canal.

    Los párrafos se juntan con línea en blanco mientras quepan; los trozos de
    un mismo párrafo partido, con un espacio.
    """
    tope = limite_de(canal)
    texto = (texto or "").strip()
    if len(texto) <= tope:
        return [texto] if texto else []
    partes: list[str] = []
    actual = ""
    for parrafo in re.split(r"\n\s*\n", texto):
        for i, trozo in enumerate(_trozos(parrafo.strip(), tope)):
            if not trozo:
                continue
            sep = "\n\n" if i == 0 else " "
            candidato = f"{actual}{sep}{trozo}" if actual else trozo
            if len(candidato) <= tope:
                actual = candidato
            else:
                if actual:
                    partes.append(actual)
                actual = trozo
    if actual:
        partes.append(actual)
    return partes
