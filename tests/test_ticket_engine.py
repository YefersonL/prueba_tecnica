"""
tests/test_ticket_engine.py
===========================
Tests unitarios para core/ticket_engine.py y adapters/sqlite_repo.py

Cobertura del motor de tickets:
  - Creación de ticket nuevo con campos correctos
  - P0 → L2 directo, escalado=True desde creación
  - P1/P2/P3 → L1, escalado=False
  - SLA deadlines calculados correctamente por severidad
  - Ticket duplicado → retorna existente, created=False
  - escalate_to_l2(): escala correctamente
  - escalate_to_l2(): idempotente si ya es L2
  - resolve(): marca resuelto, persiste timestamps
  - resolve(): resolved_by_auto=True para runbooks
  - update_status(): transición de estado
  - get_escalation_candidates(): detecta tickets estancados

Cobertura del SQLiteTicketRepository:
  - save() / get_by_id() round-trip
  - Upsert (save sobre ticket existente)
  - find_open_by_system() excluye RESOLVED/CLOSED
  - list_open() excluye RESOLVED/CLOSED
  - list_all() retorna todos
  - Preservación correcta de datetimes UTC
  - Usa :memory: para no tocar disco
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from adapters.memory_repo import InMemoryTicketRepository
from adapters.sqlite_repo import SQLiteTicketRepository
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
from core.ticket_engine import (
    SLA_RESOLUTION_HOURS,
    TicketEngine,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    text: str = "Pipeline falló.",
    source: EventSource = EventSource.MACHINE,
    received_at: datetime | None = None,
) -> RawEvent:
    return RawEvent(
        message=text,
        sender_id="users/bot-001",
        space_id="spaces/TEST",
        source=source,
        received_at=received_at or datetime.now(UTC),
    )


def _make_classification(
    severity: Severity = Severity.P1,
    system: SystemTag = SystemTag.TRANSACTIONS,
    is_duplicate: bool = False,
    existing_ticket: Ticket | None = None,
) -> ClassificationResult:
    return ClassificationResult(
        severity=severity,
        system=system,
        summary="Fallo en pipeline de transacciones.",
        is_duplicate=is_duplicate,
        existing_ticket=existing_ticket,
        classified_by="rules",
    )


@pytest.fixture
def engine() -> TicketEngine:
    return TicketEngine(ticket_repo=InMemoryTicketRepository())


@pytest.fixture
def engine_with_repo() -> tuple[TicketEngine, InMemoryTicketRepository]:
    repo = InMemoryTicketRepository()
    return TicketEngine(ticket_repo=repo), repo


# ---------------------------------------------------------------------------
# Tests: Creación de tickets
# ---------------------------------------------------------------------------


class TestTicketCreation:
    def test_creates_ticket_with_correct_fields(self, engine: TicketEngine) -> None:
        """El ticket creado debe tener los campos del evento y la clasificación."""
        event = _make_event()
        classification = _make_classification(severity=Severity.P1, system=SystemTag.TRANSACTIONS)
        result = engine.process(event, classification)

        assert result.created is True
        assert result.ticket.severity == Severity.P1
        assert result.ticket.system == SystemTag.TRANSACTIONS
        assert result.ticket.status == TicketStatus.OPEN
        assert result.ticket.space_id == event.space_id
        assert result.ticket.source_event_id == event.event_id

    def test_p0_goes_directly_to_l2(self, engine: TicketEngine) -> None:
        """P0 nunca pasa por L1 — nace en L2 escalado."""
        result = engine.process(_make_event(), _make_classification(severity=Severity.P0))
        assert result.ticket.level == SupportLevel.L2
        assert result.ticket.escalated is True
        assert result.was_escalated is True

    def test_p1_starts_at_l1(self, engine: TicketEngine) -> None:
        result = engine.process(_make_event(), _make_classification(severity=Severity.P1))
        assert result.ticket.level == SupportLevel.L1
        assert result.ticket.escalated is False

    def test_p2_starts_at_l1(self, engine: TicketEngine) -> None:
        result = engine.process(_make_event(), _make_classification(severity=Severity.P2))
        assert result.ticket.level == SupportLevel.L1

    def test_p3_starts_at_l1(self, engine: TicketEngine) -> None:
        result = engine.process(_make_event(), _make_classification(severity=Severity.P3))
        assert result.ticket.level == SupportLevel.L1

    def test_ticket_is_persisted(
        self, engine_with_repo: tuple[TicketEngine, InMemoryTicketRepository]
    ) -> None:
        """El ticket debe estar en el repositorio después de process()."""
        eng, repo = engine_with_repo
        result = eng.process(_make_event(), _make_classification())
        persisted = repo.get_by_id(result.ticket.ticket_id)
        assert persisted is not None
        assert persisted.ticket_id == result.ticket.ticket_id


# ---------------------------------------------------------------------------
# Tests: SLA Deadlines
# ---------------------------------------------------------------------------


class TestSLADeadlines:
    @pytest.mark.parametrize("severity", list(Severity))
    def test_sla_deadline_computed_correctly(
        self, engine: TicketEngine, severity: Severity
    ) -> None:
        """El deadline debe ser received_at + SLA_RESOLUTION_HOURS[severity]."""
        received_at = datetime(2026, 9, 8, 10, 0, 0, tzinfo=UTC)
        event = _make_event(received_at=received_at)
        classification = _make_classification(severity=severity)
        result = engine.process(event, classification)

        expected_deadline = received_at + timedelta(hours=SLA_RESOLUTION_HOURS[severity])
        assert result.ticket.sla_deadline == expected_deadline

    def test_p0_deadline_is_1_hour(self, engine: TicketEngine) -> None:
        """P0 → deadline en 1 hora exacta."""
        received_at = datetime(2026, 9, 8, 3, 14, 0, tzinfo=UTC)
        event = _make_event(received_at=received_at)
        result = engine.process(event, _make_classification(severity=Severity.P0))
        expected = datetime(2026, 9, 8, 4, 14, 0, tzinfo=UTC)
        assert result.ticket.sla_deadline == expected


# ---------------------------------------------------------------------------
# Tests: De-duplicación en el motor
# ---------------------------------------------------------------------------


class TestDuplicateHandling:
    def test_duplicate_returns_existing_ticket(self, engine: TicketEngine) -> None:
        """Si es duplicado, retorna el ticket existente sin crear uno nuevo."""
        existing_ticket = Ticket(
            source_event_id="evt-old",
            space_id="spaces/TEST",
            system=SystemTag.TRANSACTIONS,
            severity=Severity.P1,
            summary="Ticket original",
        )
        classification = _make_classification(
            is_duplicate=True,
            existing_ticket=existing_ticket,
        )
        result = engine.process(_make_event(), classification)

        assert result.created is False
        assert result.ticket.ticket_id == existing_ticket.ticket_id
        assert result.was_escalated is False

    def test_duplicate_does_not_persist_new_ticket(
        self, engine_with_repo: tuple[TicketEngine, InMemoryTicketRepository]
    ) -> None:
        """No se debe crear un ticket nuevo en el repo cuando es duplicado."""
        eng, repo = engine_with_repo
        existing = Ticket(
            source_event_id="evt-old",
            space_id="spaces/TEST",
            system=SystemTag.PAYMENTS,
            severity=Severity.P2,
            summary="Ticket existente",
        )
        repo.save(existing)
        initial_count = len(repo.list_all())

        classification = _make_classification(
            system=SystemTag.PAYMENTS,
            is_duplicate=True,
            existing_ticket=existing,
        )
        eng.process(_make_event(), classification)

        assert len(repo.list_all()) == initial_count  # sin ticket nuevo


# ---------------------------------------------------------------------------
# Tests: Escalamiento
# ---------------------------------------------------------------------------


class TestEscalation:
    def test_escalate_to_l2_changes_level_and_status(self, engine: TicketEngine) -> None:
        """escalate_to_l2() debe cambiar level, escalated y status."""
        result = engine.process(_make_event(), _make_classification(severity=Severity.P1))
        ticket = result.ticket

        assert ticket.level == SupportLevel.L1
        engine.escalate_to_l2(ticket, reason="SLA vencido — sin respuesta en 60 min")

        assert ticket.level == SupportLevel.L2
        assert ticket.escalated is True
        assert ticket.status == TicketStatus.ESCALATED

    def test_escalate_to_l2_is_idempotent(self, engine: TicketEngine) -> None:
        """Escalar un ticket que ya es L2 no debe cambiar nada."""
        result = engine.process(_make_event(), _make_classification(severity=Severity.P0))
        ticket = result.ticket
        original_summary = ticket.summary

        engine.escalate_to_l2(ticket, reason="Redundante")
        # El summary no debe cambiar porque ya era L2
        assert ticket.level == SupportLevel.L2
        assert ticket.summary == original_summary  # no se concatenó el reason

    def test_escalate_appends_reason_to_summary(self, engine: TicketEngine) -> None:
        """El motivo de escalamiento debe quedar en el summary para auditoría."""
        result = engine.process(_make_event(), _make_classification(severity=Severity.P1))
        ticket = result.ticket
        engine.escalate_to_l2(ticket, reason="P1 sin respuesta por 60 min")
        assert "P1 sin respuesta por 60 min" in ticket.summary


# ---------------------------------------------------------------------------
# Tests: Resolución
# ---------------------------------------------------------------------------


class TestResolution:
    def test_resolve_manual(self, engine: TicketEngine) -> None:
        """Resolución manual: status RESOLVED, resolved_by_auto=False."""
        result = engine.process(_make_event(), _make_classification())
        ticket = result.ticket
        engine.resolve(ticket, resolved_by="analyst-001", auto=False)

        assert ticket.status == TicketStatus.RESOLVED
        assert ticket.resolved_by == "analyst-001"
        assert ticket.resolved_by_auto is False
        assert ticket.resolved_at is not None

    def test_resolve_auto(self, engine: TicketEngine) -> None:
        """Resolución automática por runbook: resolved_by_auto=True."""
        result = engine.process(_make_event(), _make_classification())
        ticket = result.ticket
        engine.resolve(ticket, resolved_by="runbook:restart_pipeline", auto=True)

        assert ticket.resolved_by_auto is True
        assert "runbook" in ticket.resolved_by

    def test_resolved_ticket_not_overdue(self, engine: TicketEngine) -> None:
        """Un ticket resuelto antes del deadline no debe ser overdue."""
        received_at = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)
        event = _make_event(received_at=received_at)
        result = engine.process(event, _make_classification(severity=Severity.P1))
        ticket = result.ticket

        # Resolvemos el ticket 30 min después (bien dentro del SLA de 4h)
        engine.resolve(ticket, resolved_by="analyst-001")
        now = datetime(2026, 9, 8, 5, 0, 0, tzinfo=UTC)  # 5h después, ya resuelto
        assert ticket.is_overdue(now=now) is False


# ---------------------------------------------------------------------------
# Tests: get_escalation_candidates
# ---------------------------------------------------------------------------


class TestEscalationCandidates:
    def test_stale_p1_is_candidate(
        self, engine_with_repo: tuple[TicketEngine, InMemoryTicketRepository]
    ) -> None:
        """Ticket P1 en L1 sin actividad > 60 min → candidato a escalamiento."""
        eng, repo = engine_with_repo
        old_time = datetime.now(UTC) - timedelta(minutes=90)
        result = eng.process(
            _make_event(received_at=old_time),
            _make_classification(severity=Severity.P1),
        )
        # Forzamos updated_at a hace 90 min para simular inactividad
        ticket = result.ticket
        ticket.updated_at = old_time
        repo.save(ticket)

        candidates = eng.get_escalation_candidates(now=datetime.now(UTC))
        assert any(t.ticket_id == ticket.ticket_id for t in candidates)

    def test_recently_updated_p1_not_candidate(self, engine: TicketEngine) -> None:
        """Ticket P1 actualizado hace 5 min no debe ser candidato."""
        result = engine.process(_make_event(), _make_classification(severity=Severity.P1))
        candidates = engine.get_escalation_candidates(now=datetime.now(UTC))
        assert not any(t.ticket_id == result.ticket.ticket_id for t in candidates)

    def test_p0_already_l2_not_candidate(self, engine: TicketEngine) -> None:
        """P0 ya nace en L2 → nunca es candidato de escalamiento."""
        result = engine.process(_make_event(), _make_classification(severity=Severity.P0))
        candidates = engine.get_escalation_candidates()
        assert not any(t.ticket_id == result.ticket.ticket_id for t in candidates)

    def test_p3_never_escalates(
        self, engine_with_repo: tuple[TicketEngine, InMemoryTicketRepository]
    ) -> None:
        """P3 no tiene umbral de escalamiento → nunca es candidato."""
        eng, repo = engine_with_repo
        old_time = datetime.now(UTC) - timedelta(hours=48)
        result = eng.process(
            _make_event(received_at=old_time),
            _make_classification(severity=Severity.P3),
        )
        ticket = result.ticket
        ticket.updated_at = old_time
        repo.save(ticket)

        candidates = eng.get_escalation_candidates(now=datetime.now(UTC))
        assert not any(t.ticket_id == ticket.ticket_id for t in candidates)


# ---------------------------------------------------------------------------
# Tests: SQLiteTicketRepository
# ---------------------------------------------------------------------------


class TestSQLiteTicketRepository:
    """
    Usa ":memory:" para que cada test tenga una BD limpia sin tocar disco.
    """

    @pytest.fixture
    def repo(self) -> SQLiteTicketRepository:
        return SQLiteTicketRepository(db_path=":memory:")

    def _make_ticket(self, **kwargs) -> Ticket:
        defaults = dict(
            source_event_id="evt-001",
            space_id="spaces/TEST",
            system=SystemTag.PAYMENTS,
            severity=Severity.P2,
            summary="Test ticket",
            created_at=datetime(2026, 9, 8, 10, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 9, 8, 10, 0, 0, tzinfo=UTC),
            sla_deadline=datetime(2026, 9, 9, 10, 0, 0, tzinfo=UTC),
        )
        defaults.update(kwargs)
        return Ticket(**defaults)

    def test_save_and_get_by_id_roundtrip(self, repo: SQLiteTicketRepository) -> None:
        """Un ticket guardado debe recuperarse idéntico."""
        ticket = self._make_ticket()
        repo.save(ticket)
        retrieved = repo.get_by_id(ticket.ticket_id)

        assert retrieved is not None
        assert retrieved.ticket_id == ticket.ticket_id
        assert retrieved.severity == ticket.severity
        assert retrieved.system == ticket.system
        assert retrieved.status == ticket.status

    def test_get_by_id_returns_none_for_missing(self, repo: SQLiteTicketRepository) -> None:
        assert repo.get_by_id("no-existe") is None

    def test_upsert_overwrites_existing(self, repo: SQLiteTicketRepository) -> None:
        """save() sobre un ticket existente debe actualizarlo (upsert)."""
        ticket = self._make_ticket()
        repo.save(ticket)

        ticket.status = TicketStatus.RESOLVED
        ticket.resolved_by = "analyst-001"
        repo.save(ticket)  # segunda vez → upsert

        retrieved = repo.get_by_id(ticket.ticket_id)
        assert retrieved.status == TicketStatus.RESOLVED
        assert retrieved.resolved_by == "analyst-001"

    def test_datetime_utc_preserved(self, repo: SQLiteTicketRepository) -> None:
        """Los datetimes deben mantenerse UTC-aware tras la round-trip."""
        ticket = self._make_ticket(
            created_at=datetime(2026, 9, 8, 3, 14, 0, tzinfo=UTC),
            sla_deadline=datetime(2026, 9, 8, 4, 14, 0, tzinfo=UTC),
        )
        repo.save(ticket)
        retrieved = repo.get_by_id(ticket.ticket_id)

        assert retrieved.created_at.tzinfo is not None
        assert retrieved.created_at == ticket.created_at
        assert retrieved.sla_deadline == ticket.sla_deadline

    def test_find_open_by_system_excludes_resolved(self, repo: SQLiteTicketRepository) -> None:
        """find_open_by_system no debe retornar tickets RESOLVED ni CLOSED."""
        open_t = self._make_ticket(status=TicketStatus.OPEN)
        resolved_t = self._make_ticket(status=TicketStatus.RESOLVED)
        repo.save(open_t)
        repo.save(resolved_t)

        results = repo.find_open_by_system(SystemTag.PAYMENTS)
        ids = [t.ticket_id for t in results]
        assert open_t.ticket_id in ids
        assert resolved_t.ticket_id not in ids

    def test_list_open_excludes_closed(self, repo: SQLiteTicketRepository) -> None:
        open_t = self._make_ticket(system=SystemTag.FRAUD, status=TicketStatus.OPEN)
        closed_t = self._make_ticket(system=SystemTag.AUTH, status=TicketStatus.CLOSED)
        repo.save(open_t)
        repo.save(closed_t)

        open_list = repo.list_open()
        ids = [t.ticket_id for t in open_list]
        assert open_t.ticket_id in ids
        assert closed_t.ticket_id not in ids

    def test_list_all_returns_all(self, repo: SQLiteTicketRepository) -> None:
        t1 = self._make_ticket(system=SystemTag.AUTH, status=TicketStatus.OPEN)
        t2 = self._make_ticket(system=SystemTag.FRAUD, status=TicketStatus.RESOLVED)
        repo.save(t1)
        repo.save(t2)

        all_tickets = repo.list_all()
        ids = [t.ticket_id for t in all_tickets]
        assert t1.ticket_id in ids
        assert t2.ticket_id in ids

    def test_add_and_get_comments(self, repo: SQLiteTicketRepository) -> None:
        """Los comentarios asociados a un ticket deben persistirse y recuperarse en orden."""
        from core.models import TicketComment
        ticket = self._make_ticket()
        repo.save(ticket)

        c1 = TicketComment(
            content="Primer seguimiento",
            author="Agente 1",
            ticket_id=ticket.ticket_id,
        )
        c2 = TicketComment(
            content="Esperando respuesta del cliente",
            author="Agente 2",
            ticket_id=ticket.ticket_id,
            new_status=TicketStatus.WAITING_USER,
        )
        repo.add_comment(c1)
        repo.add_comment(c2)

        comments = repo.get_comments(ticket.ticket_id)
        assert len(comments) == 2
        assert comments[0].content == "Primer seguimiento"
        assert comments[1].new_status == TicketStatus.WAITING_USER

        # Al recuperar el ticket, sus comentarios deben estar cargados
        retrieved = repo.get_by_id(ticket.ticket_id)
        assert retrieved is not None
        assert len(retrieved.comments) == 2


class TestTicketComments:
    """Pruebas para agregar_comentario en TicketEngine."""

    @pytest.fixture
    def engine_and_repo(self):
        from adapters.memory_repo import InMemoryTicketRepository
        repo = InMemoryTicketRepository()
        engine = TicketEngine(repo=repo)
        return engine, repo

    def test_agregar_comentario_default_transition(self, engine_and_repo):
        engine, repo = engine_and_repo
        ticket = Ticket(
            ticket_id="tk-test-1",
            source_event_id="ev-1",
            space_id="spaces/test",
            system=SystemTag.PAYMENTS,
            severity=Severity.P1,
            summary="Problema de pago",
            status=TicketStatus.OPEN,
            requester_id="users/123",
        )
        repo.save(ticket)

        updated, comment = engine.agregar_comentario(
            ticket_id="tk-test-1",
            comentario="Revisando los logs de transacción",
            actor="Agente Ana",
        )

        assert updated.status == TicketStatus.IN_PROGRESS
        assert len(updated.comments) == 1
        assert updated.comments[0].author == "Agente Ana"
        assert updated.comments[0].content == "Revisando los logs de transacción"
        assert comment.content == "Revisando los logs de transacción"

    def test_agregar_comentario_explicit_status(self, engine_and_repo):
        engine, repo = engine_and_repo
        ticket = Ticket(
            ticket_id="tk-test-2",
            source_event_id="ev-2",
            space_id="spaces/test",
            system=SystemTag.AUTH,
            severity=Severity.P2,
            summary="No puede loguear",
            status=TicketStatus.OPEN,
        )
        repo.save(ticket)

        updated, comment = engine.agregar_comentario(
            ticket_id="tk-test-2",
            comentario="¿Podrías confirmar tu correo electrónico?",
            actor="Agente Carlos",
            nuevo_estado=TicketStatus.WAITING_USER,
        )

        assert updated.status == TicketStatus.WAITING_USER
        assert updated.comments[0].new_status == TicketStatus.WAITING_USER
        assert comment.new_status == TicketStatus.WAITING_USER

    def test_agregar_comentario_empty_raises(self, engine_and_repo):
        engine, repo = engine_and_repo
        with pytest.raises(ValueError, match="no puede estar vacío"):
            engine.agregar_comentario("any-id", "   ", "Agente")

    def test_agregar_comentario_not_found_raises(self, engine_and_repo):
        engine, repo = engine_and_repo
        with pytest.raises(ValueError, match="No se encontr"):
            engine.agregar_comentario("non-existent", "Seguimiento", "Agente")

