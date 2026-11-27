import json
import time

from app.dispatch import firma_valida
from app.readiness import firmar, trama


def test_readiness_firma_frescura_y_tipo():
    clave = "secreto-gemelo-080"
    body = json.dumps({"tipo": "desafio", "nonce": "a" * 64, "emitido": int(time.time() * 1000)}).encode()
    assert firma_valida(body, firmar(body, clave), clave)
    assert not firma_valida(body, firmar(body, clave), "incorrecta")
    assert trama(body, "desafio")
    assert trama(body, "retorno") is None
