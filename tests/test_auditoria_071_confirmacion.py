"""Auditoría 071 — regresiones del guardarraíl de confirmación.

C-1 (a.m./p.m. y 10.30 partían la oración), A-4 (`de la noche` con minutos),
A-5 (la cita sale del instante; «mar» no es marzo; «con Julio» no es mes),
A-6 (cita movida, hoy/mañana, «15 minutos antes», enlaces presenciales),
M-4 (respuesta en inglés) y B-4 (tipo desconocido).
"""
from __future__ import annotations

import pytest

from app.confirmacion import cita_de, confirmar, evaluar, leer_cita, plantilla

URL = "https://meet.google.com/abc-defg-hij"
# Martes 15 de septiembre de 2026, 10:30 en Bogotá (UTC-5).
INICIO = "2026-09-15T15:30:00Z"
BOGOTA = "America/Bogota"


def _d(label="martes 15 de septiembre, 10:30", url=URL, **extra):
    datos = {"tipo": "book", "label": label, "meeting_url": url, "link_pending": False, "reminder_consent": False}
    return datos | extra


def _con_instante(label="martes 15 de septiembre, 10:30", hoy="2026-09-10", **extra):
    return _d(label, start_utc=INICIO, tz=BOGOTA, hoy=hoy, **extra)


# ──────────────────────────────────────────────────────────── C-1 ───


@pytest.mark.parametrize(
    "texto,esperado",
    [
        (f"Tu cita quedó para el martes 15 de septiembre a las 10:30 p.m. {URL}", "contradiccion"),
        (f"Tu cita quedó para el martes 15 de septiembre a las 10:30 p. m. {URL}", "contradiccion"),
        (f"Tu cita quedó para el martes 15 de septiembre a las 11.30. {URL}", "contradiccion"),
        (f"Tu cita quedó para el martes 15 de septiembre a las 10.30 a.m. {URL}", "ok"),
        (f"Tu cita quedó para el martes 15 de septiembre a las 10:30 a. m. Te espero. {URL}", "ok"),
    ],
)
def test_c1_ampm_y_punto_no_parten_la_oracion(texto, esperado):
    assert evaluar(texto, _d()) == esperado
    assert evaluar(texto, _con_instante()) == esperado


def test_c1_una_hora_equivocada_nunca_sale_con_un_pie_que_la_contradiga():
    sale = confirmar(f"Te espero el martes 15 de septiembre a las 11.30. {URL}", _d())
    assert sale.startswith("Listo, tu cita quedó confirmada para martes 15 de septiembre, 10:30")
    assert "11.30" not in sale


# ──────────────────────────────────────────────────────────── A-4 ───


@pytest.mark.parametrize(
    "label,texto,esperado",
    [
        ("martes 15 de septiembre, 9:00", "Te espero el martes 15 de septiembre a las 9:00 de la noche.", "contradiccion"),
        ("martes 15 de septiembre, 15:00", "Te espero el martes 15 de septiembre a las 3:00 de la mañana.", "contradiccion"),
        ("martes 15 de septiembre, 9:00", "Te espero el martes 15 de septiembre a las 9:00 de la mañana.", "ok"),
        ("martes 15 de septiembre, 15:00", "Te espero el martes 15 de septiembre a las 3:00 de la tarde.", "ok"),
    ],
)
def test_a4_de_la_manana_tarde_noche_con_minutos(label, texto, esperado):
    assert evaluar(texto, _d(label, url=None)) == esperado


# ──────────────────────────────────────────────────────────── A-5 ───


def test_a5_mar_es_martes_no_marzo():
    c = leer_cita("mar 7 ago, 10:30")
    assert (c.dia, c.mes, c.semana, c.hora, c.minuto) == (7, "agosto", "martes", 10, 30)
    assert evaluar("Listo, te espero el martes 7 de agosto a las 10:30.", _d("mar 7 ago, 10:30", url=None)) == "ok"


@pytest.mark.parametrize("profesional", ["Julio", "Abril", "Domingo", "Mayo"])
def test_a5_el_profesional_no_contamina_mes_ni_dia(profesional):
    c = leer_cita(f"martes 15, 10:00 con {profesional}")
    assert (c.dia, c.mes, c.semana, c.hora) == (15, None, "martes", 10)


def test_a5_etiqueta_iso_de_respaldo():
    c = leer_cita("2026-08-07T15:30:00Z")
    assert (c.dia, c.mes, c.semana, c.hora, c.minuto) == (7, "agosto", "viernes", 15, 30)


