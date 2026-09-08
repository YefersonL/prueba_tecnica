"""
tests/test_ingestion.py
=======================
Tests unitarios para core/ingestion.py y adapters/queue.py

Cobertura:
  - normalize_event(): distinción HUMAN vs MACHINE por sender.type
  - normalize_event(): fallback por sender_id conocido
  - normalize_event(): heurística por sufijo de ID
  - normalize_event(): error en evento no-MESSAGE
  - normalize_event(): error en mensaje vacío
  - normalize_event(): preserva el raw_payload para auditoría
  - normalize_batch(): procesa múltiples payloads, aísla errores
  - InMemoryEventQueue: publish/consume/size/consume_all
  - Flujo end-to-end: normalize → publish → consume
  - Validación de que InMemoryEventQueue implementa el protocolo EventQueue
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.queue import InMemoryEventQueue
from core.ingestion import normalize_batch, normalize_event
from core.models import EventSource
from core.ports import EventQueue

# ---------------------------------------------------------------------------
# Helpers — payloads de prueba
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _make_payload(
    sender_type: str = "HUMAN",
    sender_id: str = "users/analyst-001",
    text: str = "El pipeline falló.",
    space_id: str = "spaces/AAAA1111",
    event_type: str = "MESSAGE",
) -> dict:
    """Construye un payload mínimo válido de Google Chat para tests."""
    return {
        "type": event_type,
        "eventTime": "2026-09-08T08:00:00.000Z",
        "message": {
            "name": f"{space_id}/messages/test-msg",
            "sender": {
                "name": sender_id,
                "displayName": "Test Sender",
                "type": sender_type,
            },
            "createTime": "2026-09-08T08:00:00.000Z",
            "text": text,
            "space": {
                "name": space_id,
                "type": "ROOM",
                "displayName": "Test Room",
            },
        },
    }


# ---------------------------------------------------------------------------
# Tests: normalize_event — distinción HUMAN/MACHINE
# ---------------------------------------------------------------------------


class TestNormalizeEvent:
    def test_bot_sender_type_yields_machine(self) -> None:
        """sender.type == BOT → EventSource.MACHINE (regla primaria)."""
        payload = _make_payload(sender_type="BOT", sender_id="users/any-bot")
        event = normalize_event(payload)
        assert event.source == EventSource.MACHINE

    def test_human_sender_type_yields_human(self) -> None:
        """sender.type == HUMAN y sender_id no en lista conocida → HUMAN."""
        payload = _make_payload(sender_type="HUMAN", sender_id="users/analyst-042")
        event = normalize_event(payload)
        assert event.source == EventSource.HUMAN

    def test_known_bot_id_overrides_human_type(self) -> None:
        """
        Si sender.type == HUMAN pero el ID está en KNOWN_BOT_SENDER_IDS → MACHINE.
        Caso real: bots migrados que aún se reportan como HUMAN.
        """
        payload = _make_payload(
            sender_type="HUMAN",
            sender_id="users/monitoring-bot-001",  # está en KNOWN_BOT_SENDER_IDS
        )
        event = normalize_event(payload)
        assert event.source == EventSource.MACHINE

    def test_known_bot_id_with_unspecified_type(self) -> None:
        """TYPE_UNSPECIFIED + ID conocido → MACHINE (fallback por ID)."""
        payload = _make_payload(
            sender_type="TYPE_UNSPECIFIED",
            sender_id="users/pipeline-alert-bot",
        )
        event = normalize_event(payload)
        assert event.source == EventSource.MACHINE

    def test_bot_suffix_heuristic(self) -> None:
        """
        sender_id que termina en '-bot' → MACHINE (heurística de nombre).
        Captura bots no registrados explícitamente.
        """
        payload = _make_payload(
            sender_type="TYPE_UNSPECIFIED",
            sender_id="users/nuevo-sistema-bot",
        )
        event = normalize_event(payload)
        assert event.source == EventSource.MACHINE

    def test_unknown_sender_defaults_to_human(self) -> None:
        """ID desconocido con tipo no especificado → HUMAN por defecto."""
        payload = _make_payload(
            sender_type="TYPE_UNSPECIFIED",
            sender_id="users/usuario-misterioso-123",
        )
        event = normalize_event(payload)
        assert event.source == EventSource.HUMAN

    def test_normalized_fields_are_correct(self) -> None:
        """Los campos del RawEvent deben reflejar exactamente el payload."""
        payload = _make_payload(
            sender_type="HUMAN",
            sender_id="users/analyst-001",
            text="  Las transacciones fallaron.  ",  # con espacios
            space_id="spaces/AAAA1111",
        )
        event = normalize_event(payload)
        assert event.sender_id == "users/analyst-001"
        assert event.space_id == "spaces/AAAA1111"
        # El texto debe estar trimmeado
        assert event.message == "Las transacciones fallaron."
        # El raw_payload debe preservarse para auditoría
        assert event.raw_payload == payload

    def test_raw_payload_preserved(self) -> None:
        """El payload original debe estar en event.raw_payload (trazabilidad)."""
        payload = _make_payload()
        event = normalize_event(payload)
        assert event.raw_payload is payload

    def test_non_message_event_raises(self) -> None:
        """Eventos de tipo distinto a MESSAGE deben lanzar ValueError."""
        payload = _make_payload(event_type="ADDED_TO_SPACE")
        with pytest.raises(ValueError, match="ADDED_TO_SPACE"):
            normalize_event(payload)

    def test_empty_text_raises(self) -> None:
        """Mensajes con texto vacío o solo espacios deben lanzar ValueError."""
        payload = _make_payload(text="   ")
        with pytest.raises(ValueError, match="vacío"):
            normalize_event(payload)

    def test_missing_message_key_raises(self) -> None:
        """Payload sin la clave 'message' debe lanzar KeyError."""
        with pytest.raises(KeyError):
            normalize_event({"type": "MESSAGE"})


# ---------------------------------------------------------------------------
# Tests: normalize_event con fixture real de Google Chat
# ---------------------------------------------------------------------------


class TestNormalizeEventWithFixture:
    """Usa el fixture JSON real para validar contra payloads representativos."""

    @pytest.fixture(autouse=True)
    def load_fixture(self) -> None:
        fixture_path = FIXTURES_DIR / "mock_google_chat_webhook.json"
        with open(fixture_path, encoding="utf-8") as f:
            self.payloads: list[dict] = json.load(f)

    def test_fixture_has_four_payloads(self) -> None:
        assert len(self.payloads) == 4

    def test_first_payload_is_human(self) -> None:
        """Primer mensaje del fixture: analista de finanzas → HUMAN."""
        event = normalize_event(self.payloads[0])
        assert event.source == EventSource.HUMAN
        assert "transacciones" in event.message.lower()

    def test_second_payload_is_machine(self) -> None:
        """Segundo mensaje del fixture: monitoring-bot-001 → MACHINE."""
        event = normalize_event(self.payloads[1])
        assert event.source == EventSource.MACHINE
        assert "pipeline" in event.message.lower()

    def test_third_payload_is_human(self) -> None:
        """Tercer mensaje: analista de riesgo → HUMAN."""
        event = normalize_event(self.payloads[2])
        assert event.source == EventSource.HUMAN

    def test_fourth_payload_is_machine(self) -> None:
        """Cuarto mensaje: monitoring bot, alerta de pagos → MACHINE."""
        event = normalize_event(self.payloads[3])
        assert event.source == EventSource.MACHINE

    def test_normalize_batch_processes_all(self) -> None:
        """normalize_batch debe procesar los 4 payloads sin errores."""
        events, errors = normalize_batch(self.payloads)
        assert len(events) == 4
        assert len(errors) == 0

    def test_normalize_batch_isolates_errors(self) -> None:
        """Un payload inválido no debe afectar al resto del batch."""
        bad_payload = {"type": "ADDED_TO_SPACE"}
        mixed = [self.payloads[0], bad_payload, self.payloads[1]]
        events, errors = normalize_batch(mixed)
        assert len(events) == 2
        assert len(errors) == 1
        assert errors[0][0] == 1  # índice del payload inválido


# ---------------------------------------------------------------------------
# Tests: InMemoryEventQueue
# ---------------------------------------------------------------------------


class TestInMemoryEventQueue:
    @pytest.fixture
    def queue(self) -> InMemoryEventQueue:
        return InMemoryEventQueue()

    @pytest.fixture
    def sample_event(self) -> object:
        return normalize_event(_make_payload())

    def test_empty_queue_size_is_zero(self, queue: InMemoryEventQueue) -> None:
        assert queue.size() == 0

    def test_publish_increases_size(
        self, queue: InMemoryEventQueue, sample_event: object
    ) -> None:
        queue.publish(sample_event)
        assert queue.size() == 1

    def test_consume_returns_event_fifo(self, queue: InMemoryEventQueue) -> None:
        """La cola debe ser FIFO: el primero en entrar es el primero en salir."""
        e1 = normalize_event(_make_payload(text="Primer mensaje"))
        e2 = normalize_event(_make_payload(text="Segundo mensaje"))
        queue.publish(e1)
        queue.publish(e2)

        first = queue.consume()
        assert first.message == "Primer mensaje"

        second = queue.consume()
        assert second.message == "Segundo mensaje"

    def test_consume_empty_returns_none(self, queue: InMemoryEventQueue) -> None:
        assert queue.consume() is None

    def test_consume_decreases_size(
        self, queue: InMemoryEventQueue, sample_event: object
    ) -> None:
        queue.publish(sample_event)
        queue.consume()
        assert queue.size() == 0

    def test_consume_all_drains_queue(self, queue: InMemoryEventQueue) -> None:
        for i in range(3):
            queue.publish(normalize_event(_make_payload(text=f"Mensaje {i}")))
        events = queue.consume_all()
        assert len(events) == 3
        assert queue.size() == 0

    def test_implements_event_queue_protocol(self, queue: InMemoryEventQueue) -> None:
        """
        Verifica que InMemoryEventQueue satisface el protocolo EventQueue.
        Esto captura regresiones si el protocolo cambia y el adaptador no se actualiza.
        """
        assert isinstance(queue, EventQueue)


# ---------------------------------------------------------------------------
# Test: flujo end-to-end ingesta → cola
# ---------------------------------------------------------------------------


class TestIngestionToQueueFlow:
    def test_end_to_end_normalize_and_enqueue(self) -> None:
        """
        Simula el flujo completo: webhook payload → normalize → publish → consume.
        Este es el happy path del Paso 2.
        """
        queue = InMemoryEventQueue()

        # Simula 2 mensajes entrando al webhook
        human_payload = _make_payload(
            sender_type="HUMAN",
            text="El sistema de reportes está caído.",
        )
        bot_payload = _make_payload(
            sender_type="BOT",
            sender_id="users/monitoring-bot-001",
            text="❌ Falló el pipeline de ingesta de transacciones — 03:14 AM",
        )

        for payload in [human_payload, bot_payload]:
            event = normalize_event(payload)
            queue.publish(event)

        assert queue.size() == 2

        # El clasificador consumiría los eventos en orden
        first = queue.consume()
        assert first.source == EventSource.HUMAN

        second = queue.consume()
        assert second.source == EventSource.MACHINE

        assert queue.size() == 0
