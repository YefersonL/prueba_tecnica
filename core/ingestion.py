"""
core/ingestion.py
=================
Lógica de normalización de eventos de Google Chat → RawEvent.

Esta función es el único punto de entrada al sistema desde el exterior.
Es pura (sin side effects): toma un dict (payload JSON) y retorna un RawEvent.

Por qué aquí y no en adapters/:
  La regla de distinción HUMAN/MACHINE es lógica de dominio — define cómo
  interpretamos los mensajes del negocio. El adaptador (api/webhook.py) solo
  parsea JSON y llama a esta función. Si la regla cambia, cambia aquí, no
  en el adaptador HTTP.

Reglas de distinción humano/máquina (en orden de prioridad):
  1. sender.type == "BOT"  → MACHINE  (campo oficial de Google Chat API)
  2. sender.type == "HUMAN" → HUMAN
  3. Fallback por sender_id conocido (prefijo "bot-" o sufijo "-bot") → MACHINE
  4. Default → HUMAN (preferimos falsos positivos humanos sobre ignorar alertas)

Documentación del payload de Google Chat:
  https://developers.google.com/chat/api/reference/rest/v1/spaces.messages
"""

from __future__ import annotations

import re

from core.models import EventSource, RawEvent

# IDs de bots/sistemas conocidos — en producción esto vendría de config/BD
# Mantenemos la lista aquí como constante de dominio, no en el adaptador.
KNOWN_BOT_SENDER_IDS: frozenset[str] = frozenset(
    {
        "users/monitoring-bot-001",
        "users/pipeline-alert-bot",
        "users/fraud-alert-bot",
        "users/payments-monitor-bot",
    }
)


def _resolve_source(sender_type: str, sender_id: str) -> EventSource:
    """
    Determina si el origen del mensaje es HUMAN o MACHINE.

    Parámetros
    ----------
    sender_type : Valor de message.sender.type en el payload de Google Chat.
                  Valores posibles: "HUMAN", "BOT", "TYPE_UNSPECIFIED".
    sender_id   : Valor de message.sender.name (ej. "users/analyst-001").

    La lógica es defensiva por diseño: ante la duda, clasificamos como HUMAN
    para que el clasificador posterior pueda manejar el mensaje.
    """
    # Regla 1: campo oficial de Google Chat (más confiable)
    if sender_type.upper() == "BOT":
        return EventSource.MACHINE
    if sender_type.upper() == "HUMAN":
        # Doble check: si un HUMAN ID está en la lista de bots conocidos,
        # priorizamos la lista (puede pasar durante migración de bots)
        if sender_id in KNOWN_BOT_SENDER_IDS:
            return EventSource.MACHINE
        return EventSource.HUMAN

    # Regla 2: lista de IDs conocidos (fallback cuando sender.type no es confiable)
    if sender_id in KNOWN_BOT_SENDER_IDS:
        return EventSource.MACHINE

    # Regla 3: heurística por sufijo de ID
    lower_id = sender_id.lower()
    if lower_id.endswith("-bot") or lower_id.endswith("_bot") or "bot-" in lower_id:
        return EventSource.MACHINE

    # Default: HUMAN
    return EventSource.HUMAN


def normalize_event(payload: dict) -> RawEvent:
    """
    Transforma el payload JSON de un webhook de Google Chat en un RawEvent.

    Parámetros
    ----------
    payload : Dict con la estructura del evento de Google Chat.
              Ver fixtures/mock_google_chat_webhook.json para ejemplos.

    Retorna
    -------
    RawEvent con todos los campos normalizados.

    Lanza
    -----
    KeyError  : Si faltan campos obligatorios del payload.
    ValueError: Si el payload no tiene el tipo de evento esperado ("MESSAGE").

    Decisión de diseño: preferimos lanzar excepciones explícitas sobre silenciar
    errores. El adaptador HTTP (api/webhook.py) será responsable de catchearlas
    y retornar HTTP 422. Esto hace que los errores de payload sean visibles y
    rastreables desde el inicio del pipeline.
    """
    # 1. Detectar si viene en formato Google Workspace Add-on (chat.messagePayload)
    chat_obj = payload.get("chat")
    if isinstance(chat_obj, dict):
        message_payload = chat_obj.get("messagePayload") or {}
        message = message_payload.get("message") or {}
        space_obj = message_payload.get("space") or message.get("space") or {}
        sender = message.get("sender") or chat_obj.get("user") or {}
    else:
        # Formato clásico webhook Google Chat
        event_type = payload.get("type", "")
        if event_type != "MESSAGE":
            raise ValueError(
                f"Tipo de evento no soportado: '{event_type}'. Solo se procesan eventos MESSAGE."
            )
        message = payload.get("message")
        if not message or not isinstance(message, dict):
            raise KeyError("El payload no contiene el objeto 'message'.")
        space_obj = payload.get("space") or message.get("space") or {}
        sender = message.get("sender") or payload.get("user") or {}

    sender_id: str = sender.get("name", "users/unknown")
    sender_type: str = sender.get("type", "TYPE_UNSPECIFIED")
    space_id: str = space_obj.get("name", "spaces/default")

    raw_text: str = message.get("text", "").strip()
    if not raw_text:
        raise ValueError(
            f"Mensaje vacío recibido de sender '{sender_id}' en space '{space_id}'."
        )

    # Limpiar mención al bot si viene al inicio (ej. "@Fintech Support Bot Las transacciones...")
    text = re.sub(r"^@\S+\s*", "", raw_text).strip() or raw_text

    source = _resolve_source(sender_type=sender_type, sender_id=sender_id)

    return RawEvent(
        message=text,
        sender_id=sender_id,
        space_id=space_id,
        source=source,
        raw_payload=payload,
    )




def normalize_batch(payloads: list[dict]) -> list[RawEvent]:
    """
    Normaliza una lista de payloads. Errores por payload individual se
    recopilan y se retornan junto con los eventos exitosos.

    Retorna
    -------
    Tupla (eventos_ok, errores) donde errores es una lista de (índice, excepción).
    Útil para procesar el fixture de prueba que contiene múltiples mensajes.

    Nota: retornamos los exitosos aunque haya fallos — no queremos que un
    payload malformado bloquee el procesamiento de los demás.
    """
    events: list[RawEvent] = []
    errors: list[tuple[int, Exception]] = []

    for i, payload in enumerate(payloads):
        try:
            events.append(normalize_event(payload))
        except (KeyError, ValueError) as exc:
            errors.append((i, exc))

    return events, errors
