"""
tests/test_models.py
====================
Tests unitarios para core/models.py

Valida:
  1. Creación correcta de RawEvent con valores por defecto.
  2. Creación correcta de Ticket con valores por defecto.
  3. Método touch() actualiza updated_at.
  4. Método is_overdue() con SLA vencido retorna True.
  5. Método is_overdue() con SLA vigente retorna False.
  6. Ticket resuelto NO se considera overdue aunque pasó el deadline.
  7. Los enums tienen los valores string correctos (necesario para serialización JSON / SQLite).
  8. Ticket.ticket_id y RawEvent.event_id son UUIDs únicos entre instancias.
"""

from datetime import UTC, datetime, timedelta

import pytest

from core.models import (
    EventSource,
    RawEvent,
    Severity,
    SupportLevel,
    SystemTag,
    Ticket,
    TicketStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_raw_event() -> RawEvent:
    return RawEvent(
        message="El pipeline de ingesta falló a las 3 AM.",
        sender_id="users/monitoring-bot-001",
        space_id="spaces/AAAA1111",
        source=EventSource.MACHINE,
    )


@pytest.fixture
def sample_ticket(sample_raw_event: RawEvent) -> Ticket:
    return Ticket(
        source_event_id=sample_raw_event.event_id,
        space_id=sample_raw_event.space_id,
        system=SystemTag.INGESTION,
        severity=Severity.P1,
        summary="Fallo en pipeline de ingesta de transacciones.",
    )


# ---------------------------------------------------------------------------
# Tests: RawEvent
# ---------------------------------------------------------------------------


class TestRawEvent:
    def test_default_fields_populated(self, sample_raw_event: RawEvent) -> None:
        """event_id y received_at deben generarse automáticamente."""
        assert sample_raw_event.event_id is not None
        assert isinstance(sample_raw_event.received_at, datetime)
        assert sample_raw_event.raw_payload == {}

    def test_source_is_machine(self, sample_raw_event: RawEvent) -> None:
        assert sample_raw_event.source == EventSource.MACHINE

    def test_unique_event_ids(self) -> None:
        """Dos RawEvents distintos deben tener IDs distintos."""
        e1 = RawEvent(
            message="msg1",
            sender_id="bot",
            space_id="spaces/X",
            source=EventSource.MACHINE,
        )
        e2 = RawEvent(
            message="msg2",
            sender_id="bot",
            space_id="spaces/X",
            source=EventSource.MACHINE,
        )
        assert e1.event_id != e2.event_id

    def test_human_event_source(self) -> None:
        event = RawEvent(
            message="Hay un error en pagos.",
            sender_id="users/analyst-001",
            space_id="spaces/BBBB2222",
            source=EventSource.HUMAN,
        )
        assert event.source == EventSource.HUMAN


# ---------------------------------------------------------------------------
# Tests: Ticket
# ---------------------------------------------------------------------------


class TestTicket:
    def test_default_status_is_open(self, sample_ticket: Ticket) -> None:
        assert sample_ticket.status == TicketStatus.OPEN

    def test_default_level_is_l1(self, sample_ticket: Ticket) -> None:
        assert sample_ticket.level == SupportLevel.L1

    def test_not_escalated_by_default(self, sample_ticket: Ticket) -> None:
        assert sample_ticket.escalated is False

    def test_not_auto_resolved_by_default(self, sample_ticket: Ticket) -> None:
        assert sample_ticket.resolved_by_auto is False

    def test_unique_ticket_ids(self, sample_raw_event: RawEvent) -> None:
        """Dos tickets distintos deben tener IDs distintos."""
        t1 = Ticket(
            source_event_id=sample_raw_event.event_id,
            space_id="spaces/X",
            system=SystemTag.PAYMENTS,
            severity=Severity.P2,
            summary="Issue 1",
        )
        t2 = Ticket(
            source_event_id=sample_raw_event.event_id,
            space_id="spaces/X",
            system=SystemTag.PAYMENTS,
            severity=Severity.P2,
            summary="Issue 2",
        )
        assert t1.ticket_id != t2.ticket_id

    def test_touch_updates_updated_at(self, sample_ticket: Ticket) -> None:
        """touch() debe actualizar updated_at a un valor posterior al original."""
        original_updated_at = sample_ticket.updated_at
        # Forzamos un delta mínimo para que el timestamp sea diferente
        import time
        time.sleep(0.01)
        sample_ticket.touch()
        assert sample_ticket.updated_at > original_updated_at


# ---------------------------------------------------------------------------
# Tests: is_overdue()
# ---------------------------------------------------------------------------


class TestIsOverdue:
    def test_overdue_when_deadline_passed(self, sample_ticket: Ticket) -> None:
        """Ticket con deadline en el pasado y estado OPEN → overdue."""
        sample_ticket.sla_deadline = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC)  # 1 hora después
        assert sample_ticket.is_overdue(now=now) is True

    def test_not_overdue_within_deadline(self, sample_ticket: Ticket) -> None:
        """Ticket con deadline en el futuro → no overdue."""
        sample_ticket.sla_deadline = datetime(2026, 12, 31, 23, 59, 0, tzinfo=UTC)
        now = datetime(2026, 9, 8, 14, 0, 0, tzinfo=UTC)
        assert sample_ticket.is_overdue(now=now) is False

    def test_no_deadline_not_overdue(self, sample_ticket: Ticket) -> None:
        """Ticket sin deadline definido nunca es overdue."""
        sample_ticket.sla_deadline = None
        assert sample_ticket.is_overdue() is False

    def test_resolved_ticket_not_overdue(self, sample_ticket: Ticket) -> None:
        """Un ticket ya resuelto no se considera overdue aunque pasó el deadline."""
        sample_ticket.sla_deadline = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        sample_ticket.status = TicketStatus.RESOLVED
        now = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
        assert sample_ticket.is_overdue(now=now) is False

    def test_closed_ticket_not_overdue(self, sample_ticket: Ticket) -> None:
        """Un ticket cerrado tampoco se considera overdue."""
        sample_ticket.sla_deadline = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        sample_ticket.status = TicketStatus.CLOSED
        now = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
        assert sample_ticket.is_overdue(now=now) is False


# ---------------------------------------------------------------------------
# Tests: Enum string values (crítico para serialización)
# ---------------------------------------------------------------------------


class TestEnumValues:
    def test_severity_values(self) -> None:
        assert Severity.P0.value == "P0"
        assert Severity.P1.value == "P1"
        assert Severity.P2.value == "P2"
        assert Severity.P3.value == "P3"

    def test_ticket_status_values(self) -> None:
        assert TicketStatus.OPEN.value == "open"
        assert TicketStatus.RESOLVED.value == "resolved"
        assert TicketStatus.ESCALATED.value == "escalated"

    def test_event_source_values(self) -> None:
        assert EventSource.HUMAN.value == "human"
        assert EventSource.MACHINE.value == "machine"

    def test_system_tag_values(self) -> None:
        assert SystemTag.TRANSACTIONS.value == "transactions"
        assert SystemTag.UNKNOWN.value == "unknown"
