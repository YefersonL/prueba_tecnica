"""
tests/test_classifier.py
========================
Tests unitarios para core/classifier.py

Cobertura:
  Reglas determinísticas (MACHINE):
    - Pipeline de transacciones → P1/TRANSACTIONS
    - Ingesta fallida → P1/INGESTION
    - Alerta de pagos con latencia → P2/PAYMENTS
    - Emoji ❌ con fallo → catchall P2
    - Texto de máquina sin patrón conocido → fallback LLM
    - Alerta de producción caída → P0

  Clasificación LLM (HUMAN):
    - Texto de fraude → MockLLM devuelve P1/FRAUD
    - Texto ambiguo bajo impacto → P3/UNKNOWN

  De-duplicación:
    - Ticket abierto reciente del mismo sistema → is_duplicate=True
    - Ticket abierto del mismo sistema fuera de ventana → no duplicado
    - Ticket cerrado del mismo sistema → no duplicado
    - Sistema UNKNOWN nunca es duplicado
    - Evento sin duplicado → is_duplicate=False

  classified_by:
    - Alerta de máquina con match → "rules"
    - Texto humano → "llm"
    - Máquina sin match → "llm"
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from adapters.llm_mock import MockLLMClient
from adapters.memory_repo import InMemoryTicketRepository
from core.classifier import EventClassifier
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
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    text: str,
    source: EventSource = EventSource.MACHINE,
    received_at: datetime | None = None,
) -> RawEvent:
    return RawEvent(
        message=text,
        sender_id="users/bot-001" if source == EventSource.MACHINE else "users/analyst-001",
        space_id="spaces/TEST",
        source=source,
        received_at=received_at or datetime.now(UTC),
    )


def _make_open_ticket(
    system: SystemTag,
    created_at: datetime | None = None,
    status: TicketStatus = TicketStatus.OPEN,
) -> Ticket:
    return Ticket(
        source_event_id="evt-000",
        space_id="spaces/TEST",
        system=system,
        severity=Severity.P1,
        summary="Ticket existente",
        status=status,
        created_at=created_at or datetime.now(UTC),
    )


@pytest.fixture
def classifier() -> EventClassifier:
    """Clasificador con MockLLMClient y repositorio en memoria vacío."""
    return EventClassifier(
        llm_client=MockLLMClient(),
        ticket_repo=InMemoryTicketRepository(),
    )


@pytest.fixture
def classifier_with_repo() -> tuple[EventClassifier, InMemoryTicketRepository]:
    """Clasificador con repositorio expuesto para pre-cargar tickets."""
    repo = InMemoryTicketRepository()
    clf = EventClassifier(
        llm_client=MockLLMClient(),
        ticket_repo=repo,
        dedup_window_minutes=30,
    )
    return clf, repo


# ---------------------------------------------------------------------------
# Tests: Reglas determinísticas (MACHINE events)
# ---------------------------------------------------------------------------


class TestDeterministicRules:
    def test_pipeline_transacciones_p1(self, classifier: EventClassifier) -> None:
        """Alerta de pipeline de transacciones → P1/TRANSACTIONS por regla."""
        event = _make_event("❌ Falló el pipeline de transacciones — 03:14 AM")
        result = classifier.classify(event)
        assert result.severity == Severity.P1
        assert result.system == SystemTag.TRANSACTIONS
        assert result.classified_by == "rules"

    def test_ingesta_fallida_p1(self, classifier: EventClassifier) -> None:
        """Fallo de ingesta → P1/INGESTION."""
        event = _make_event("ingesta de datos falló en el batch nocturno")
        result = classifier.classify(event)
        assert result.severity == Severity.P1
        assert result.system == SystemTag.INGESTION
        assert result.classified_by == "rules"

    def test_pagos_latencia_p2(self, classifier: EventClassifier) -> None:
        """Pipeline de pagos con latencia → P2/PAYMENTS."""
        event = _make_event("⚠️ Pipeline de pagos con latencia elevada — p99 > 5000ms")
        result = classifier.classify(event)
        assert result.severity == Severity.P2
        assert result.system == SystemTag.PAYMENTS
        assert result.classified_by == "rules"

    def test_produccion_caida_p0(self, classifier: EventClassifier) -> None:
        """'producción caída' → P0 (máxima severidad)."""
        event = _make_event("ALERTA: producción caída — todos los servicios afectados")
        result = classifier.classify(event)
        assert result.severity == Severity.P0
        assert result.classified_by == "rules"

    def test_catchall_emoji_fallo(self, classifier: EventClassifier) -> None:
        """Emoji ❌ seguido de 'falló' → catchall P2/UNKNOWN."""
        event = _make_event("❌ falló el servicio de notificaciones — 10:00 AM")
        result = classifier.classify(event)
        assert result.severity == Severity.P2
        assert result.classified_by == "rules"

    def test_machine_without_pattern_falls_back_to_llm(
        self, classifier: EventClassifier
    ) -> None:
        """Alerta de máquina sin patrón conocido → fallback a LLM."""
        event = _make_event("Sistema XYZ reiniciado automáticamente")
        result = classifier.classify(event)
        # Sin keywords de reglas → llm
        assert result.classified_by == "llm"

    def test_summary_is_populated(self, classifier: EventClassifier) -> None:
        """El resumen nunca debe estar vacío."""
        event = _make_event("❌ Falló el pipeline de ingesta de transacciones — 03:14 AM")
        result = classifier.classify(event)
        assert result.summary
        assert len(result.summary) > 0


# ---------------------------------------------------------------------------
# Tests: Clasificación LLM (HUMAN events)
# ---------------------------------------------------------------------------


class TestLLMClassification:
    def test_fraude_humano_p1(self, classifier: EventClassifier) -> None:
        """Texto humano mencionando fraude → MockLLM retorna P1/FRAUD."""
        event = _make_event(
            "El sistema de detección de fraude está caído. "
            "Transacciones sin revisión pasando a producción.",
            source=EventSource.HUMAN,
        )
        result = classifier.classify(event)
        assert result.severity == Severity.P1
        assert result.system == SystemTag.FRAUD
        assert result.classified_by == "llm"

    def test_transacciones_humano(self, classifier: EventClassifier) -> None:
        """Analista reportando problema de transacciones → MockLLM P1/TRANSACTIONS."""
        event = _make_event(
            "Las transacciones del batch nocturno no se procesaron. "
            "Afecta el cierre contable.",
            source=EventSource.HUMAN,
        )
        result = classifier.classify(event)
        assert result.severity == Severity.P1
        assert result.system == SystemTag.TRANSACTIONS

    def test_ambiguous_low_impact(self, classifier: EventClassifier) -> None:
        """Texto ambiguo sin keywords → MockLLM retorna P3/UNKNOWN."""
        event = _make_event(
            "Vi algo raro en el sistema, no sé si es importante.",
            source=EventSource.HUMAN,
        )
        result = classifier.classify(event)
        assert result.severity == Severity.P3
        assert result.system == SystemTag.UNKNOWN

    def test_human_always_uses_llm(self, classifier: EventClassifier) -> None:
        """Mensajes HUMAN siempre van por LLM, incluso si tienen palabras de reglas."""
        event = _make_event(
            "ingesta de datos falló",  # tiene keyword de regla
            source=EventSource.HUMAN,  # pero es HUMAN → va por LLM
        )
        result = classifier.classify(event)
        assert result.classified_by == "llm"


# ---------------------------------------------------------------------------
# Tests: De-duplicación
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_open_recent_ticket_is_duplicate(
        self, classifier_with_repo: tuple[EventClassifier, InMemoryTicketRepository]
    ) -> None:
        """Ticket abierto del mismo sistema dentro de la ventana → duplicado."""
        clf, repo = classifier_with_repo
        # Pre-cargar ticket abierto de TRANSACTIONS hace 10 min
        existing = _make_open_ticket(
            system=SystemTag.TRANSACTIONS,
            created_at=datetime.now(UTC) - timedelta(minutes=10),
        )
        repo.save(existing)

        event = _make_event("❌ Falló el pipeline de transacciones — 08:00 AM")
        result = clf.classify(event)

        assert result.is_duplicate is True
        assert result.existing_ticket is not None
        assert result.existing_ticket.ticket_id == existing.ticket_id

    def test_old_ticket_outside_window_not_duplicate(
        self, classifier_with_repo: tuple[EventClassifier, InMemoryTicketRepository]
    ) -> None:
        """Ticket del mismo sistema pero fuera de la ventana → no duplicado."""
        clf, repo = classifier_with_repo
        old_ticket = _make_open_ticket(
            system=SystemTag.TRANSACTIONS,
            created_at=datetime.now(UTC) - timedelta(minutes=60),  # > 30 min
        )
        repo.save(old_ticket)

        event = _make_event("❌ Falló el pipeline de transacciones — nuevo incidente")
        result = clf.classify(event)

        assert result.is_duplicate is False
        assert result.existing_ticket is None

    def test_resolved_ticket_not_duplicate(
        self, classifier_with_repo: tuple[EventClassifier, InMemoryTicketRepository]
    ) -> None:
        """Ticket resuelto del mismo sistema → no duplicado (ya está cerrado)."""
        clf, repo = classifier_with_repo
        resolved = _make_open_ticket(
            system=SystemTag.TRANSACTIONS,
            created_at=datetime.now(UTC) - timedelta(minutes=5),
            status=TicketStatus.RESOLVED,
        )
        repo.save(resolved)

        event = _make_event("❌ Falló el pipeline de transacciones — nueva alerta")
        result = clf.classify(event)

        assert result.is_duplicate is False

    def test_unknown_system_never_duplicate(
        self, classifier_with_repo: tuple[EventClassifier, InMemoryTicketRepository]
    ) -> None:
        """Sistema UNKNOWN no participa en de-duplicación (muy amplio)."""
        clf, repo = classifier_with_repo
        unknown_ticket = _make_open_ticket(
            system=SystemTag.UNKNOWN,
            created_at=datetime.now(UTC) - timedelta(minutes=2),
        )
        repo.save(unknown_ticket)

        event = _make_event("❌ falló algo desconocido")
        result = clf.classify(event)

        # catchall devuelve UNKNOWN → no hay dedup
        assert result.is_duplicate is False

    def test_no_open_tickets_not_duplicate(
        self, classifier_with_repo: tuple[EventClassifier, InMemoryTicketRepository]
    ) -> None:
        """Sin tickets previos → nunca es duplicado."""
        clf, _ = classifier_with_repo
        event = _make_event("❌ Falló el pipeline de transacciones — primera alerta")
        result = clf.classify(event)
        assert result.is_duplicate is False

    def test_different_system_not_duplicate(
        self, classifier_with_repo: tuple[EventClassifier, InMemoryTicketRepository]
    ) -> None:
        """Ticket de sistema distinto no cuenta como duplicado."""
        clf, repo = classifier_with_repo
        payments_ticket = _make_open_ticket(
            system=SystemTag.PAYMENTS,
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        repo.save(payments_ticket)

        # El evento es de TRANSACTIONS, no PAYMENTS
        event = _make_event("❌ Falló el pipeline de transacciones")
        result = clf.classify(event)

        assert result.is_duplicate is False
