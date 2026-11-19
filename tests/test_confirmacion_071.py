"""vocero-crm 071 (D17) — Guardarraíl de la confirmación de cita.

Banco de casos: ok (sale tal cual), pie (se añade lo que falta) y
contradicción (plantilla con el trato del negocio). Formatos de 12 y 24 h,
«hoy», «mañana», con y sin enlace, cita nueva y movida.
"""
from __future__ import annotations

import pytest

from app.confirmacion import confirmar, evaluar, leer_cita, plantilla

URL = "https://meet.google.com/abc-defg-hij"


def _d(label: str, url: str | None = URL, **extra) -> dict:
    return {"tipo": "book", "label": label, "meeting_url": url, "link_pending": False, "reminder_consent": False, **extra}


VIERNES = "viernes 18 de septiembre, 10:00 am"
TARDE = "jueves 20 de agosto, 3:30 pm"
MANANA = "mañana martes 15, 10:00"
HOY = "hoy viernes 7 de agosto, 16:00"
CON_PROFESIONAL = "lunes 21 de septiembre, 9:00 con Dra. Ana"

CASOS = [
    # ── ok: fecha + hora + enlace, en la voz del modelo ──────────────────
    ("ok", VIERNES, f"¡Listo, Gerardo! Te espero el viernes 18 de septiembre a las 10:00. Entra aquí: {URL}"),
    ("ok", VIERNES, f"Quedó agendada para el 18 de septiembre a las 10 am 🙌 {URL}"),
    ("ok", VIERNES, f"Agendado: viernes 18, 10:00 a. m. Enlace: {URL}"),
    ("ok", VIERNES, f"Perfecto, nos vemos el viernes 18 de septiembre a las 10 de la mañana. {URL}"),
    ("ok", TARDE, f"Listo, jueves 20 de agosto a las 3:30 pm. {URL}"),
    ("ok", TARDE, f"Te agendé el 20 de agosto a las 15:30. {URL}"),
    ("ok", TARDE, f"Quedó el jueves 20 a las 3 y media. {URL}"),
    ("ok", MANANA, f"¡Hecho! Te espero mañana a las 10:00. {URL}"),
    ("ok", MANANA, f"Listo, mañana martes 15 a las 10 am. {URL}"),
    ("ok", HOY, f"Listo, hoy a las 4 pm te esperamos. {URL}"),
    ("ok", HOY, f"Agendado para hoy a las 16:00. {URL}"),
    ("ok", CON_PROFESIONAL, f"Listo: lunes 21 de septiembre a las 9:00 con la Dra. Ana. {URL}"),
    ("ok", VIERNES, f"Te espero el viernes 18 de septiembre a las 10:00. {URL} Abrimos desde las 8:00."),
    # ── pie: falta algo ──────────────────────────────────────────────────
    ("pie", VIERNES, "¡Listo, Gerardo! Ya quedó tu cita 🙌"),
    ("pie", VIERNES, f"¡Agendado! {URL}"),
    ("pie", VIERNES, "Te espero el viernes 18 de septiembre a las 10:00."),  # sin enlace
    ("pie", VIERNES, f"Nos vemos el viernes a las 10:00. {URL}"),  # sin día del mes
    ("pie", MANANA, f"Listo, mañana te espero. {URL}"),  # sin hora
    ("pie", TARDE, f"Quedó para el 20 de agosto. {URL}"),  # sin hora
    ("pie", HOY, f"Te esperamos más tarde. {URL}"),
    ("pie", MANANA, f"Te espero a las 10 de la mañana. {URL}"),  # «de la mañana» no es el día
    ("pie", VIERNES, f"Listo. {URL} Por cierto, abrimos a las 8:00."),  # R3: otra oración
    # ── contradicción: otra hora, otro día, enlace mal ───────────────────
    ("contradiccion", VIERNES, f"Te espero el viernes 18 de septiembre a las 11:00. {URL}"),
    ("contradiccion", VIERNES, f"Quedó el 19 de septiembre a las 10:00. {URL}"),
    ("contradiccion", VIERNES, f"Listo, el jueves 18 de septiembre a las 10:00. {URL}"),
    ("contradiccion", TARDE, f"Te agendé el jueves 20 de agosto a las 3:00 pm. {URL}"),
    ("contradiccion", TARDE, f"Quedó el 20 de agosto a las 4 pm. {URL}"),
    ("contradiccion", MANANA, f"Listo, mañana a las 11:00. {URL}"),
    ("contradiccion", HOY, f"Hoy a las 5 pm. {URL}"),
    ("contradiccion", VIERNES, "Te mandaré el enlace después"),  # habla del enlace sin darlo
    ("contradiccion", VIERNES, "Listo, el 18 de septiembre a las 10:00. https://zoom.us/j/otro"),  # otro enlace
    ("contradiccion", VIERNES, ""),
]


