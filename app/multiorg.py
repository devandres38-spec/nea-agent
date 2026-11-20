"""Modo multi-organización: una Nea atendiendo a varios negocios.

## El problema que resuelve

En modo cloud, Nea atendía a UNA organización: `CRM_BRAIN_SECRET`,
`CRM_ORGANIZATION` y `LLM_API_KEY` son valores únicos. Servir a diez negocios
significaba levantar diez Neas.

El atajo evidente —el mismo secreto para todas las organizaciones del CRM—
rompe el aislamiento del lado del CRM: allá cada miembro puede ver su secreto,
y con uno compartido cualquiera escribiría en la bandeja de otro.

## Cómo funciona

El CRM registra a esta Nea como un *despliegue* con UN secreto que ningún
miembro ve. Ese secreto:

1. **Firma los despachos.** Todos, sea cual sea la organización, así que se
   verifican con uno solo — que es lo que permite atenderlas a todas.
2. **Deriva la credencial de cada organización** para contestar:

       credencial(org) = HMAC-SHA256(secreto, "vocero:cerebro:v1:{org_id}")

Derivarla en vez de recibirla tiene una consecuencia práctica que importa: un
worker de seguimiento que despierta cuatro horas después la vuelve a calcular
igual. No hay token que caduque a media conversación ni credencial que guardar.

## Con qué piensa

**No con una llave propia, y tampoco con la del miembro.** El CRM piensa por
Nea: `/api/brains/llm` es un endpoint compatible con OpenAI que le pone la
credencial de esa organización y reenvía al proveedor.

Así la llave del miembro no sale nunca del CRM —una filtrada no se puede
desfiltrar— y, de paso, cuando el proveedor la rechaza es el CRM quien recibe
el 401 y puede pausarla en el panel de su dueño. Cuando la llave viajaba, ese
código se lo comía Nea y el miembro veía «activa» mientras nada funcionaba.

Del contexto de cada conversación sale la ruta y qué modelos puede usar. Ver
`crm_brains.py`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from collections import OrderedDict
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx

from app.agenda import SondaDeAgenda
from app.crm_brains import BrainsCrmClient
from app.llm import OpenAiLlm
from app.profile import ProfileProvider, temperatura_valida

if TYPE_CHECKING:
    from app.state import AppContext

logger = logging.getLogger("nea.multiorg")

# Separación de dominio. Tiene que ser EXACTAMENTE la misma cadena que usa
# `src/server/brains/deployment.ts` en el CRM: es un contrato entre dos
# repositorios, y un guion de diferencia lo rompe entero sin decir por qué.
DOMINIO = "vocero:cerebro:v1"


def credencial_derivada(secreto_despliegue: str, organization_id: str) -> str:
    """La credencial de UNA organización dentro de este despliegue.

    base64url SIN relleno: es lo que produce `.digest("base64url")` de Node, y
    aquí se compara carácter a carácter contra lo que espera el CRM.
    """
    mac = hmac.new(
        secreto_despliegue.encode(),
        f"{DOMINIO}:{organization_id}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def limites_http(dispatch_concurrency: int) -> httpx.Limits:
    """El pool HTTP compartido hacia el CRM: nunca menos de 32 conexiones."""
    maximo = max(32, int(dispatch_concurrency) * 2 + 8)
    return httpx.Limits(max_connections=maximo, max_keepalive_connections=maximo // 2)


class RegistroDeOrganizaciones:
    """Lo que esta Nea necesita para hablar con el CRM en nombre de N negocios.

    vocero-crm 071 (D3, D4) — Ya no cachea un cliente del CRM por
    organización. Ese cliente guardaba en la instancia lo que traía el último
    contexto (perfil, IA, capacidades) y lo compartían todos los turnos del
    negocio: con turnos concurrentes, uno leía lo del otro. Ahora:

    - Hay UN transporte HTTP hacia el CRM para todos los negocios (todos van
      al mismo host): un solo pool de sockets, con límites, que se cierra al
      apagar y nunca a media conversación.
    - `cliente()` devuelve un cliente NUEVO y ligero cada vez —uno por turno—
      con la credencial derivada de esa organización en cada petición. Lo que
      aprende del turno muere con él.
    - La cabecera `X-Vocero-Organization` lleva el **id** (el CRM acepta slug
      o id): si el negocio cambia de subdominio, un worker que despierta horas
      después sigue entrando. La credencial ya se firmaba con el id.
    """

    # Cuántos clientes del modelo se recuerdan (uno por organización y
    # modelo). Al pasarse se suelta el más viejo: no se cierra nada, porque el
    # transporte es el compartido.
    MAX_LLMS = 256

    # Sondas de agenda por negocio: una por organización vista (son pocas).
    MAX_SONDAS = 4096

    def __init__(
        self,
        base_url: str,
        secreto_despliegue: str,
        *,
        nombre_por_defecto: str = "Asistente",
        brief_path: str | None = None,
        agenda_ttl: float = 60.0,
        dispatch_concurrency: int = 8,
    ) -> None:
        self._base_url = base_url
        self._secreto = secreto_despliegue
        self._nombre = nombre_por_defecto
        self._brief = brief_path
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=15.0,
            # Auditoría 071 (B-1): el pool crece con los turnos en vuelo
            # (~2 llamadas solapadas por turno + el proxy del LLM).
            limits=limites_http(dispatch_concurrency),
        )
        # Cacheados por la tupla completa y no por organizacion: si el miembro
        # cambia de modelo, la tupla cambia y el siguiente turno ya usa el
        # cliente nuevo sin que nadie tenga que invalidar nada.
        self._llms: OrderedDict[tuple[str, str, str, str, float], OpenAiLlm] = OrderedDict()
        self._agenda_ttl = agenda_ttl
        self._sondas: OrderedDict[str, SondaDeAgenda] = OrderedDict()

    def cliente(self, organization_id: str, slug: str) -> BrainsCrmClient:
        """Un cliente del CRM para UN turno de esa organización."""
        return BrainsCrmClient(
            self._base_url,
            credencial_derivada(self._secreto, organization_id),
            organization_id,
            client=self._http,
            compartido=True,
        )

    def sonda(self, organization_id: str, slug: str) -> SondaDeAgenda:
        """071 (D5) — La sonda de agenda de ESE negocio.

        Antes era una para toda la Nea, preguntando con el centinela (siempre
        «sí»), así que solo cambiaba cuando una herramienta de CUALQUIER
        negocio chocaba con un 404: uno apagaba la agenda de todos durante el
        TTL. Ahora cada negocio tiene la suya, con su credencial. Con un CRM
        que dice `agendaEnabled` en el contexto casi no se usa (respaldo).
        """
        sonda = self._sondas.get(organization_id)
        if sonda is None:
            sonda = SondaDeAgenda(self.cliente(organization_id, slug), ttl=self._agenda_ttl)
            self._sondas[organization_id] = sonda
            while len(self._sondas) > self.MAX_SONDAS:
                self._sondas.popitem(last=False)
        else:
            self._sondas.move_to_end(organization_id)
        return sonda

    def perfil(self, cliente: BrainsCrmClient) -> ProfileProvider:
        """El perfil de ESTE turno: lo lee del contexto que trajo su cliente.

        En esta superficie el perfil viaja dentro del contexto y
        `ProfileProvider` no lo cachea entre turnos (`cloud`), así que basta
        uno por turno, atado al cliente del turno.
        """
        return ProfileProvider(
            cliente, default_name=self._nombre, brief_path=self._brief
        )

    def llm(
        self,
        organization_id: str,
        slug: str,
        config: dict[str, Any],
        por_defecto: Any,
    ) -> OpenAiLlm:
        """El cliente del modelo, apuntando al CRM.

        No lleva la llave de nadie: lleva la credencial derivada de esta
        organización, la misma con la que Nea le habla al CRM para todo lo
        demás. El CRM le pone la llave del miembro al reenviar al proveedor.

        Para el SDK de OpenAI esto es un proveedor compatible más. Por eso el
        cambio cabe aquí y no se nota en el resto del turno.

        `por_defecto` son los ajustes de esta Nea y solo rellenan el modelo
        que conversa. El que TRANSCRIBE no se rellena: si el miembro no
        eligió uno que oiga, es mejor no transcribir que mandarle el audio a
        uno que no oye y quedarnos con lo que invente.
        """
        base_url = self._base_url.rstrip("/") + str(
            config.get("path") or "/api/brains/llm"
        )
        model = str(config.get("model") or por_defecto.llm_model)
        transcribe = str(config.get("transcribeModel") or "")
        # 073-A (D2) — La que mande el CRM para este negocio; si no, la de aquí.
        temperatura = _temperatura_de(config, por_defecto)
        credencial = credencial_derivada(self._secreto, organization_id)
        clave = (credencial, model, transcribe, base_url, temperatura)
        cliente = self._llms.get(clave)
        if cliente is not None:
            self._llms.move_to_end(clave)
        else:
            cliente = OpenAiLlm(
                credencial,
                model,
                transcribe_model=transcribe,
                base_url=base_url,
                default_headers={"X-Vocero-Organization": organization_id},
                http_client=self._http,
                # Los ajustes de velocidad son de esta Nea, no del miembro:
                # el CRM reenvia el cuerpo TAL CUAL, asi que `extra_body`
                # llega al proveedor igual que en mono-organizacion.
                reasoning_effort=por_defecto.llm_reasoning_effort,
                provider_sort=por_defecto.llm_provider_sort,
                # Y el tope por intento, tambien de esta Nea: el CRM reenvia,
                # pero quien espera al proveedor colgado es el turno de aqui.
                timeout=por_defecto.llm_timeout_seconds,
                max_tokens=por_defecto.llm_max_tokens,
                temperature=temperatura,
                # vocero-crm 074 (E8): el CRM anota el uso por despacho.
                cabecera_despacho=True,
            )
            self._llms[clave] = cliente
            while len(self._llms) > self.MAX_LLMS:
                self._llms.popitem(last=False)
        return cliente

    def http_de_ensayo(self, organization_id: str, slug: str, ficha: str) -> Any:
        """Cliente HTTP del CRM para UN ensayo (app/ensayo.py), sin caché.

        Lleva la credencial derivada de esa organización Y la ficha del
        ensayo (`X-Vocero-Ensayo`), que el CRM firmó para ESA organización y
        ESA conversación inventada. Con ella el CRM contesta los huecos sin
        registrarlos como oferta. No se cachea: la ficha caduca en minutos y
        un cliente compartido la mezclaría con los turnos de verdad.
        """
        import httpx

        return httpx.AsyncClient(
            base_url=self._base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {credencial_derivada(self._secreto, organization_id)}",
                "X-Vocero-Organization": organization_id,
                "X-Vocero-Ensayo": ficha,
            },
            timeout=15.0,
        )

    def llm_de_ensayo(
        self,
        organization_id: str,
        slug: str,
        config: dict[str, Any],
        por_defecto: Any,
        ficha: str,
    ) -> OpenAiLlm:
        """El modelo de UN ensayo: el mismo camino que `llm`, con la ficha.

        Piensa con la llave del negocio por `/api/brains/llm`, nunca con una de
        la plataforma. La ficha le dice al CRM que es un ensayo pedido por el
        dueño, que puede pensar aunque el negocio todavía no esté activado.
        """
        base_url = self._base_url.rstrip("/") + str(
            config.get("path") or "/api/brains/llm"
        )
        return OpenAiLlm(
            credencial_derivada(self._secreto, organization_id),
            str(config.get("model") or por_defecto.llm_model),
            # Un ensayo es de texto: no transcribe.
            transcribe_model="",
            base_url=base_url,
            default_headers={"X-Vocero-Organization": organization_id, "X-Vocero-Ensayo": ficha},
            reasoning_effort=por_defecto.llm_reasoning_effort,
            provider_sort=por_defecto.llm_provider_sort,
            timeout=por_defecto.llm_timeout_seconds,
            max_tokens=por_defecto.llm_max_tokens,
            temperature=_temperatura_de(config, por_defecto),
        )

    async def aclose(self) -> None:
        """Cierra el transporte compartido. Solo al apagar la app."""
        self._llms.clear()
        await self._http.aclose()


class CrmSinOrganizacion:
    """El "cliente" del CRM mientras no se sabe de qué organización.

    En modo multi-organización no existe un cliente del CRM por defecto: cada
    turno arma el suyo con la credencial de la organización que le toca. Pero
    `AppContext.crm` tiene que ser algo, y ese algo importa mucho.

    Si fuera un cliente de verdad —el de la primera organización, digamos—, un
    camino que se olvidara de cambiarlo escribiría en la bandeja EQUIVOCADA y
    nadie se enteraría: el mensaje sale, el CRM lo acepta, y aparece en el
    negocio que no era. Con esto, ese olvido revienta en el sitio exacto.

    Las dos excepciones son las que usa el arranque y el apagado, y ninguna
    escribe nada.
    """

    async def agenda_available(self) -> bool:
        """No se sonda: no hay con qué credencial preguntar.

        Se asume que sí, que es lo mismo que hace el sondeo cuando no puede
        concluir. Si resulta que no, el primer intento real recibe 404 y las
        herramientas de agenda contestan "aquí no se agenda" — un camino que ya
        existe y está probado.
        """
        return True

    async def sondear_agenda(self, timeout: float | None = None) -> bool | None:
        """Lo mismo, para la sonda con caducidad (app/agenda.py).

        True y no None a propósito: con None la sonda se quedaría con lo
        último que supo, y tras un 404 de la bandera eso sería «apagada» para
        siempre. Con True, al vencer el TTL se vuelve a intentar con el primer
        turno que llegue, que es el que de verdad pregunta con credencial.
        """
        return True

    async def aclose(self) -> None:
        return None

    def __getattr__(self, nombre: str) -> Any:
        raise RuntimeError(
            f"se intentó usar el CRM ({nombre}) sin saber de qué organización "
            f"es este turno: en modo multi-organización el cliente lo arma el "
            f"despacho, no el arranque"
        )


class LlmSinOrganizacion:
    """El "cliente" del modelo mientras no se sabe de qué organización.

    Hermano de `CrmSinOrganizacion`, y por el mismo motivo. En modo
    multi-organización no hay un modelo por defecto: cada turno arma el suyo
    apuntando al CRM con la credencial derivada de SU organización, y es el
    CRM quien pone la llave del miembro al reenviar.

    Antes aquí se construía un `OpenAiLlm` con `LLM_API_KEY` del entorno. Tenía
    dos fallos, y el segundo es peor que el primero:

    1. **No arranca.** En este modo esa variable no existe a propósito —Nea no
       lleva la llave de nadie—, y el SDK de OpenAI se niega a construirse sin
       credencial. El proceso moría en el `lifespan`.
    2. **Si existiera, cobraría al que no era.** Un camino que se olvidara de
       cambiar el cliente pensaría con la llave del dueño de la plataforma, y
       el consumo de un miembro se lo pagaría otro. En silencio.

    Por eso revienta en vez de degradar: `LlmExhausted` haría que el turno
    siguiera su camino de «no pude pensar», y un olvido de cableado se leería
    como un fallo del proveedor.
    """

    def __getattr__(self, nombre: str) -> Any:
        raise RuntimeError(
            f"se intentó usar el modelo ({nombre}) sin saber de qué "
            f"organización es este turno: en modo multi-organización quien "
            f"piensa es el CRM con la credencial del miembro, y ese cliente lo "
            f"arma el despacho, no el arranque"
        )

def contexto_del_turno(ctx: "AppContext") -> "AppContext":
    """vocero-crm 071 (D3) — El `AppContext` de UN turno, fuera de multiorg.

    Una copia, siempre: el turno escribe en su contexto (`agenda_enabled`) y
    con turnos concurrentes esa escritura no puede alcanzar a otro. El cliente
    del CRM es una vista del de la app (`para_turno`): mismo transporte, su
    propio estado. En cloud de un negocio el perfil viaja dentro del contexto
    de la conversación, así que también es del turno.
    """
    para_turno = getattr(ctx.crm, "para_turno", None)
    crm = para_turno() if callable(para_turno) else ctx.crm
    profile = ctx.profile
    if ctx.settings.cloud_mode and crm is not ctx.crm:
        profile = ProfileProvider(
            crm,
            default_name=ctx.settings.agent_name,
            brief_path=ctx.settings.brief_path or None,
        )
    return replace(ctx, crm=crm, profile=profile)


def ctx_de_organizacion(
    ctx: "AppContext",
    organization_id: str,
    organization_slug: str,
) -> "AppContext | None":
    """El contexto de un worker de fondo, apuntando a UNA organización.

    Los workers (seguimiento, envío diferido) despiertan mucho después del
    turno, cuando ya no hay despacho del que sacar de quién era el mensaje. Lo
    sacan de la fila que están procesando, que por eso guarda su organización.

    Fuera del modo multi-organización devuelve el contexto tal cual: una Nea de
    un solo negocio ya tiene el cliente correcto y no hay nada que resolver.

    None significa «no puedo saber de quién es esto». Pasa con filas escritas
    antes de esta versión, que no llevan organización. Callarse es lo correcto:
    contestar con la credencial equivocada sería escribirle al negocio que no
    era.
    """
    if not ctx.settings.multi_org:
        return ctx
    if not organization_id or ctx.registro is None:
        return None
    cliente = ctx.registro.cliente(organization_id, organization_slug)
    return replace(
        ctx,
        crm=cliente,
        profile=ctx.registro.perfil(cliente),
        organizacion=(organization_id, organization_slug),
    )

def organizacion_del_despacho(payload: dict[str, Any]) -> tuple[str, str] | None:
    """`(id, slug)` de la organización a la que pertenece este despacho.

    El slug puede faltar —una organización recién creada podría no tenerlo— y
    entonces se usa el id también como slug, que es lo que acepta el CRM en la
    cabecera. El id, en cambio, es obligatorio: sin él no hay credencial que
    derivar, y contestar sería imposible.
    """
    org = payload.get("organization") or {}
    org_id = str(org.get("id") or "")
    if not org_id:
        return None
    return org_id, str(org.get("slug") or org_id)


def _temperatura_de(config: dict[str, Any], por_defecto: Any) -> float:
    """073-A (D2) — La que mande el CRM para este negocio; si no, la de aquí.

    Una sola regla para el turno y el ensayo: el dueño prueba al agente que
    de verdad va a contestar.
    """
    temperatura = temperatura_valida(config.get("temperature"))
    return por_defecto.llm_temperature if temperatura is None else temperatura