def test_a5_la_cita_sale_del_instante_y_la_zona():
    c = cita_de(_con_instante(label="mar 15 sep, 10:30 con Julio"))
    assert (c.dia, c.mes, c.semana, c.hora, c.minuto) == (15, "septiembre", "martes", 10, 30)
    assert c.relativo is None and c.relativo_seguro


def test_a5_tools_entrega_el_instante_al_guardarrail():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from app.state import Conversation, OfferedSlot
    from app.tools import ToolRuntime
    from tests.conftest import make_ctx

    rt = ToolRuntime(make_ctx(), Conversation(id=1, wa_identity="1"), "cv_1", tz=ZoneInfo(BOGOTA))
    slot = OfferedSlot(1, datetime(2026, 9, 15, 15, 30, tzinfo=timezone.utc), None, "mar 15 sep, 10:30")
    datos = rt._instante_de(slot)
    assert datos["start_utc"] == INICIO and datos["tz"] == BOGOTA and len(datos["hoy"]) == 10


# ──────────────────────────────────────────────────────────── A-6 ───


def test_a6_cita_movida_solo_cuenta_la_fecha_nueva():
    datos = _d("jueves 17 de septiembre, 15:00", tipo="reschedule")
    texto = f"Moví tu cita del martes 15 a las 10:00 para el jueves 17 a las 15:00. {URL}"
    assert evaluar(texto, datos) == "ok"
    malo = f"Moví tu cita del martes 15 a las 10:00 para el jueves 17 a las 16:00. {URL}"
    assert evaluar(malo, datos) == "contradiccion"
    assert confirmar(malo, datos).startswith("Listo, tu cita quedó movida para jueves 17")


@pytest.mark.parametrize(
    "hoy,texto,esperado",
    [
        ("2026-09-15", f"Listo, mañana a las 10:30 te espero. {URL}", "contradiccion"),  # es hoy
        ("2026-09-14", f"Listo, hoy a las 10:30 te espero. {URL}", "contradiccion"),  # es mañana
        ("2026-09-14", f"Listo, mañana a las 10:30 te espero. {URL}", "ok"),
        ("2026-09-15", f"Listo, hoy a las 10:30 te espero. {URL}", "ok"),
        ("2026-09-10", f"Listo, mañana a las 10:30 te espero. {URL}", "contradiccion"),  # ni hoy ni mañana
    ],
)
def test_a6_hoy_y_manana(hoy, texto, esperado):
    assert evaluar(texto, _con_instante(hoy=hoy)) == esperado


def test_a6_hoy_y_manana_desde_la_etiqueta():
    assert evaluar(f"Listo, mañana a las 10:00 te espero. {URL}", _d("hoy viernes 7 de agosto, 10:00")) == "contradiccion"
    assert evaluar(f"Listo, hoy a las 10:00 te espero. {URL}", _d("mañana martes 15, 10:00")) == "contradiccion"


def test_a6_quince_minutos_antes_no_es_el_dia_15():
    texto = f"Te espero el martes 15 de septiembre a las 10:30. {URL}\nLlega 15 minutos antes, abrimos a las 9:00."
    assert evaluar(texto, _d()) == "ok"


def test_a6_cita_presencial_con_enlace_de_maps():
    datos = _d(url=None)
    texto = "Te espero el martes 15 de septiembre a las 10:30. Así llegas: https://maps.app.goo.gl/xyz"
    assert evaluar(texto, datos) == "ok"
    assert confirmar(texto, datos) == texto


def test_a6_con_enlace_real_otro_de_maps_no_es_contradiccion():
    texto = f"Te espero el martes 15 de septiembre a las 10:30. {URL} Parqueadero: https://maps.app.goo.gl/xyz"
    assert evaluar(texto, _d()) == "ok"


# ────────────────────────────────────────────────── M-4 (inglés) ───


def test_m4_respuesta_en_ingles_sale_tal_cual():
    texto = f"All set! Your appointment is on Tuesday, September 15 at 10:30 am. Join here: {URL}"
    assert evaluar(texto, _d()) == "ok"
    assert evaluar(f"See you tomorrow at 10:30 am. {URL}", _con_instante(hoy="2026-09-14")) == "ok"
    assert evaluar(f"See you on Tuesday, September 15 at 11:30 am. {URL}", _d()) == "contradiccion"


# ──────────────────────────────────────────────────────────── B-4 ───


def test_b4_tipo_desconocido_es_cita_nueva():
    assert plantilla(_d(tipo="otro")).startswith("Listo, tu cita quedó confirmada")
