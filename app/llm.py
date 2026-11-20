"""Capa LLM: OpenAI chat.completions con tool-calling y extracción tolerante.

Gotchas del brief que se honran aquí:
- `content` vacío con tool_calls es NORMAL (turno solo-herramientas).
- Respuesta vacía de verdad (sin content ni tool_calls) o excepción → reintento
  con backoff (2 reintentos). Agotado → `LlmExhausted` y el turno degrada en
  silencio + handoff error (Constitución IV).
- Cada intento tiene tope (`LLM_TIMEOUT_SECONDS`) y el SDK no reintenta por su
  cuenta: los reintentos son estos, no se multiplican.
- Los `arguments` de las tools pueden venir malformados: JSON inválido → {}.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import random
import re
import time
from app.observabilidad import despacho_actual
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from openai import AsyncOpenAI

logger = logging.getLogger("nea.llm")


class LlmExhausted(Exception):
    """El LLM falló todos los reintentos — el turno debe degradar en silencio."""

    # 073-A (D2) — Con qué motivo pasa el turno la conversación a una persona.
    motivo_handoff: str = "error"


# 073-A (D2) — Lo que el proveedor contesta con estos códigos no cambia por
# reintentar: la llave no tiene saldo (402), no vale (401/403) o la petición
# está mal (400/422, p. ej. contexto demasiado largo). Antes se reintentaban
# tres veces por turno, con la llave del negocio.
_CAUSA_POR_STATUS = {
    400: "peticion_invalida",
    401: "credencial",
    402: "sin_saldo",
    403: "credencial",
    422: "peticion_invalida",
}

# 073-A (D2) — Espera máxima que se le concede a un `Retry-After`: el lead
# está mirando «escribiendo…».
RETRY_AFTER_MAX_SEGUNDOS = 10.0


class LlmRechazado(LlmExhausted):
    """073-A (D2) — El proveedor rechazó la llamada de forma terminal."""

    def __init__(self, status: int, causa: str, detalle: str) -> None:
        super().__init__(f"{status} {causa}: {detalle}")
        self.status = status
        self.causa = causa
        # Sin saldo (o sin credencial en el CRM) es lo que el dueño tiene que
        # leer en su bandeja; «modelo» o «error» le ocultaban la causa.
        self.motivo_handoff = "sin_saldo" if causa == "sin_saldo" else "error"


def _status_de(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _retry_after(exc: Exception) -> float | None:
    """Segundos de `Retry-After` (segundos o fecha HTTP), acotados a
    `[0, RETRY_AFTER_MAX_SEGUNDOS]`. Ilegible o no finito → None."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    crudo = str(headers.get("retry-after", "") or "").strip()
    if not crudo:
        return None
    try:
        valor = float(crudo)
    except ValueError:
        valor = _segundos_hasta(crudo)
        if valor is None:
            return None
    if not math.isfinite(valor):
        return None
    return min(max(valor, 0.0), RETRY_AFTER_MAX_SEGUNDOS)


def _segundos_hasta(fecha_http: str) -> float | None:
    """`Retry-After: Wed, 21 Oct 2026 07:28:00 GMT` → segundos desde ahora."""
    try:
        cuando = parsedate_to_datetime(fecha_http)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if cuando is None:
        return None
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=timezone.utc)
    return (cuando - datetime.now(timezone.utc)).total_seconds()


class ImagenNoSoportada(LlmExhausted):
    """vocero-crm 071 (D12) — El modelo de este negocio no acepta imágenes.

    Hereda de `LlmExhausted` para que quien no la conozca degrade como
    siempre; el turno la atiende antes: quita la imagen, avisa con honestidad
    y vuelve a pensar UNA vez, sin handoff `error`.
    """


# Cómo dicen los proveedores «este modelo no ve». Reintentar no lo cambia:
# antes eran 3 intentos, silencio y handoff `error` por mandar una foto.
_SIN_VISION = (
    "support image input",
    "image input is not supported",
    "image_url is only supported",
    "does not support image",
    "doesn't support image",
    "image inputs are not supported",
    "vision is not supported",
    "model does not support images",
)


def _es_sin_vision(exc: Exception) -> bool:
    texto = str(exc).lower()
    return any(p in texto for p in _SIN_VISION)


