"""071 (B0, D14) — El prompt de hoy, congelado byte a byte.

El chasis se va a trocear en secciones y los `.replace()` de cloud se van a
convertir en un registro de roles. Ese refactor NO puede cambiar una sola letra
de lo que lee el modelo: cada palabra del chasis salió de un incidente en
producción. Estas pruebas comparan contra copias guardadas del prompt que
generaba el código antes del refactor.

Los dorados de B0 NO se regeneran: son el texto de antes de la 071. La
prueba les aplica solo los cambios aprobados en la spec (`DELTAS_071`) y exige
que el resultado sea idéntico byte a byte. Así cualquier otra diferencia —una
coma— rompe la prueba.

Los casos nuevos de la 071 (trato `usted` y `neutro`) tienen dorados propios.
Regenerarlos (solo si un cambio del texto está aprobado en una spec):

    NEA_ACTUALIZAR_DORADOS=1 pytest tests/test_chasis_dorado.py
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dataclasses import replace

from app.chasis.armar import LINEA_IDIOMA, VOZ_TU
from app.profile import BusinessProfile, profile_from_payload
from app.crm_brains import _perfil_desde_contexto
from app.prompt import build_system_prompt
from app.state import Conversation, OfferedSlot

DORADOS = Path(__file__).parent / "fixtures" / "prompt"
AHORA = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
BOGOTA = ZoneInfo("America/Bogota")

PERFIL_SINGLE = BusinessProfile(
    agent_name="Sofi",
    tone="Cercano y directo",
    instructions="Somos una clínica dental en Bogotá.",
    escalation_rules="Urgencias: pasar a una persona.",
    greeting="¡Hola! Soy Sofi, de Clínica Sonrisa.",
    kb_text="P: Precio limpieza\nR: 120.000 COP",
    resources=[{"label": "Blog", "url": "https://ejemplo.co/blog"}],
)

CONTEXTO = {
    "contact": {"name": "Gerardo", "ficha": {"rubro": "salud", "vacio": ""}, "channel": "whatsapp"},
    "lead": {"stageName": "Nuevo", "nextStages": ["Interesado", "Agendado"]},
    "adOrigen": {"headline": "Limpieza dental 2x1"},
    "booking": {
        "timezone": "America/Bogota",
        "next": None,
        "agendas": [
            {"name": "Dra. Ana", "description": "ortodoncia"},
            {"name": "Dr. Luis", "description": "limpiezas"},
        ],
    },
}


def _perfil_cloud() -> BusinessProfile:
    return profile_from_payload(
        _perfil_desde_contexto(
            {
                "agent": {"name": "Asistente", "tone": "Formal", "role": "calificador"},
                "knowledge": [{"kind": "qa", "question": "Horario", "answer": "8 a 6"}],
            }
        ),
        "Asistente",
    )


def _ofrecidos() -> list[OfferedSlot]:
    inicio = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)
    return [OfferedSlot(conversation_id=1, start_utc=inicio, end_utc=None, label="mañana martes 15, 10:00")]


CASOS = {
    "single_sin_agenda": dict(
        profile=PERFIL_SINGLE,
        context={"contact": {"name": "Gerardo"}},
        conv=Conversation(id=1, wa_identity="573001112233"),
        agenda=False,
    ),
    "single_con_agenda": dict(
        profile=PERFIL_SINGLE,
        context=CONTEXTO,
        conv=Conversation(id=1, wa_identity="573001112233", greeted=True),
        offered=_ofrecidos(),
        agenda=True,
        canal="whatsapp",
    ),
    "single_minimo": dict(
        profile=BusinessProfile(),
        context=None,
        conv=Conversation(id=1, wa_identity="573001112233"),
    ),
    "single_instagram": dict(
        profile=PERFIL_SINGLE,
        context=CONTEXTO,
        conv=Conversation(id=1, wa_identity="ig:123"),
        referral_headline="Anuncio de Instagram",
        agenda=True,
        canal="instagram",
    ),
    "cloud_agenda_v2": dict(
        profile=_perfil_cloud(),
        context=CONTEXTO,
        conv=Conversation(id=1, wa_identity="573001112233", greeted=True),
        agenda=True,
        recordatorios=True,
        canal="whatsapp",
    ),
    "cloud_sin_agenda_messenger": dict(
        profile=_perfil_cloud(),
        context={"contact": {"name": "Gerardo"}},
        conv=Conversation(id=1, wa_identity="fb:9"),
        agenda=False,
        canal="messenger",
    ),
}


def aplicar_deltas_071(texto: str, nombre: str) -> str:
    """Los únicos cambios aprobados sobre el prompt de antes de la 071.

    D15: la regla de idioma, una línea nueva justo después de la del trato.
    """
    assert texto.count(VOZ_TU) == 1, nombre
    return texto.replace(VOZ_TU, VOZ_TU + "\n" + LINEA_IDIOMA)


@pytest.mark.parametrize("nombre", sorted(CASOS))
def test_el_prompt_no_cambia_ni_una_letra(nombre: str) -> None:
    prompt = build_system_prompt(now=AHORA, tz=BOGOTA, **CASOS[nombre])
    original = (DORADOS / f"{nombre}.txt").read_bytes().decode("utf-8")
    esperado = aplicar_deltas_071(original, nombre)
    assert prompt == esperado, f"el prompt de {nombre} cambió más allá de lo aprobado en la 071"


# 071 (D15) — Casos nuevos, con dorado propio.
CASOS_071 = {
    "071_single_usted": dict(CASOS["single_con_agenda"], profile=replace(PERFIL_SINGLE, treatment="usted")),
    "071_cloud_neutro": dict(CASOS["cloud_agenda_v2"], profile=replace(_perfil_cloud(), treatment="neutro")),
}


@pytest.mark.parametrize("nombre", sorted(CASOS_071))
def test_los_tratos_nuevos_tienen_su_dorado(nombre: str) -> None:
    prompt = build_system_prompt(now=AHORA, tz=BOGOTA, **CASOS_071[nombre])
    archivo = DORADOS / f"{nombre}.txt"
    if os.environ.get("NEA_ACTUALIZAR_DORADOS") == "1":
        archivo.write_bytes(prompt.encode("utf-8"))
    assert prompt == archivo.read_bytes().decode("utf-8")


def test_un_trato_solo_cambia_la_linea_de_la_voz() -> None:
    """Fuera de la línea del trato, `usted` y `neutro` dicen lo mismo que `tu`."""
    base = build_system_prompt(now=AHORA, tz=BOGOTA, **CASOS["single_con_agenda"])
    for nombre in CASOS_071:
        otro = build_system_prompt(now=AHORA, tz=BOGOTA, **CASOS_071[nombre])
        dif = [(a, b) for a, b in zip(base.split("\n"), otro.split("\n")) if a != b]
        if nombre.startswith("071_single"):
            assert len(dif) == 1 and dif[0][0] == VOZ_TU
