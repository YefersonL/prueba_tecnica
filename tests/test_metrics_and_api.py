"""
tests/test_metrics.py
=====================
Tests unitarios para core/metrics.py

Cobertura:
  - Sistema vacío → métricas en cero, listas vacías
  - volume_by_system cuenta por sistema correctamente
  - auto_resolution_pct con cero resueltos → 0.0
  - auto_resolution_pct con todos auto → 100.0
  - auto_resolution_pct mixto → valor exacto
  - avg_resolution_min None si no hay resueltos
  - avg_resolution_min calculado correctamente
  - sla_breaches lista tickets vencidos no resueltos
  - sla_breaches excluye tickets resueltos (aunque vencidos)
  - open_by_severity cuenta tickets abiertos por prioridad
  - total_tickets y total_open correctos

tests/test_api.py
=================
Tests de integración para api/main.py usando TestClient de FastAPI (sin red real).

Cobertura:
  - GET /health → 200 con status "ok"
  - POST /webhook/google-chat con payload MACHINE válido → 200 + campos correctos
  - POST /webhook/google-chat con payload HUMAN válido → 200
  - POST /webhook/google-chat payload inválido (no-MESSAGE) → 422
  - POST /webhook/google-chat payload JSON malformado → 400
  - POST /sla/run → 200 + campos del reporte
  - GET /dashboard → 200 + métricas correctas después de varios tickets
  - GET /tickets → 200 + lista de tickets
  - Ticket de ingesta P1 → resuelto automáticamente (runbook)
"""

from __future__ import annotations

# ============================================================
# test_metrics.py
# ============================================================

from datetime import UTC, datetime, timedelta

import pytest

from adapters.memory_repo import InMemoryTicketRepository
from core.metrics import DashboardMetrics, compute_metrics
from core.models import Severity, SupportLevel, SystemTag, Ticket, TicketStatus


def _make_ticket(
    system: SystemTag = SystemTag.TRANSACTIONS,
    severity: Severity = Severity.P1,
    status: TicketStatus = TicketStatus.OPEN,
    resolved_by_auto: bool = False,
    created_at: datetime | None = None,
    resolved_at: datetime | None = None,
    sla_deadline: datetime | None = None,
) -> Ticket:
    now = datetime.now(UTC)
    return Ticket(
        source_event_id="evt-001",
        space_id="spaces/TEST",
        system=system,
        severity=severity,
        summary="Test",
        status=status,
        resolved_by_auto=resolved_by_auto,
        created_at=created_at or now,
        updated_at=now,
        resolved_at=resolved_at,
        sla_deadline=sla_deadline,
    )