def tiene_imagenes(messages: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages
    )


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LlmReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class Llm(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmReply: ...

    async def transcribe(
        self, data: bytes, mime: str, filename: str = "audio.ogg"
    ) -> str: ...


# Los proveedores compatibles piden el formato aparte del binario. WhatsApp
# manda notas de voz en OGG/Opus; el resto se deduce del mime y, si no se
# reconoce, se manda como ogg en vez de fallar antes de intentarlo.
_FORMATOS = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
    "audio/aac": "aac",
}


def _formato_de_audio(mime: str) -> str:
    return _FORMATOS.get((mime or "").split(";")[0].strip().lower(), "ogg")


# Segundos por intento, el default de LLM_TIMEOUT_SECONDS (ver config.py).
DEFAULT_TIMEOUT = 45.0

# 2.13.1 — Tope de tokens de salida por llamada, el default de
# LLM_MAX_TOKENS (ver config.py). Sin tope, una salida desbocada se cobra
# entera de la llave del negocio; demasiado bajo, trunca a media llamada de
# herramienta (JSON roto → `{}`) o el razonamiento. 0 = no se manda.
DEFAULT_MAX_TOKENS = 2048

# 073-A (D2) — `temperature` por defecto (LLM_TEMPERATURE). La fija la
# plataforma; el cliente final no la ve.
DEFAULT_TEMPERATURE = 0.4

# 2.13.1 — Cuánto duran apagados los ajustes de velocidad tras una queja del
# proveedor. Antes era para siempre (hasta reiniciar el contenedor).
EXTRAS_APAGADOS_SEGUNDOS = 3600.0

# 2.13.1 — Una queja de los ajustes NOMBRA el parámetro. «Upstream provider
# returned 502» o «Provider returned error» dicen `provider` y no hablan del
# parámetro: con la regla de antes, un 502 pasajero de OpenRouter apagaba los
# ajustes para siempre.
_NOMBRA_LOS_EXTRAS = re.compile(
    r"reasoning"
    r"|temperature"
    r"|provider\.sort"
    r"|[\"'`]provider[\"'`]"
    r"|[\"'`]sort[\"'`]"
    r"|\b(?:provider|sort) (?:field|param|parameter|option|preferences?)\b",
    re.IGNORECASE,
)


def _espera(intento: int) -> float:
    """Backoff con jitter: 1–1.5 s, 2–3 s. Sin jitter, los turnos que fallaron
    a la vez reintentan a la vez contra el mismo proveedor."""
    base = float(2**intento)
    return base + random.uniform(0, base * 0.5)


def _corto_por_el_tope(resp: Any) -> bool:
    """¿La respuesta se cortó por `max_tokens`?"""
    choices = getattr(resp, "choices", None) or []
    return bool(choices) and getattr(choices[0], "finish_reason", None) == "length"


def _es_queja_de_los_extras(exc: Exception) -> bool:
    """¿El proveedor rechazó la petición (400) por `reasoning`/`provider`?

    073-A — También por `temperature`: va con los ajustes y se apaga con ellos.

    Solo un 400: un 429 o un 5xx no dicen nada de si el modelo acepta los
    ajustes, y un error sin código HTTP (red, timeout) tampoco.
    """
    if getattr(exc, "status_code", None) != 400:
        return False
    return bool(_NOMBRA_LOS_EXTRAS.search(str(exc)))


