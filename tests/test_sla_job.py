"""
tests/test_sla_job.py
=====================
Tests unitarios para core/sla_job.py y adapters/chat_notifier.py

Cobertura del SLA Job:
  - Ticket vencido no escalado → escala a L2 y notifica
  - Ticket vencido ya escalado (L2) → no re-escala (idempotente)
  - Ticket estancado en L1 (inactividad > umbral) → escala y notifica
  - Ticket dentro de SLA y con actividad reciente → sin acción
  - Reporte contiene conteos correctos (total_checked, escalados)
  - Ticket resuelto no aparece en ningún chequeo
  - has_incidents True/False según estado del reporte
  - total_escalated suma overdue + stale correctamente

Cobertura del ChatNotifier:
  - send_ack(): campos correctos en el mensaje
  - send_status_update(): incluye ticket_id y estado
  - send_resolution(): duración correcta, tag automático
  - sent_messages se acumula correctamente
  - clear() vacía el historial
  - Payload compatible con formato Google Chat webhook
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from adapters.chat_notifier import LocalChatNotifier
from adapters.memory_repo import InMemoryTicketRepository
from core.classifier import ClassificationResult
from core.models import (
    EventSource,
    RawEvent,
    Severity,
    SupportLevel,
    SystemTag,
    Ticket,
    TicketStatus,
)
from core.sla_job import SLAJob, SLAJobReport
from core.ticket_engine import TicketEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(received_at: datetime | None = None) -> RawEvent:
    return RawEvent(
        message="Pipeline falló.",
        sender_id="users/bot-001",
        space_id="spaces/TEST",
        source=EventSource.MACHINE,
        received_at=received_at or datetime.now(UTC),
    )


def _make_classification(severity: Severity, system: SystemTag = SystemTag.TRANSACTIONS) -> ClassificationResult:
    return ClassificationResult(
        severity=severity,
        system=system,
        summary=f"Fallo en {system.value}.",
        classified_by="rules",
    )


def _make_setup() -> tuple[SLAJob, TicketEngine, InMemoryTicketRepository, LocalChatNotifier]:
    """Construye el stack completo para tests del SLA Job."""
    repo = InMemoryTicketRepository()
    engine = TicketEngine(ticket_repo=repo)
    notifier = LocalChatNotifier(log_to_console=False)
    job = SLAJob(ticket_repo=repo, ticket_engine=engine, notifier=notifier)
    return job, engine, repo, notifier


# ---------------------------------------------------------------------------
# Tests: SLA de resolución vencido
# ---------------------------------------------------------------------------


class TestOverdueDetection:
    def test_overdue_ticket_gets_escalated(self) -> None:
        """Ticket cuyo SLA de resolución venció → escala a L2."""
        job, engine, repo, notifier = _make_setup()

        # Crear ticket P1 hace 5 horas (SLA P1 = 4h → vencido)
        five_hours_ago = datetime.now(UTC) - timedelta(hours=5)
        event = _make_event(received_at=five_hours_ago)
        result = engine.process(event, _make_classification(Severity.P1))
        ticket = result.ticket

        assert ticket.level == SupportLevel.L1

        report = job.run(now=datetime.now(UTC))

        assert ticket.ticket_id in [t.ticket_id for t in report.overdue_escalated]
        # Verificar escalamiento efectivo en el objeto
        escalated = repo.get_by_id(ticket.ticket_id)
        assert escalated.level == SupportLevel.L2
        assert escalated.escalated is True

    def test_overdue_ticket_triggers_notification(self) -> None:
        """Escalamiento por SLA vencido debe generar notificación."""
        job, engine, repo, notifier = _make_setup()

        five_hours_ago = datetime.now(UTC) - timedelta(hours=5)
        engine.process(_make_event(received_at=five_hours_ago), _make_classification(Severity.P1))

        job.run(now=datetime.now(UTC))

        # Debe haber al menos un status_update
        status_msgs = [m for m in notifier.sent_messages if m.message_type == "status_update"]
        assert len(status_msgs) >= 1
        assert "SLA VENCIDO" in status_msgs[0].text

    def test_already_escalated_overdue_not_re_escalated(self) -> None:
        """Ticket vencido que ya está en L2 → no se vuelve a escalar."""
        job, engine, repo, notifier = _make_setup()

        five_hours_ago = datetime.now(UTC) - timedelta(hours=5)
        event = _make_event(received_at=five_hours_ago)
        result = engine.process(event, _make_classification(Severity.P1))
        ticket = result.ticket

        # Escalamos manualmente antes del job
        engine.escalate_to_l2(ticket, reason="Escalado previamente")
        notifier.clear()  # Limpiar notificaciones del escalamiento manual

        report = job.run(now=datetime.now(UTC))

        # El ticket aparece en already_overdue pero NO en overdue_escalated
        assert ticket.ticket_id in [t.ticket_id for t in report.already_overdue]
        assert ticket.ticket_id not in [t.ticket_id for t in report.overdue_escalated]
        # Sin nuevas notificaciones de status_update
        status_msgs = [m for m in notifier.sent_messages if m.message_type == "status_update"]
        assert len(status_msgs) == 0

    def test_p0_already_l2_overdue_counted_in_already_overdue(self) -> None:
        """P0 que nace en L2 y vence → aparece en already_overdue (ya escalado)."""
        job, engine, repo, notifier = _make_setup()

        two_hours_ago = datetime.now(UTC) - timedelta(hours=2)  # P0 SLA = 1h
        engine.process(_make_event(received_at=two_hours_ago), _make_classification(Severity.P0))

        report = job.run(now=datetime.now(UTC))

        assert len(report.already_overdue) == 1
        assert len(report.overdue_escalated) == 0  # ya era L2, no se re-escala


# ---------------------------------------------------------------------------
# Tests: Estancamiento (inactividad L1)
# ---------------------------------------------------------------------------


class TestStaleDetection:
    def test_stale_p1_ticket_escalated(self) -> None:
        """P1 en L1 sin actividad por > 60 min → escalado por estancamiento."""
        job, engine, repo, notifier = _make_setup()

        # Crear ticket P1 reciente pero sin actividad hace 90 min
        result = engine.process(_make_event(), _make_classification(Severity.P1))
        ticket = result.ticket
        ticket.updated_at = datetime.now(UTC) - timedelta(minutes=90)
        repo.save(ticket)

        report = job.run(now=datetime.now(UTC))

        assert ticket.ticket_id in [t.ticket_id for t in report.stale_escalated]
        escalated = repo.get_by_id(ticket.ticket_id)
        assert escalated.level == SupportLevel.L2

    def test_stale_notification_sent(self) -> None:
        """Escalamiento por estancamiento debe generar notificación con tag ESTANCADO."""
        job, engine, repo, notifier = _make_setup()

        result = engine.process(_make_event(), _make_classification(Severity.P1))
        ticket = result.ticket
        ticket.updated_at = datetime.now(UTC) - timedelta(minutes=90)
        repo.save(ticket)

        job.run(now=datetime.now(UTC))

        status_msgs = [m for m in notifier.sent_messages if m.message_type == "status_update"]
        assert len(status_msgs) >= 1
        assert "ESTANCADO" in status_msgs[0].text

    def test_active_ticket_not_stale(self) -> None:
        """Ticket con actividad reciente no es candidato a escalamiento."""
        job, engine, _, notifier = _make_setup()

        # Ticket actualizado hace solo 5 minutos
        result = engine.process(_make_event(), _make_classification(Severity.P1))
        # updated_at es now() por defecto → no estancado

        report = job.run(now=datetime.now(UTC))

        assert result.ticket.ticket_id not in [t.ticket_id for t in report.stale_escalated]

    def test_p3_not_stale_candidate(self) -> None:
        """P3 no tiene umbral de escalamiento → nunca es estancado."""
        job, engine, repo, notifier = _make_setup()

        result = engine.process(_make_event(), _make_classification(Severity.P3))
        ticket = result.ticket
        ticket.updated_at = datetime.now(UTC) - timedelta(hours=48)
        repo.save(ticket)

        report = job.run(now=datetime.now(UTC))

        assert ticket.ticket_id not in [t.ticket_id for t in report.stale_escalated]


# ---------------------------------------------------------------------------
# Tests: Reporte y métricas
# ---------------------------------------------------------------------------


class TestSLAJobReport:
    def test_empty_queue_report(self) -> None:
        """Con cero tickets abiertos, el reporte debe estar vacío."""
        job, _, _, _ = _make_setup()
        report = job.run(now=datetime.now(UTC))

        assert report.total_checked == 0
        assert report.total_escalated == 0
        assert report.has_incidents is False

    def test_total_checked_counts_all_open(self) -> None:
        """total_checked debe contar todos los tickets abiertos revisados."""
        job, engine, _, _ = _make_setup()

        for sev in [Severity.P1, Severity.P2, Severity.P3]:
            engine.process(_make_event(), _make_classification(sev))

        report = job.run(now=datetime.now(UTC))
        assert report.total_checked == 3

    def test_resolved_ticket_not_checked(self) -> None:
        """Tickets resueltos no deben aparecer en total_checked."""
        job, engine, repo, _ = _make_setup()

        result = engine.process(_make_event(), _make_classification(Severity.P1))
        engine.resolve(result.ticket, resolved_by="analyst-001")

        report = job.run(now=datetime.now(UTC))
        assert report.total_checked == 0

    def test_total_escalated_sums_both_categories(self) -> None:
        """total_escalated = overdue_escalated + stale_escalated."""
        job, engine, repo, _ = _make_setup()

        # Un ticket vencido (P1, 5h atrás)
        five_h = datetime.now(UTC) - timedelta(hours=5)
        engine.process(_make_event(received_at=five_h), _make_classification(Severity.P1))

        # Un ticket estancado (P2, reciente pero sin actividad)
        r2 = engine.process(_make_event(), _make_classification(Severity.P2, SystemTag.PAYMENTS))
        r2.ticket.updated_at = datetime.now(UTC) - timedelta(hours=5)
        repo.save(r2.ticket)

        report = job.run(now=datetime.now(UTC))
        assert report.total_escalated == len(report.overdue_escalated) + len(report.stale_escalated)

    def test_has_incidents_false_when_clean(self) -> None:
        """Sin problemas → has_incidents es False."""
        job, engine, _, _ = _make_setup()
        engine.process(_make_event(), _make_classification(Severity.P3))
        report = job.run(now=datetime.now(UTC))
        assert report.has_incidents is False


# ---------------------------------------------------------------------------
# Tests: ChatNotifier
# ---------------------------------------------------------------------------


class TestLocalChatNotifier:
    @pytest.fixture
    def notifier(self) -> LocalChatNotifier:
        return LocalChatNotifier(log_to_console=False)

    @pytest.fixture
    def ticket(self) -> Ticket:
        now = datetime.now(UTC)
        return Ticket(
            source_event_id="evt-001",
            space_id="spaces/TEST",
            system=SystemTag.TRANSACTIONS,
            severity=Severity.P1,
            summary="Fallo en transacciones.",
            created_at=now,
            sla_deadline=now + timedelta(hours=4),
        )

    @pytest.fixture
    def event(self) -> RawEvent:
        return RawEvent(
            message="Pipeline falló.",
            sender_id="users/analyst-001",
            space_id="spaces/TEST",
            source=EventSource.HUMAN,
        )

    def test_send_ack_recorded(self, notifier: LocalChatNotifier, event: RawEvent, ticket: Ticket) -> None:
        notifier.send_ack(event, ticket)
        assert len(notifier.sent_messages) == 1
        assert notifier.sent_messages[0].message_type == "ack"

    def test_send_ack_contains_ticket_id(
        self, notifier: LocalChatNotifier, event: RawEvent, ticket: Ticket
    ) -> None:
        notifier.send_ack(event, ticket)
        assert ticket.ticket_id[:8] in notifier.sent_messages[0].text

    def test_send_ack_contains_severity(
        self, notifier: LocalChatNotifier, event: RawEvent, ticket: Ticket
    ) -> None:
        notifier.send_ack(event, ticket)
        assert "P1" in notifier.sent_messages[0].text

    def test_send_ack_payload_has_text_key(
        self, notifier: LocalChatNotifier, event: RawEvent, ticket: Ticket
    ) -> None:
        """Payload debe tener la clave 'text' compatible con Google Chat webhook."""
        notifier.send_ack(event, ticket)
        assert "text" in notifier.sent_messages[0].payload

    def test_send_status_update_recorded(self, notifier: LocalChatNotifier, ticket: Ticket) -> None:
        notifier.send_status_update(ticket, message="SLA en riesgo.")
        assert len(notifier.sent_messages) == 1
        assert notifier.sent_messages[0].message_type == "status_update"
        assert "SLA en riesgo." in notifier.sent_messages[0].text

    def test_send_resolution_auto_tag(self, notifier: LocalChatNotifier, ticket: Ticket) -> None:
        """Resolución automática debe incluir el tag 🤖."""
        ticket.resolved_by = "runbook:restart"
        ticket.resolved_by_auto = True
        ticket.resolved_at = datetime.now(UTC)
        notifier.send_resolution(ticket)
        assert "automáticamente" in notifier.sent_messages[0].text

    def test_send_resolution_duration(self, notifier: LocalChatNotifier, ticket: Ticket) -> None:
        """La duración debe calcularse correctamente."""
        ticket.created_at = datetime(2026, 9, 8, 10, 0, 0, tzinfo=UTC)
        ticket.resolved_at = datetime(2026, 9, 8, 11, 30, 0, tzinfo=UTC)
        ticket.resolved_by = "analyst-001"
        notifier.send_resolution(ticket)
        assert "1h 30m" in notifier.sent_messages[0].text

    def test_clear_empties_messages(
        self, notifier: LocalChatNotifier, event: RawEvent, ticket: Ticket
    ) -> None:
        notifier.send_ack(event, ticket)
        notifier.send_status_update(ticket, "cambio")
        notifier.clear()
        assert len(notifier.sent_messages) == 0

    def test_multiple_messages_accumulate(
        self, notifier: LocalChatNotifier, event: RawEvent, ticket: Ticket
    ) -> None:
        notifier.send_ack(event, ticket)
        notifier.send_status_update(ticket, "escalado")
        notifier.send_resolution(ticket)
        assert len(notifier.sent_messages) == 3