class TestComputeMetrics:
    @pytest.fixture
    def empty_repo(self) -> InMemoryTicketRepository:
        return InMemoryTicketRepository()

    def test_empty_repo_returns_zeros(self, empty_repo: InMemoryTicketRepository) -> None:
        metrics = compute_metrics(empty_repo)
        assert metrics.total_tickets == 0
        assert metrics.total_open == 0
        assert metrics.auto_resolution_pct == 0.0
        assert metrics.avg_resolution_min is None
        assert metrics.sla_breaches == []

    def test_volume_by_system(self, empty_repo: InMemoryTicketRepository) -> None:
        empty_repo.save(_make_ticket(system=SystemTag.TRANSACTIONS))
        empty_repo.save(_make_ticket(system=SystemTag.TRANSACTIONS))
        empty_repo.save(_make_ticket(system=SystemTag.PAYMENTS))
        metrics = compute_metrics(empty_repo)
        assert metrics.volume_by_system["transactions"] == 2
        assert metrics.volume_by_system["payments"] == 1
        assert metrics.volume_by_system["fraud"] == 0

    def test_auto_resolution_pct_zero_when_no_resolved(self, empty_repo: InMemoryTicketRepository) -> None:
        empty_repo.save(_make_ticket(status=TicketStatus.OPEN))
        metrics = compute_metrics(empty_repo)
        assert metrics.auto_resolution_pct == 0.0

    def test_auto_resolution_pct_100_when_all_auto(self, empty_repo: InMemoryTicketRepository) -> None:
        for _ in range(3):
            empty_repo.save(_make_ticket(status=TicketStatus.RESOLVED, resolved_by_auto=True))
        metrics = compute_metrics(empty_repo)
        assert metrics.auto_resolution_pct == 100.0

    def test_auto_resolution_pct_mixed(self, empty_repo: InMemoryTicketRepository) -> None:
        """2 auto + 2 manual = 50%."""
        for _ in range(2):
            empty_repo.save(_make_ticket(status=TicketStatus.RESOLVED, resolved_by_auto=True))
        for _ in range(2):
            empty_repo.save(_make_ticket(status=TicketStatus.RESOLVED, resolved_by_auto=False))
        metrics = compute_metrics(empty_repo)
        assert metrics.auto_resolution_pct == 50.0

    def test_avg_resolution_min_none_when_no_resolved(self, empty_repo: InMemoryTicketRepository) -> None:
        empty_repo.save(_make_ticket(status=TicketStatus.OPEN))
        metrics = compute_metrics(empty_repo)
        assert metrics.avg_resolution_min is None

    def test_avg_resolution_min_calculated(self, empty_repo: InMemoryTicketRepository) -> None:
        """Resolución en exactamente 60 minutos → avg = 60.0"""
        created = datetime(2026, 9, 8, 10, 0, 0, tzinfo=UTC)
        resolved = datetime(2026, 9, 8, 11, 0, 0, tzinfo=UTC)
        t = _make_ticket(
            status=TicketStatus.RESOLVED,
            created_at=created,
            resolved_at=resolved,
        )
        empty_repo.save(t)
        metrics = compute_metrics(empty_repo)
        assert metrics.avg_resolution_min == 60.0

    def test_sla_breaches_lists_overdue_open_tickets(self, empty_repo: InMemoryTicketRepository) -> None:
        """Ticket abierto con deadline en el pasado → aparece en sla_breaches."""
        past_deadline = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        t = _make_ticket(status=TicketStatus.OPEN, sla_deadline=past_deadline)
        empty_repo.save(t)
        now = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
        metrics = compute_metrics(empty_repo, now=now)
        assert len(metrics.sla_breaches) == 1
        assert metrics.sla_breaches[0]["ticket_id"] == t.ticket_id

    def test_sla_breaches_excludes_resolved(self, empty_repo: InMemoryTicketRepository) -> None:
        """Ticket resuelto no aparece en sla_breaches aunque venció."""
        past_deadline = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        t = _make_ticket(
            status=TicketStatus.RESOLVED,
            sla_deadline=past_deadline,
        )
        empty_repo.save(t)
        now = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
        metrics = compute_metrics(empty_repo, now=now)
        assert len(metrics.sla_breaches) == 0

    def test_open_by_severity(self, empty_repo: InMemoryTicketRepository) -> None:
        empty_repo.save(_make_ticket(severity=Severity.P0, status=TicketStatus.OPEN))
        empty_repo.save(_make_ticket(severity=Severity.P1, status=TicketStatus.OPEN))
        empty_repo.save(_make_ticket(severity=Severity.P1, status=TicketStatus.OPEN))
        empty_repo.save(_make_ticket(severity=Severity.P1, status=TicketStatus.RESOLVED))  # no cuenta
        metrics = compute_metrics(empty_repo)
        assert metrics.open_by_severity["P0"] == 1
        assert metrics.open_by_severity["P1"] == 2
        assert metrics.open_by_severity["P2"] == 0

    def test_total_counts(self, empty_repo: InMemoryTicketRepository) -> None:
        empty_repo.save(_make_ticket(status=TicketStatus.OPEN))
        empty_repo.save(_make_ticket(status=TicketStatus.OPEN))
        empty_repo.save(_make_ticket(status=TicketStatus.RESOLVED))
        metrics = compute_metrics(empty_repo)
        assert metrics.total_tickets == 3
        assert metrics.total_open == 2


# ============================================================
# test_api.py — Tests de integración FastAPI
# ============================================================

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """
    TestClient con dependencias sobreescritas para tests.
    Usa InMemoryTicketRepository para no tocar disco entre tests.
    """
    from api.dependencies import (
        get_classifier,
        get_event_queue,
        get_notifier,
        get_runbook_engine,
        get_sla_job,
        get_ticket_engine,
        get_ticket_repo,
    )
    from api.main import app
    from adapters.chat_notifier import LocalChatNotifier
    from adapters.llm_mock import MockLLMClient
    from adapters.memory_repo import InMemoryTicketRepository
    from adapters.queue import InMemoryEventQueue
    from core.classifier import EventClassifier
    from core.runbook_engine import RunbookEngine
    from core.sla_job import SLAJob
    from core.ticket_engine import TicketEngine

    # Construir stack con adaptadores en memoria
    repo = InMemoryTicketRepository()
    queue = InMemoryEventQueue()
    notifier = LocalChatNotifier(log_to_console=False)
    t_engine = TicketEngine(ticket_repo=repo)
    classifier = EventClassifier(llm_client=MockLLMClient(), ticket_repo=repo)
    rb_engine = RunbookEngine(ticket_engine=t_engine, notifier=notifier)
    sla_job = SLAJob(ticket_repo=repo, ticket_engine=t_engine, notifier=notifier)

    # Sobreescribir dependencias FastAPI
    app.dependency_overrides[get_ticket_repo] = lambda: repo
    app.dependency_overrides[get_event_queue] = lambda: queue
    app.dependency_overrides[get_notifier] = lambda: notifier
    app.dependency_overrides[get_ticket_engine] = lambda: t_engine
    app.dependency_overrides[get_classifier] = lambda: classifier
    app.dependency_overrides[get_runbook_engine] = lambda: rb_engine
    app.dependency_overrides[get_sla_job] = lambda: sla_job

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def _machine_payload(
    text: str = "ingesta de datos falló — batch nocturno",
    space_id: str = "spaces/TEST",
) -> dict:
    return {
        "type": "MESSAGE",
        "eventTime": "2026-09-08T03:14:00Z",
        "message": {
            "name": f"{space_id}/messages/msg-001",
            "sender": {
                "name": "users/monitoring-bot-001",
                "displayName": "Monitoring Bot",
                "type": "BOT",
            },
            "createTime": "2026-09-08T03:14:00Z",
            "text": text,
            "space": {"name": space_id, "type": "ROOM", "displayName": "Soporte"},
        },
    }


