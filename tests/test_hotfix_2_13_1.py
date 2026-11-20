"""2.13.1 — Regresiones de los hotfixes del bot (P-3 y §2.1 del diagnóstico).

- Los ajustes de velocidad (`reasoning`, `provider.sort`) solo se apagan por
  un 400 que NOMBRA el parámetro, y no para siempre: una hora.
- `max_tokens` alto y configurable; si la salida choca con él, se repite UNA
  vez con el doble en vez de mandar algo cortado.
- `has_knowledge` mira el KB, no las instrucciones.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from app import llm as llm_mod
from app.config import Settings
from app.llm import EXTRAS_APAGADOS_SEGUNDOS, LlmExhausted, OpenAiLlm
from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.state import Conversation

pytestmark = pytest.mark.anyio

URL = "https://prov.test/chat/completions"


def _ok(texto: str = "hola", finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": texto},
                    "finish_reason": finish,
                }
            ]
        },
    )


def _cliente(**kw) -> OpenAiLlm:
    return OpenAiLlm(
        "k",
        "z-ai/glm-5.3-flash",
        base_url="https://prov.test",
        reasoning_effort="minimal",
        provider_sort="throughput",
        **kw,
    )


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch: pytest.MonkeyPatch) -> None:
    async def nada(_s: float) -> None:
        return None

    monkeypatch.setattr(llm_mod.asyncio, "sleep", nada)


class TestLosExtrasSoloSeApaganPorUnaQuejaDeVerdad:
    @pytest.mark.parametrize(
        ("status", "mensaje"),
        [
            (502, "Upstream provider returned 502"),
            (429, "Provider is rate limited, retry shortly"),
            (503, "No provider available for this model"),
        ],
    )
    @respx.mock
    async def test_un_error_que_dice_provider_no_los_apaga(
        self, status: int, mensaje: str
    ) -> None:
        ruta = respx.post(URL).mock(
            side_effect=[
                httpx.Response(status, json={"error": {"message": mensaje}}),
                _ok(),
            ]
        )
        llm = _cliente()
        await llm.complete([{"role": "user", "content": "hola"}])

        assert llm._extras_ok is True
        # El reintento SIGUE llevando los ajustes.
        segundo = json.loads(ruta.calls[1].request.content)
        assert segundo["reasoning"] == {"effort": "minimal"}
        assert segundo["provider"] == {"sort": "throughput"}

    @pytest.mark.parametrize(
        "mensaje",
        [
            "Reasoning is not supported for this model",
            "Unrecognized request argument supplied: 'provider'",
            "Invalid value for provider.sort",
        ],
    )
    @respx.mock
    async def test_un_400_que_nombra_el_parametro_los_apaga(self, mensaje: str) -> None:
        ruta = respx.post(URL).mock(
            side_effect=[httpx.Response(400, json={"error": {"message": mensaje}}), _ok()]
        )
        llm = _cliente()
        reply = await llm.complete([{"role": "user", "content": "hola"}])

        assert reply.content == "hola"
        assert llm._extras_ok is False
        segundo = json.loads(ruta.calls[1].request.content)
        assert "reasoning" not in segundo and "provider" not in segundo

    @respx.mock
    async def test_se_reencienden_pasada_la_hora(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reloj = [1000.0]
        monkeypatch.setattr(llm_mod.time, "monotonic", lambda: reloj[0])
        ruta = respx.post(URL).mock(
            side_effect=[
                httpx.Response(400, json={"error": {"message": "reasoning not supported"}}),
                _ok(),
                _ok(),
                _ok(),
            ]
        )
        llm = _cliente()
        await llm.complete([{"role": "user", "content": "una"}])
        assert llm._extras_ok is False

        reloj[0] += EXTRAS_APAGADOS_SEGUNDOS - 1
        await llm.complete([{"role": "user", "content": "otra"}])
        assert "reasoning" not in json.loads(ruta.calls[2].request.content)

        reloj[0] += 2
        await llm.complete([{"role": "user", "content": "y otra"}])
        assert json.loads(ruta.calls[3].request.content)["reasoning"] == {"effort": "minimal"}


@respx.mock
async def test_un_400_generico_no_apaga_los_extras_y_es_terminal() -> None:
    # 073-A (D2): un 400 que no nombra los ajustes ya no se reintenta.
    ruta = respx.post(URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "Provider returned error"}})
    )
    llm = _cliente()
    with pytest.raises(LlmExhausted):
        await llm.complete([{"role": "user", "content": "hola"}])
    assert llm._extras_ok is True
    assert len(ruta.calls) == 1


class TestElTopeDeSalida:
    def test_viene_de_la_variable_de_entorno(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert Settings().llm_max_tokens == 2048
        monkeypatch.setenv("LLM_MAX_TOKENS", "1500")
        assert Settings().llm_max_tokens == 1500

    @respx.mock
    async def test_cero_no_lo_manda(self) -> None:
        ruta = respx.post(URL).mock(return_value=_ok())
        await _cliente(max_tokens=0).complete([{"role": "user", "content": "hola"}])
        assert "max_tokens" not in json.loads(ruta.calls.last.request.content)

    @respx.mock
    async def test_si_choca_repite_una_vez_con_el_doble(self) -> None:
        ruta = respx.post(URL).mock(
            side_effect=[_ok("a medias", finish="length"), _ok("completa")]
        )
        reply = await _cliente(max_tokens=1500).complete([{"role": "user", "content": "hola"}])

        # Lo cortado NO sale: sale la respuesta de la segunda llamada.
        assert reply.content == "completa"
        topes = [json.loads(c.request.content)["max_tokens"] for c in ruta.calls]
        assert topes == [1500, 3000]

    @respx.mock
    async def test_una_herramienta_cortada_no_llega_con_argumentos_vacios(self) -> None:
        cortada = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "t1",
                                    "type": "function",
                                    "function": {"name": "update_ficha", "arguments": '{"presupuesto": "6'},
                                }
                            ],
                        },
                        "finish_reason": "length",
                    }
                ]
            },
        )
        entera = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "t2",
                                    "type": "function",
                                    "function": {
                                        "name": "update_ficha",
                                        "arguments": '{"presupuesto": "600000"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )
        respx.post(URL).mock(side_effect=[cortada, entera])
        reply = await _cliente().complete([{"role": "user", "content": "hola"}], tools=[])

        assert reply.tool_calls[0].arguments == {"presupuesto": "600000"}

    @respx.mock
    async def test_solo_se_amplia_una_vez(self) -> None:
        # Acotado: si con el doble vuelve a chocar, se usa lo que trajo.
        ruta = respx.post(URL).mock(
            side_effect=[_ok("uno", finish="length"), _ok("dos", finish="length")]
        )
        reply = await _cliente().complete([{"role": "user", "content": "hola"}])
        assert reply.content == "dos"
        assert len(ruta.calls) == 2

    @respx.mock
    async def test_la_repeticion_no_gasta_un_reintento(self) -> None:
        # Chocar con el tope en el ÚLTIMO intento todavía repite con el doble.
        caida = httpx.Response(500, json={"error": {"message": "boom"}})
        ruta = respx.post(URL).mock(
            side_effect=[caida, caida, _ok("a medias", finish="length"), _ok("bien")]
        )
        reply = await _cliente().complete([{"role": "user", "content": "hola"}])
        assert reply.content == "bien"
        assert len(ruta.calls) == 4

    @respx.mock
    async def test_tres_caidas_siguen_agotando(self) -> None:
        caida = httpx.Response(500, json={"error": {"message": "boom"}})
        respx.post(URL).mock(side_effect=[caida, caida, caida])
        with pytest.raises(LlmExhausted):
            await _cliente().complete([{"role": "user", "content": "hola"}])


class TestHasKnowledge:
    AHORA = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    BOGOTA = ZoneInfo("America/Bogota")

    def _prompt(self, profile: BusinessProfile) -> str:
        return build_system_prompt(
            profile=profile,
            context=None,
            conv=Conversation(id=1, wa_identity="573001112233"),
            now=self.AHORA,
            tz=self.BOGOTA,
        )

    def test_instrucciones_sin_kb_no_es_conocimiento(self) -> None:
        prof = BusinessProfile(instructions="Somos una barbería en Medellín.")
        assert prof.has_instructions
        assert not prof.has_knowledge

    def test_kb_si_es_conocimiento(self) -> None:
        assert BusinessProfile(kb_text="P: Precio\nR: 25.000 COP").has_knowledge

    def test_el_prompt_avisa_que_falta_el_kb(self) -> None:
        texto = self._prompt(BusinessProfile(instructions="Somos una barbería en Medellín."))
        assert "aún no cargó su base de conocimiento" in texto
        assert "no configuró instrucciones ni conocimiento" not in texto

    def test_sin_nada_el_aviso_de_siempre(self) -> None:
        texto = self._prompt(BusinessProfile())
        assert "no configuró instrucciones ni conocimiento" in texto
        assert "aún no cargó su base de conocimiento" not in texto

    def test_con_kb_no_hay_aviso(self) -> None:
        texto = self._prompt(
            BusinessProfile(instructions="Barbería.", kb_text="P: Precio\nR: 25.000 COP")
        )
        assert "OJO: el negocio" not in texto
