"""
tests/test_runbook_engine.py
============================
Tests unitarios para core/runbook_engine.py

Cobertura:
  Runbook de ingesta:
    - Ticket P1/INGESTION → resuelto automáticamente
    - Ticket P0/INGESTION → NO resuelto (requiere humano)
    - resolved_by_auto=True en el ticket resuelto
    - Notificación de resolución enviada

  Runbook de pagos:
    - Ticket P2/PAYMENTS → resuelto automáticamente
    - Ticket P1/PAYMENTS → NO resuelto (latencia P1 requiere revisión)

  Motor de runbooks:
    - Sistema sin runbook → no_runbook_found=True, resolved=False
    - Sistema con runbook que falla (excepción) → resolved=False, no propaga
    - Múltiples handlers: prueba en orden, usa el primero que resuelve
    - Ticket resuelto queda en estado RESOLVED en el repo
    - Inyección de registro personalizado (testabilidad del registry)

  Flujo end-to-end:
    - Evento de ingesta → normalize → classify → process → try_auto_resolve
      → ticket RESOLVED automáticamente
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adapters.chat_notifier import LocalChatNotifier
from adapters.memory_repo import InMemoryTicketRepository
from core.models import (
    EventSource,
    RawEvent,
    Severity,
    SystemTag,
    Ticket,
    TicketStatus,
)
from core.runbook_engine import (
    RUNBOOK_NAMES,
    RunbookEngine,
    _runbook_ingestion_pipeline_restart,
    _runbook_payments_latency_clear,
)
from core.ticket_engine import TicketEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ticket(
    system: SystemTag = SystemTag.INGESTION,
    severity: Severity = Severity.P1,
) -> Ticket:
    return Ticket(
        source_event_id="evt-001",
        space_id="spaces/TEST",
        system=system,
        severity=severity,
        summary=f"Fallo en {system.value}.",
        created_at=datetime.now(UTC),
    )


def _make_engine_stack() -> tuple[RunbookEngine, TicketEngine, InMemoryTicketRepository, LocalChatNotifier]:
    repo = InMemoryTicketRepository()
    ticket_engine = TicketEngine(ticket_repo=repo)
    notifier = LocalChatNotifier(log_to_console=False)
    runbook_engine = RunbookEngine(ticket_engine=ticket_engine, notifier=notifier)
    return runbook_engine, ticket_engine, repo, notifier


# ---------------------------------------------------------------------------
# Tests: handlers individuales (unit puro, sin engine)
# ---------------------------------------------------------------------------


class TestRunbookHandlers:
    def test_ingestion_handler_resolves_p1(self) -> None:
        """Handler de ingesta debe retornar True para P1."""
        ticket = _make_ticket(system=SystemTag.INGESTION, severity=Severity.P1)
        assert _runbook_ingestion_pipeline_restart(ticket) is True

    def test_ingestion_handler_resolves_p2(self) -> None:
        """Handler de ingesta debe retornar True para P2."""
        ticket = _make_ticket(system=SystemTag.INGESTION, severity=Severity.P2)
        assert _runbook_ingestion_pipeline_restart(ticket) is True

    def test_ingestion_handler_rejects_p0(self) -> None:
        """P0 requiere intervención humana → handler retorna False."""
        ticket = _make_ticket(system=SystemTag.INGESTION, severity=Severity.P0)
        assert _runbook_ingestion_pipeline_restart(ticket) is False

    def test_ingestion_handler_rejects_p3(self) -> None:
        """P3 no tiene lógica en el runbook → retorna False."""
        ticket = _make_ticket(system=SystemTag.INGESTION, severity=Severity.P3)
        assert _runbook_ingestion_pipeline_restart(ticket) is False

    def test_payments_handler_resolves_p2(self) -> None:
        """Handler de pagos resuelve P2 (latencia)."""
        ticket = _make_ticket(system=SystemTag.PAYMENTS, severity=Severity.P2)
        assert _runbook_payments_latency_clear(ticket) is True

    def test_payments_handler_rejects_p1(self) -> None:
        """P1 en pagos puede implicar pérdida de dinero → requiere humano."""
        ticket = _make_ticket(system=SystemTag.PAYMENTS, severity=Severity.P1)
        assert _runbook_payments_latency_clear(ticket) is False

    def test_payments_handler_rejects_p0(self) -> None:
        ticket = _make_ticket(system=SystemTag.PAYMENTS, severity=Severity.P0)
        assert _runbook_payments_latency_clear(ticket) is False


# ---------------------------------------------------------------------------
# Tests: RunbookEngine.try_auto_resolve
# ---------------------------------------------------------------------------


class TestRunbookEngine:
    def test_ingestion_p1_resolved_automatically(self) -> None:
        """Ticket P1/INGESTION debe resolverse automáticamente."""
        rb_engine, ticket_engine, repo, notifier = _make_engine_stack()
        ticket = _make_ticket(system=SystemTag.INGESTION, severity=Severity.P1)
        repo.save(ticket)

        result = rb_engine.try_auto_resolve(ticket)

        assert result.resolved is True
        assert result.runbook_name == "runbook:ingestion_pipeline_restart"
        assert result.no_runbook_found is False

    def test_resolved_ticket_has_auto_flag(self) -> None:
        """El ticket resuelto debe tener resolved_by_auto=True."""
        rb_engine, _, repo, _ = _make_engine_stack()
        ticket = _make_ticket(system=SystemTag.INGESTION, severity=Severity.P1)
        repo.save(ticket)

        result = rb_engine.try_auto_resolve(ticket)

        persisted = repo.get_by_id(ticket.ticket_id)
        assert persisted.resolved_by_auto is True
        assert persisted.status == TicketStatus.RESOLVED

    def test_resolved_ticket_resolved_by_runbook_name(self) -> None:
        """resolved_by debe contener el nombre del runbook para auditoría."""
        rb_engine, _, repo, _ = _make_engine_stack()
        ticket = _make_ticket(system=SystemTag.INGESTION, severity=Severity.P1)
        repo.save(ticket)

        result = rb_engine.try_auto_resolve(ticket)

        persisted = repo.get_by_id(ticket.ticket_id)
        assert "ingestion_pipeline_restart" in persisted.resolved_by

    def test_resolution_notification_sent(self) -> None:
        """Debe enviarse notificación de resolución al espacio de chat."""
        rb_engine, _, repo, notifier = _make_engine_stack()
        ticket = _make_ticket(system=SystemTag.INGESTION, severity=Severity.P1)
        repo.save(ticket)

        rb_engine.try_auto_resolve(ticket)

        resolution_msgs = [m for m in notifier.sent_messages if m.message_type == "resolution"]
        assert len(resolution_msgs) == 1
        assert "automáticamente" in resolution_msgs[0].text

    def test_p0_ingestion_not_auto_resolved(self) -> None:
        """P0 no debe resolverse automáticamente aunque haya runbook."""
        rb_engine, _, repo, notifier = _make_engine_stack()
        ticket = _make_ticket(system=SystemTag.INGESTION, severity=Severity.P0)
        repo.save(ticket)

        result = rb_engine.try_auto_resolve(ticket)

        assert result.resolved is False
        assert result.no_runbook_found is False  # sí había runbook, pero rechazó
        assert len(notifier.sent_messages) == 0

    def test_system_without_runbook_returns_no_runbook_found(self) -> None:
        """Sistema sin runbook registrado → no_runbook_found=True."""
        rb_engine, _, repo, _ = _make_engine_stack()
        ticket = _make_ticket(system=SystemTag.FRAUD, severity=Severity.P1)
        repo.save(ticket)

        result = rb_engine.try_auto_resolve(ticket)

        assert result.resolved is False
        assert result.no_runbook_found is True

    def test_failing_handler_does_not_propagate_exception(self) -> None:
        """
        Si el handler lanza una excepción, el motor NO debe propagarla.
        El ticket pasa a flujo normal (resolved=False).
        """
        def _crashing_handler(ticket: Ticket) -> bool:
            raise RuntimeError("Error simulado en runbook")

        rb_engine, _, repo, _ = _make_engine_stack()
        # Inyectamos un registro con handler que falla
        rb_engine._registry = {SystemTag.INGESTION: [_crashing_handler]}

        ticket = _make_ticket(system=SystemTag.INGESTION, severity=Severity.P1)
        repo.save(ticket)

        # No debe lanzar excepción
        result = rb_engine.try_auto_resolve(ticket)
        assert result.resolved is False

    def test_multiple_handlers_tries_first_then_second(self) -> None:
        """
        Con múltiples handlers, el motor prueba en orden y usa el primero exitoso.
        """
        call_log: list[str] = []

        def _handler_fails(ticket: Ticket) -> bool:
            call_log.append("fails")
            return False

        def _handler_succeeds(ticket: Ticket) -> bool:
            call_log.append("succeeds")
            return True

        rb_engine, _, repo, _ = _make_engine_stack()
        rb_engine._registry = {SystemTag.INGESTION: [_handler_fails, _handler_succeeds]}

        ticket = _make_ticket(system=SystemTag.INGESTION, severity=Severity.P1)
        repo.save(ticket)

        result = rb_engine.try_auto_resolve(ticket)

        assert call_log == ["fails", "succeeds"]
        assert result.resolved is True

    def test_payments_p2_resolved_by_latency_runbook(self) -> None:
        """P2/PAYMENTS → resuelto por el runbook de latencia."""
        rb_engine, _, repo, _ = _make_engine_stack()
        ticket = _make_ticket(system=SystemTag.PAYMENTS, severity=Severity.P2)
        repo.save(ticket)

        result = rb_engine.try_auto_resolve(ticket)

        assert result.resolved is True
        assert "payments_latency_clear" in result.runbook_name

    def test_payments_p1_not_auto_resolved(self) -> None:
        """P1/PAYMENTS requiere revisión humana — no se auto-resuelve."""
        rb_engine, _, repo, _ = _make_engine_stack()
        ticket = _make_ticket(system=SystemTag.PAYMENTS, severity=Severity.P1)
        repo.save(ticket)

        result = rb_engine.try_auto_resolve(ticket)
        assert result.resolved is False


# ---------------------------------------------------------------------------
# Test: flujo end-to-end completo (ingesta → runbook → resuelto)
# ---------------------------------------------------------------------------


class TestRunbookEndToEnd:
    def test_full_pipeline_ingestion_alert_auto_resolved(self) -> None:
        """
        Flujo completo: alerta de bot sobre ingesta → clasificada → motor de
        tickets → runbook → ticket RESOLVED automáticamente.

        Este test es el que se mostrará en la defensa para demostrar el
        valor end-to-end del sistema sin intervención humana.
        """
        from adapters.llm_mock import MockLLMClient
        from adapters.queue import InMemoryEventQueue
        from core.classifier import EventClassifier
        from core.ingestion import normalize_event

        # Setup del stack completo
        repo = InMemoryTicketRepository()
        queue = InMemoryEventQueue()
        ticket_engine = TicketEngine(ticket_repo=repo)
        notifier = LocalChatNotifier(log_to_console=False)
        classifier = EventClassifier(llm_client=MockLLMClient(), ticket_repo=repo)
        rb_engine = RunbookEngine(ticket_engine=ticket_engine, notifier=notifier)

        # 1. Webhook de Google Chat → normalizar evento
        payload = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/AAAA1111/messages/msg-001",
                "sender": {
                    "name": "users/monitoring-bot-001",
                    "type": "BOT",
                    "displayName": "Monitoring Bot",
                },
                "text": "ingesta de datos falló — pipeline nocturno — 03:14 AM",
                "space": {"name": "spaces/AAAA1111", "type": "ROOM", "displayName": "Soporte"},
                "createTime": "2026-09-08T03:14:00Z",
            },
            "eventTime": "2026-09-08T03:14:00Z",
        }

        event = normalize_event(payload)
        assert event.source.value == "machine"
        queue.publish(event)

        # 2. Clasificador procesa el evento
        consumed_event = queue.consume()
        classification = classifier.classify(consumed_event)
        assert classification.system == SystemTag.INGESTION
        assert classification.severity == Severity.P1

        # 3. Motor de tickets crea el ticket
        engine_result = ticket_engine.process(consumed_event, classification)
        ticket = engine_result.ticket
        assert engine_result.created is True
        assert ticket.status == TicketStatus.OPEN

        # 4. Runbook evalúa y resuelve automáticamente
        rb_result = rb_engine.try_auto_resolve(ticket)
        assert rb_result.resolved is True
        assert rb_result.runbook_name == "runbook:ingestion_pipeline_restart"

        # 5. Verificar estado final del sistema
        final_ticket = repo.get_by_id(ticket.ticket_id)
        assert final_ticket.status == TicketStatus.RESOLVED
        assert final_ticket.resolved_by_auto is True
        assert final_ticket.resolved_at is not None

        # 6. Verificar que se notificó la resolución
        resolution_msgs = [m for m in notifier.sent_messages if m.message_type == "resolution"]
        assert len(resolution_msgs) == 1
        assert "automáticamente" in resolution_msgs[0].text

        # Métrica clave: 100% de resolución automática para este tipo de alerta
        all_tickets = repo.list_all()
        auto_resolved = [t for t in all_tickets if t.resolved_by_auto]
        assert len(auto_resolved) / len(all_tickets) == 1.0