def _human_payload(text: str = "El sistema de fraude está caído en producción.") -> dict:
    return {
        "type": "MESSAGE",
        "eventTime": "2026-09-08T09:30:00Z",
        "message": {
            "name": "spaces/TEST/messages/msg-002",
            "sender": {
                "name": "users/analyst-001",
                "displayName": "María López",
                "type": "HUMAN",
            },
            "createTime": "2026-09-08T09:30:00Z",
            "text": text,
            "space": {"name": "spaces/TEST", "type": "ROOM", "displayName": "Soporte"},
        },
    }


class TestHealthEndpoint:
    def test_health_returns_200(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_returns_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.json()["status"] == "ok"


class TestWebhookEndpoint:
    def test_machine_event_returns_200(self, client: TestClient) -> None:
        resp = client.post("/webhook/google-chat", json=_machine_payload())
        assert resp.status_code == 200

    def test_machine_event_returns_ticket_fields(self, client: TestClient) -> None:
        resp = client.post("/webhook/google-chat", json=_machine_payload())
        data = resp.json()
        assert "ticket_id" in data
        assert "severity" in data
        assert "system" in data
        assert data["classified_by"] == "rules"

    def test_ingestion_p1_auto_resolved(self, client: TestClient) -> None:
        """Alerta de ingesta P1 → runbook la resuelve automáticamente."""
        resp = client.post(
            "/webhook/google-chat",
            json=_machine_payload(text="ingesta de datos falló — pipeline nocturno"),
        )
        data = resp.json()
        assert resp.status_code == 200
        assert data["resolved_automatically"] is True
        assert data["runbook_used"] == "runbook:ingestion_pipeline_restart"

    def test_human_event_uses_llm(self, client: TestClient) -> None:
        resp = client.post("/webhook/google-chat", json=_human_payload())
        data = resp.json()
        assert resp.status_code == 200
        assert data["classified_by"] == "llm"

    def test_invalid_event_type_returns_422(self, client: TestClient) -> None:
        bad_payload = {"type": "CARD_CLICKED"}
        resp = client.post("/webhook/google-chat", json=bad_payload)
        assert resp.status_code == 422

    def test_added_to_space_returns_welcome_200(self, client: TestClient) -> None:
        payload = {"type": "ADDED_TO_SPACE"}
        resp = client.post("/webhook/google-chat", json=payload)
        assert resp.status_code == 200
        assert "¡Hola!" in resp.json()["ack_message"]


    def test_malformed_json_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/webhook/google-chat",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_ticket_created_flag(self, client: TestClient) -> None:
        """Primer evento de un sistema → ticket_created=True."""
        resp = client.post(
            "/webhook/google-chat",
            json=_machine_payload(
                text="pipeline de pagos con latencia — p99 > 5000ms",
                space_id="spaces/PAYMENTS-TEST",
            ),
        )
        assert resp.json()["ticket_created"] is True


class TestSLAJobEndpoint:
    def test_sla_run_returns_200(self, client: TestClient) -> None:
        resp = client.post("/sla/run")
        assert resp.status_code == 200

    def test_sla_run_returns_report_fields(self, client: TestClient) -> None:
        resp = client.post("/sla/run")
        data = resp.json()
        assert "total_checked" in data
        assert "overdue_escalated" in data
        assert "stale_escalated" in data
        assert "has_incidents" in data


class TestDashboardEndpoint:
    def test_dashboard_returns_200(self, client: TestClient) -> None:
        resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_dashboard_returns_metrics_fields(self, client: TestClient) -> None:
        resp = client.get("/dashboard")
        data = resp.json()
        assert "total_tickets" in data
        assert "auto_resolution_pct" in data
        assert "volume_by_system" in data
        assert "sla_breaches" in data


class TestTicketsEndpoint:
    def test_tickets_returns_200(self, client: TestClient) -> None:
        resp = client.get("/tickets")
        assert resp.status_code == 200

    def test_tickets_returns_list(self, client: TestClient) -> None:
        resp = client.get("/tickets")
        assert isinstance(resp.json(), list)


class TestFrontendEndpoint:
    def test_root_returns_frontend_html(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "FintechDesk" in resp.text

    def test_front_static_css_and_js(self, client: TestClient) -> None:
        css_resp = client.get("/front/style.css")
        assert css_resp.status_code == 200
        js_resp = client.get("/front/app.js")
        assert js_resp.status_code == 200

