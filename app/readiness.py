"""Comprobación de ida y vuelta sin efectos en el CRM."""
import hashlib
import hmac
import json
import re
import time

import httpx
from fastapi import APIRouter, Request, Response

from app.dispatch import firma_valida
from app.http_limits import limited_body

router = APIRouter()
HEADER = "X-Vocero-Signature"


def firmar(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def trama(body: bytes, tipo: str) -> dict | None:
    try:
        dato = json.loads(body)
    except (ValueError, UnicodeError):
        return None
    if (
        not isinstance(dato, dict) or set(dato) != {"tipo", "nonce", "emitido"}
        or dato["tipo"] != tipo or type(dato["emitido"]) is not int
        or not isinstance(dato["nonce"], str) or not re.fullmatch(r"[a-f0-9]{64}", dato["nonce"])
        or abs(time.time() * 1000 - dato["emitido"]) > 30_000
    ):
        return None
    return dato


@router.post("/vocero/readiness")
async def readiness(request: Request) -> Response:
    settings = request.app.state.ctx.settings
    if not settings.multi_org or len(settings.brain_deployment_secret) < 16:
        return Response(status_code=409)
    secreto = settings.brain_deployment_secret
    body = await limited_body(request)
    if not firma_valida(body, request.headers.get(HEADER), secreto):
        return Response(status_code=401)
    desafio = trama(body, "desafio")
    if desafio is None:
        return Response(status_code=422)
    retorno = json.dumps({**desafio, "tipo": "retorno"}, separators=(",", ":")).encode()
    try:
        async with httpx.AsyncClient(timeout=5) as cliente:
            res = await cliente.post(
                settings.crm_base_url.rstrip("/") + "/api/brains/readiness",
                content=retorno,
                headers={"Content-Type": "application/json", HEADER: firmar(retorno, secreto)},
            )
    except httpx.HTTPError:
        return Response(status_code=502)
    if res.status_code == 401:
        return Response(status_code=401)
    if (res.status_code != 200 or not firma_valida(res.content, res.headers.get(HEADER), secreto)
            or trama(res.content, "prueba") != {**desafio, "tipo": "prueba"}):
        return Response(status_code=502)
    confirmacion = json.dumps({**desafio, "tipo": "confirmacion"}, separators=(",", ":")).encode()
    return Response(confirmacion, media_type="application/json", headers={HEADER: firmar(confirmacion, secreto)})
