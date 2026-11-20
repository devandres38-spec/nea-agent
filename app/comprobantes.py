"""vocero-crm 073-C (D5, decisión #4) — Un comprobante de pago nunca se da por bueno.

En Colombia el lead paga por Nequi, Daviplata o transferencia (Bancolombia,
PSE…) y manda la captura. Un pantallazo se edita en segundos, y el modelo
—que ve la imagen o lee «ya te pagué»— tendía a responder «¡pago recibido!».
Eso es confirmar dinero que nadie verificó.

Regla: si en el turno llega una imagen o un documento Y el lead habla de un
pago (en el texto o en el pie), o el lead dice que ya pagó/transfirió, el
turno termina en handoff OBLIGATORIO con la nota «posible comprobante —
validar en banco antes de confirmar». El modelo escribe su respuesta (el
chasis le prohíbe confirmar el pago); el handoff lo garantiza este módulo.
"""
from __future__ import annotations

import re
import unicodedata

NOTA = "Posible comprobante de pago — validar en banco antes de confirmar."

_HABLA_DE_PAGO = re.compile(
    r"\b(?:"
    r"comprobante|recibo de pago|soporte de pago|soporte del pago|pantallazo del pago|captura del pago"
    r"|nequi|daviplata|bancolombia|davivienda|pse|transfiya|bre-?b|llave bre-?b"
    r"|consignaci[oó]n|consign[eé]|transferencia|transfer[ií]|ya (?:te |les )?(?:pagu[eé]|transfer[ií]|consign[eé]|envi[eé] (?:el|la) (?:plata|pago|dinero))"
    r"|(?:ya )?(?:hice|realic[eé]) (?:el|la) (?:pago|transferencia|consignaci[oó]n)"
    r"|abono|anticipo"
    r")"
)
# Sin adjunto, solo cuenta si el lead AFIRMA que ya pagó (no si pregunta cómo).
_DICE_QUE_PAGO = re.compile(
    r"\b(?:ya (?:te |les )?(?:pagu[eé]|transfer[ií]|consign[eé])|(?:ya )?(?:hice|realic[eé]) (?:el|la) (?:pago|transferencia|consignaci[oó]n)|te (?:envi[eé]|mand[eé]) el comprobante)\b"
)


def posible_comprobante(textos: list[str], hay_adjunto: bool) -> bool:
    """¿Este turno trae (o anuncia) un comprobante de pago?"""
    juntos = " ".join(t for t in textos if t)
    plano = unicodedata.normalize("NFC", juntos.lower())
    if hay_adjunto and _HABLA_DE_PAGO.search(plano):
        return True
    return bool(_DICE_QUE_PAGO.search(plano))