class OpenAiLlm:
    RETRIES = 2  # además del intento inicial

    def __init__(
        self,
        api_key: str,
        model: str,
        transcribe_model: str = "whisper-1",
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        reasoning_effort: str = "",
        provider_sort: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        http_client: Any | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = DEFAULT_TEMPERATURE,
        cabecera_despacho: bool = False,
    ) -> None:
        # base_url ≠ None → proveedor OpenAI-compatible (p. ej. OpenRouter,
        # para el bench de modelos del 002). Ojo: la transcripción de audio
        # es una API de OpenAI — con otro proveedor degrada honesta
        # (LlmExhausted → fallback), por eso el bench alterno cubre texto.
        # `default_headers` lo usa el modo multi-organización: cuando quien
        # piensa es el CRM (y no OpenRouter directo), hay que decirle de qué
        # organización es cada llamada.
        #
        # `timeout` y `max_retries=0` van juntos. Por defecto el SDK espera
        # hasta 600 s por petición y reintenta dos veces más POR SU CUENTA,
        # así que cada intento de abajo eran hasta tres peticiones de diez
        # minutos. Los reintentos son de Nea (RETRIES + backoff en `complete`,
        # dos intentos en `transcribe`): un timeout es una excepción más, cae
        # en ese mismo camino y, agotado, termina en `LlmExhausted` → silencio
        # + handoff `error`, como cualquier otro fallo del proveedor.
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
            timeout=timeout,
            max_retries=0,
            # 071 (D3) — En multi-organización todos los modelos se piensan por
            # el CRM (`/api/brains/llm`): mismo host que el resto. Comparten el
            # transporte del registro en vez de abrir un pool por modelo que
            # nadie cerraba al cambiar de modelo.
            **({"http_client": http_client} if http_client is not None else {}),
        )
        self._model = model
        self._transcribe_model = transcribe_model
        # Los dos ajustes de velocidad. Se deciden una vez, aqui, y viajan en
        # cada llamada; `_extras_ok` los apaga solos si el proveedor se queja.
        self._reasoning_effort = reasoning_effort.strip()
        self._provider_sort = provider_sort.strip()
        # 2.13.1 — Hasta cuándo (reloj monotónico) siguen apagados. None =
        # encendidos. Caduca: el proveedor detrás de OpenRouter cambia.
        self._extras_apagados_hasta: float | None = None
        # 2.13.1 — Tope de salida por llamada de chat (no por transcripción).
        self._max_tokens = max(0, int(max_tokens or 0))
        # 073-A (D2) — Sin fijarla el proveedor usa 1.0: más variación de la
        # que una conversación de ventas aguanta. None = no se manda.
        self._temperature = temperature
        # 071 (D12) — ¿El modelo ve imágenes? Se aprende del primer rechazo y
        # es pegajoso, como `_extras_ok`: el soporte no cambia a media vida.
        self._vision_ok = True
        # Con proveedor propio (OpenRouter y compañía) no hay endpoint de
        # transcripción: el audio se manda DENTRO del chat, a un modelo que
        # sepa oír. Se decide aquí y no en cada llamada para que el camino sea
        # el mismo durante toda la vida del proceso.
        self._audio_por_chat = base_url is not None
        # Contadores de uso (para el bench de costos del 002): tokens reales
        # reportados por el proveedor, acumulados por instancia.
        self.usage = {"prompt": 0, "cached": 0, "completion": 0, "llamadas": 0}
        # vocero-crm 074 (E8) — Solo cuando quien recibe es el CRM (su proxy
        # registra el uso por despacho en `llm_usage`): a un proveedor externo
        # no se le cuenta qué despacho es.
        self._cabecera_despacho = cabecera_despacho

    def _cabeceras_del_turno(self) -> dict[str, Any]:
        """`extra_headers` con `X-Vocero-Dispatch` del turno en curso (o nada)."""
        if not self._cabecera_despacho:
            return {}
        despacho = despacho_actual()
        return {"extra_headers": {"X-Vocero-Dispatch": despacho}} if despacho else {}

    @property
    def puede_transcribir(self) -> bool:
        """073-C (D5): hay modelo que oye; sin él, no se le piden audios al lead."""
        return bool(self._transcribe_model)

    async def transcribe(
        self, data: bytes, mime: str, filename: str = "audio.ogg"
    ) -> str:
        """Audio → texto. Vacío o fallo → LlmExhausted.

        Dos caminos, porque los proveedores no ofrecen lo mismo:

        - **OpenAI**: endpoint propio de transcripción (whisper).
        - **Compatible** (OpenRouter…): no existe ese endpoint. El audio va
          codificado DENTRO de un mensaje de chat, a un modelo que acepte
          audio. Ojo: el modelo que conversa y el que oye no tienen por qué
          ser el mismo — hoy los GLM, por ejemplo, no oyen.
        """
        # Sin modelo que oiga no se intenta siquiera. Mandar el audio al que
        # conversa devolvería una alucinación con pinta de transcripción, que
        # es peor que decir que no se pudo: quien la lee no sabe que es falsa.
        if not self._transcribe_model:
            raise LlmExhausted(
                "sin modelo de transcripción configurado — no se transcribe"
            )
        if self._audio_por_chat:
            return await self._transcribir_por_chat(data, mime)
        last_error: Exception | None = None
        content_type = (mime or "audio/ogg").split(";")[0].strip()
        for attempt in range(2):
            try:
                resp = await self._client.audio.transcriptions.create(
                    **self._cabeceras_del_turno(),
                    model=self._transcribe_model,
                    file=(filename, data, content_type),
                    language="es",
                )
                text = (getattr(resp, "text", None) or "").strip()
                if text:
                    return text
                last_error = ValueError("transcripción vacía")
                logger.warning("transcribe: texto vacío, intento %d", attempt + 1)
            except Exception as exc:
                last_error = exc
                logger.warning("transcribe: fallo en intento %d: %s", attempt + 1, exc)
            if attempt == 0:
                await asyncio.sleep(1.0)
        raise LlmExhausted(str(last_error))

    async def _transcribir_por_chat(self, data: bytes, mime: str) -> str:
        """El audio, en base64, dentro de un mensaje de chat.

        Es como transcriben los proveedores compatibles: no hay endpoint de
        audio, hay modelos que oyen. Se le pide el texto y NADA más — sin esa
        instrucción, un modelo servicial contesta a lo que dijo el cliente en
        vez de transcribirlo, y Nea acabaría respondiendo a su propia
        paráfrasis.
        """
        fmt = _formato_de_audio(mime)
        b64 = base64.b64encode(data).decode()
        last_error: Exception | None = None

        for attempt in range(2):
            try:
                resp = await self._client.chat.completions.create(
                    **self._cabeceras_del_turno(),
                    model=self._transcribe_model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "Transcribe este audio al español. "
                                        "Responde SOLO con la transcripción "
                                        "literal, sin comillas, sin comentarios "
                                        "y sin responder a lo que dice."
                                    ),
                                },
                                {
                                    "type": "input_audio",
                                    "input_audio": {"data": b64, "format": fmt},
                                },
                            ],
                        }
                    ],
                )
                text = (resp.choices[0].message.content or "").strip()
                if text:
                    return text
                last_error = ValueError("transcripción vacía")
                logger.warning("transcribe(chat): vacío, intento %d", attempt + 1)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "transcribe(chat) con %s: fallo en intento %d: %s",
                    self._transcribe_model,
                    attempt + 1,
                    exc,
                )
            if attempt == 0:
                await asyncio.sleep(1.0)
        raise LlmExhausted(str(last_error))

    def _extras(self) -> dict[str, Any]:
        """Los ajustes que no son del contrato de OpenAI.

        Van por `extra_body` a proposito: OpenRouter los entiende y un
        proveedor que no, los ignora en vez de fallar.

        OJO con `reasoning: {"exclude": true}`: parece lo mismo que "minimal"
        y no lo es. Oculta el razonamiento de la respuesta pero el modelo lo
        SIGUE generando - medido en huaraches el 8-sep: 650 tokens y 19.7 s de
        mediana contra 67 y 7.7 s con "minimal". Que nadie lo "mejore".
        """
        if not self._extras_ok:
            return {}
        extra: dict[str, Any] = {}
        if self._reasoning_effort:
            extra["reasoning"] = {"effort": self._reasoning_effort}
        if self._provider_sort:
            extra["provider"] = {"sort": self._provider_sort}
        return extra

    @property
    def _extras_ok(self) -> bool:
        """¿Se mandan hoy los ajustes de velocidad? Se reencienden solos."""
        hasta = self._extras_apagados_hasta
        if hasta is None:
            return True
        if time.monotonic() >= hasta:
            self._extras_apagados_hasta = None
            logger.info("llm: vuelvo a probar los ajustes de velocidad con %s", self._model)
            return True
        return False

    def _quiza_por_los_extras(self, exc: Exception) -> bool:
        """Si el proveedor rechazó `reasoning` o `provider`, se apagan un rato.

        Aqui pasan modelos de muchos miembros, no uno solo elegido por
        nosotros. Bajar el tiempo de respuesta es una optimizacion, y una
        optimizacion no puede costar la respuesta: al primer reproche este
        cliente deja de mandarlos y el reintento sale limpio.

        2.13.1 — Solo un 400 que NOMBRA el parámetro (`_es_queja_de_los_extras`)
        y solo por `EXTRAS_APAGADOS_SEGUNDOS`: antes cualquier error con la
        palabra «provider» —un 502 o un 429 pasajero de OpenRouter— los
        apagaba hasta reiniciar el contenedor. Sigue siendo por organización,
        porque cada una tiene su cliente.
        """
        if not self._extras_ok or not _es_queja_de_los_extras(exc):
            return False
        self._extras_apagados_hasta = time.monotonic() + EXTRAS_APAGADOS_SEGUNDOS
        logger.warning(
            "llm: %s no acepta los ajustes de velocidad - se apagan %d min para "
            "este modelo y se reintenta sin ellos",
            self._model,
            int(EXTRAS_APAGADOS_SEGUNDOS // 60),
        )
        return True

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmReply:
        if not self._vision_ok and tiene_imagenes(messages):
            raise ImagenNoSoportada(f"{self._model} no acepta imágenes (ya se sabía)")
        last_error: Exception | None = None
        # 2.13.1 — El tope de ESTA llamada. Si el modelo choca con él
        # (`finish_reason=length`) lo que trae está cortado —una herramienta
        # con el JSON a medias, una frase sin terminar—: se repite UNA vez con
        # el doble en vez de mandarlo así. Sigue acotado (ver config.py).
        tope = self._max_tokens
        attempt = 0
        while attempt <= self.RETRIES:
            try:
                kwargs: dict[str, Any] = {}
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                if tope:
                    kwargs["max_tokens"] = tope
                if self._temperature is not None and self._extras_ok:
                    kwargs["temperature"] = self._temperature
                extra = self._extras()
                if extra:
                    kwargs["extra_body"] = extra
                kwargs.update(self._cabeceras_del_turno())
                resp = await self._client.chat.completions.create(
                    model=self._model, messages=messages, **kwargs
                )
                u = getattr(resp, "usage", None)
                if u is not None:
                    det = getattr(u, "prompt_tokens_details", None)
                    self.usage["llamadas"] += 1
                    self.usage["prompt"] += getattr(u, "prompt_tokens", 0) or 0
                    self.usage["completion"] += getattr(u, "completion_tokens", 0) or 0
                    self.usage["cached"] += getattr(det, "cached_tokens", 0) or 0
                if tope and tope == self._max_tokens and _corto_por_el_tope(resp):
                    tope = self._max_tokens * 2
                    logger.warning(
                        "llm: %s chocó con el tope de %d tokens — repito con %d",
                        self._model,
                        self._max_tokens,
                        tope,
                    )
                    continue  # sin espera ni gastar intento: no es un fallo
                reply = self._parse(resp)
                if reply.content or reply.tool_calls:
                    return reply
                last_error = ValueError("respuesta vacía del LLM (sin content ni tools)")
                logger.warning("llm: respuesta vacía, intento %d", attempt + 1)
            except Exception as exc:  # red, API, parseo
                if tiene_imagenes(messages) and _es_sin_vision(exc):
                    self._vision_ok = False
                    logger.warning("llm: %s no acepta imágenes — sin reintentos", self._model)
                    raise ImagenNoSoportada(str(exc)) from exc
                last_error = exc
                status = _status_de(exc)
                if self._quiza_por_los_extras(exc):
                    # El 400 era por los ajustes: el reintento sale sin ellos.
                    pausa = None
                elif status in _CAUSA_POR_STATUS:
                    # 073-A (D2): terminal. Reintentar solo cobraba de nuevo.
                    causa = _CAUSA_POR_STATUS[status]
                    logger.warning("llm: %s rechazó la llamada (%d %s) — sin reintentos", self._model, status, causa)
                    raise LlmRechazado(status, causa, str(exc)[:300]) from exc
                else:
                    pausa = _retry_after(exc)
                logger.warning("llm: fallo en intento %d: %s", attempt + 1, exc)
            else:
                pausa = None
            if attempt < self.RETRIES:
                await asyncio.sleep(pausa if pausa is not None else _espera(attempt))
            attempt += 1
        raise LlmExhausted(str(last_error))

    @staticmethod
    def _parse(resp: Any) -> LlmReply:
        """Extracción tolerante: nunca truena por formato inesperado."""
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return LlmReply(content=None)
        message = getattr(choices[0], "message", None)
        if message is None:
            return LlmReply(content=None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            content = content.strip() or None
        else:
            content = None
        tool_calls: list[ToolCall] = []
        for tc in getattr(message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None)
            if not name:
                continue
            raw_args = getattr(fn, "arguments", None) or "{}"
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    args = {}
            except (TypeError, ValueError):
                logger.warning("llm: arguments malformados en %s — uso {}", name)
                args = {}
            tool_calls.append(
                ToolCall(id=getattr(tc, "id", "") or "", name=name, arguments=args)
            )
        return LlmReply(content=content, tool_calls=tool_calls)


# 036 — La ruta del proxy del CRM cuando el perfil no la dice.
RUTA_LLM_DEL_CRM = "/api/bot/llm"


class LlmPorElCrm:
    """El modelo, llamado a través del CRM (spec 036 D6 de vocero-crm).

    Junto a Vocero raíz la llave del modelo vive en Ajustes → IA del CRM, no
    aquí. `POST {CRM}/api/bot/llm/*` es compatible con OpenAI: le pone esa
    llave y reenvía el cuerpo tal cual. Nea entra con `CRM_BOT_API_KEY` en
    `Authorization: Bearer`, que es lo que manda el SDK.

    El modelo lo elige el negocio en el CRM y llega en el perfil (`llm.model`);
    sin él, `LLM_MODEL`. Por eso este cliente no es un `OpenAiLlm` fijo: en
    cada llamada mira el perfil —que ya está en caché con su TTL, no es un
    viaje más al CRM— y usa el `OpenAiLlm` de ESE modelo. Cambiar el modelo en
    el CRM llega sin reiniciar Nea, como el resto del perfil. Se cachea uno
    por (modelo, ruta), igual que `RegistroDeOrganizaciones.llm`: cada uno
    tiene su pool de conexiones y su memoria de `_extras_ok`.

    El modelo que TRANSCRIBE es el de `LLM_TRANSCRIBE_MODEL`, por el mismo
    proxy: el audio viaja dentro del chat (`_audio_por_chat`).

    Si el CRM no tiene IA (402 `sin_credencial`) o el proveedor no contesta
    (504), el SDK lo levanta como cualquier otro fallo: reintentos y, agotados,
    `LlmExhausted` → silencio + handoff `error`. Nada nuevo que cablear.
    """

    def __init__(
        self,
        crm_base_url: str,
        api_key: str,
        perfil: Any,
        *,
        model: str,
        transcribe_model: str = "whisper-1",
        reasoning_effort: str = "",
        provider_sort: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = DEFAULT_TEMPERATURE,
    ) -> None:
        self._crm = crm_base_url.rstrip("/")
        self._api_key = api_key
        self._perfil = perfil  # ProfileProvider (o None en pruebas)
        self._model = model
        self._transcribe_model = transcribe_model
        self._reasoning_effort = reasoning_effort
        self._provider_sort = provider_sort
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._clientes: dict[tuple[str, str, float | None], OpenAiLlm] = {}

    async def _eleccion(self) -> tuple[str, str, float | None]:
        """(modelo, base_url) según el perfil vigente.

        Un perfil que no se pudo leer no tumba el turno: se piensa con
        `LLM_MODEL` por la ruta de siempre, y si el CRM tampoco contesta ahí,
        es el camino de fallo de siempre.
        """
        model: str | None = None
        path: str | None = None
        temperatura: float | None = None
        if self._perfil is not None:
            try:
                perfil = await self._perfil.get()
                model = getattr(perfil, "llm_model", None)
                path = getattr(perfil, "llm_path", None)
                temperatura = getattr(perfil, "llm_temperature", None)
            except Exception as exc:
                logger.warning("llm por el CRM: sin perfil (%s) — uso LLM_MODEL", exc)
        return (
            model or self._model,
            self._crm + (path or RUTA_LLM_DEL_CRM),
            temperatura if temperatura is not None else self._temperature,
        )

    async def cliente(self) -> OpenAiLlm:
        """El `OpenAiLlm` del modelo que el negocio tiene elegido ahora."""
        clave = await self._eleccion()
        cliente = self._clientes.get(clave)
        if cliente is None:
            model, base_url, temperatura = clave
            cliente = OpenAiLlm(
                self._api_key,
                model,
                transcribe_model=self._transcribe_model,
                base_url=base_url,
                reasoning_effort=self._reasoning_effort,
                provider_sort=self._provider_sort,
                timeout=self._timeout,
                max_tokens=self._max_tokens,
                temperature=temperatura,
                cabecera_despacho=True,
            )
            self._clientes[clave] = cliente
            logger.info("llm por el CRM: pienso con %s", model)
        return cliente

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmReply:
        return await (await self.cliente()).complete(messages, tools)

    @property
    def puede_transcribir(self) -> bool:
        """073-C (D5): hay modelo que oye; sin él, no se le piden audios al lead."""
        return bool(self._transcribe_model)

    async def transcribe(
        self, data: bytes, mime: str, filename: str = "audio.ogg"
    ) -> str:
        return await (await self.cliente()).transcribe(data, mime, filename)