@pytest.mark.parametrize("esperado,label,texto", CASOS)
def test_banco_de_casos(esperado, label, texto):
    assert evaluar(texto, _d(label)) == esperado


def test_el_banco_tiene_al_menos_30_casos():
    assert len(CASOS) >= 30
    assert {c[0] for c in CASOS} == {"ok", "pie", "contradiccion"}


def test_leer_la_etiqueta():
    c = leer_cita(TARDE)
    assert (c.dia, c.mes, c.semana, c.hora, c.minuto) == (20, "agosto", "jueves", 15, 30)
    c = leer_cita(MANANA)
    assert (c.dia, c.mes, c.semana, c.relativo, c.hora) == (15, None, "martes", "manana", 10)
    c = leer_cita("lun 20 jul, 10:00")
    assert (c.dia, c.mes, c.semana, c.hora) == (20, "julio", "lunes", 10)


# ─────────────────────────────────────────────────────────── salidas ───


def test_ok_sale_tal_cual():
    texto = f"¡Listo, Gerardo! Te espero el viernes 18 de septiembre a las 10:00. {URL}"
    assert confirmar(texto, _d(VIERNES)) == texto


def test_el_pie_anade_solo_lo_que_falta():
    sale = confirmar("¡Listo, Gerardo! Ya quedó tu cita 🙌", _d(VIERNES))
    assert sale == f"¡Listo, Gerardo! Ya quedó tu cita 🙌\n\n📅 {VIERNES}\nEnlace de la reunión: {URL}"
    sale = confirmar("Te espero el viernes 18 de septiembre a las 10:00.", _d(VIERNES))
    assert "📅" not in sale and sale.endswith(f"Enlace de la reunión: {URL}")


def test_el_pie_respeta_el_trato_en_el_enlace_pendiente_y_los_recordatorios():
    datos = _d(VIERNES, url=None, link_pending=True, reminder_consent=True)
    sale = confirmar("Listo, el viernes 18 de septiembre a las 10:00.", datos, "usted")
    assert "le llegará por aquí" in sale and "que autorizó" in sale
    sale = confirmar("Listo, el viernes 18 de septiembre a las 10:00.", datos, "neutro")
    assert "llegará por aquí" in sale and " te " not in sale and " le " not in sale


def test_la_contradiccion_cae_a_la_plantilla_con_el_trato():
    malo = f"Te espero el viernes 18 de septiembre a las 11:00. {URL}"
    assert confirmar(malo, _d(VIERNES), "tu").startswith("Listo, tu cita quedó confirmada para")
    assert confirmar(malo, _d(VIERNES), "usted").startswith("Listo, su cita quedó confirmada para")
    assert confirmar(malo, _d(VIERNES), "neutro").startswith("Listo: la cita quedó confirmada para")
    assert f"Enlace de la reunión: {URL}" in confirmar(malo, _d(VIERNES))


def test_la_cita_movida_tiene_su_plantilla():
    datos = _d(VIERNES, tipo="reschedule")
    assert plantilla(datos).startswith("Listo, tu cita quedó movida para viernes 18")
    assert plantilla(datos, "usted").startswith("Listo, su cita quedó movida")


def test_un_trato_desconocido_es_tu():
    assert plantilla(_d(VIERNES), "vos").startswith("Listo, tu cita")


def test_sin_enlace_ni_pendiente_no_se_inventa_uno():
    datos = _d(VIERNES, url=None)
    assert evaluar("Listo, el viernes 18 de septiembre a las 10:00.", datos) == "ok"
    assert evaluar("Listo, el viernes 18 de septiembre a las 10:00. https://inventado.test", datos) == "contradiccion"


async def test_reprogramar_pasa_por_el_guardarrail(respx_mock):
    """Antes la cita movida salía con el texto del modelo sin revisar."""
    from datetime import datetime, timezone

    import httpx

    from app.state import Conversation, OfferedSlot
    from app.tools import ToolRuntime
    from tests.conftest import CRM_URL, make_ctx

    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation("573001112233")
    inicio = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)
    await ctx.store.replace_offered_slots(
        conv.id, [OfferedSlot(conversation_id=conv.id, start_utc=inicio, end_utc=None, label=VIERNES)]
    )
    respx_mock.patch(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(200, json={"bookingId": "bk_1", "meetingLink": URL, "label": "vie 18 sep, 10:00"})
    )
    rt = ToolRuntime(ctx, Conversation(id=conv.id, wa_identity="573001112233"), "cv_1")
    r = await rt.execute(
        "reschedule_session", {"start_utc": "2026-09-18T15:00:00Z", "dia_confirmado": "sí, el viernes 18"}
    )
    assert r["ok"] is True, r
    assert rt.booking_confirmation["tipo"] == "reschedule"
    assert rt.finalize_reply("Listo, la moví al jueves 17 de septiembre a las 10:00.").startswith(
        "Listo, tu cita quedó movida para viernes 18 de septiembre"
    )
    await ctx.crm.aclose()
