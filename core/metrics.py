"""
core/metrics.py
===============
Cálculo de métricas del sistema de soporte — función pura, sin HTTP.

Separar las métricas del endpoint HTTP permite:
  1. Testarlas sin levantar la app FastAPI.
  2. Reutilizarlas desde un script de línea de comandos o un job de BigQuery.
  3. En GCP: el mismo código alimentaría Looker Studio o Data Studio via Cloud Functions.

Métricas calculadas:
  - volume_by_system    : Número de tickets por sistema (todos los estados).
  - auto_resolution_pct : % de tickets resueltos automáticamente (por runbook).
  - avg_resolution_min  : Tiempo promedio de resolución en minutos (solo resueltos).
  - sla_breaches        : Tickets que vencieron su SLA y no están resueltos.
  - open_by_severity    : Tickets abiertos agrupados por severidad (para prioritización).
  - total_tickets       : Total de tickets en el sistema.
  - total_open          : Tickets actualmente abiertos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Optional

from core.models import Severity, SystemTag, Ticket, TicketStatus
from core.ports import TicketRepository


# ---------------------------------------------------------------------------
# Modelo de métricas
# ---------------------------------------------------------------------------


@dataclass
class DashboardMetrics:
    """
    Snapshot de métricas del sistema en un momento dado.

    Todos los campos son calculados a partir de los tickets en el repositorio.
    El campo `computed_at` permite cachear el resultado y saber cuándo expira.
    """

    computed_at: datetime
    total_tickets: int
    total_open: int
    volume_by_system: dict[str, int]
    open_by_severity: dict[str, int]
    auto_resolution_pct: float           # 0.0 – 100.0
    avg_resolution_min: Optional[float]  # None si no hay tickets resueltos aún
    sla_breaches: list[dict]             # Lista de tickets con SLA vencido


# ---------------------------------------------------------------------------
# Función de cálculo
# ---------------------------------------------------------------------------


def compute_metrics(
    repo: TicketRepository,
    now: datetime | None = None,
) -> DashboardMetrics:
    """
    Calcula las métricas del sistema a partir del repositorio de tickets.

    Parámetros
    ----------
    repo : Repositorio de tickets (cualquier implementación del puerto).
    now  : Timestamp de referencia. Si None, usa datetime.now(UTC).
           Inyectable para tests determinísticos.

    Retorna
    -------
    DashboardMetrics con todas las métricas calculadas.

    Complejidad: O(n) en número de tickets — suficiente para prueba técnica.
    En producción con millones de tickets, estas queries irían a BigQuery
    con agregaciones SQL en vez de Python en memoria.
    """
    check_time = now or datetime.now(UTC)
    all_tickets = repo.list_all()
    open_tickets = repo.list_open()

    # --- Volumen por sistema ---
    volume_by_system: dict[str, int] = {tag.value: 0 for tag in SystemTag}
    for ticket in all_tickets:
        volume_by_system[ticket.system.value] += 1

    # --- Tickets abiertos por severidad ---
    open_by_severity: dict[str, int] = {sev.value: 0 for sev in Severity}
    for ticket in open_tickets:
        open_by_severity[ticket.severity.value] += 1

    # --- % resolución automática ---
    resolved_tickets = [
        t for t in all_tickets
        if t.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED)
    ]
    auto_resolved = [t for t in resolved_tickets if t.resolved_by_auto]

    if resolved_tickets:
        auto_resolution_pct = round(len(auto_resolved) / len(resolved_tickets) * 100, 1)
    else:
        auto_resolution_pct = 0.0

    # --- Tiempo promedio de resolución (minutos) ---
    resolution_times: list[float] = []
    for ticket in resolved_tickets:
        if ticket.resolved_at and ticket.created_at:
            delta = ticket.resolved_at - ticket.created_at
            resolution_times.append(delta.total_seconds() / 60)

    avg_resolution_min = (
        round(sum(resolution_times) / len(resolution_times), 1)
        if resolution_times
        else None
    )

    # --- SLA breaches (tickets vencidos no resueltos) ---
    sla_breaches = []
    for ticket in open_tickets:
        if ticket.is_overdue(now=check_time):
            sla_breaches.append({
                "ticket_id": ticket.ticket_id,
                "system": ticket.system.value,
                "severity": ticket.severity.value,
                "level": ticket.level.value,
                "sla_deadline": (
                    ticket.sla_deadline.isoformat() if ticket.sla_deadline else None
                ),
                "minutes_overdue": (
                    round(
                        (check_time - ticket.sla_deadline).total_seconds() / 60, 1
                    )
                    if ticket.sla_deadline
                    else None
                ),
                "summary": ticket.summary[:100],
            })

    return DashboardMetrics(
        computed_at=check_time,
        total_tickets=len(all_tickets),
        total_open=len(open_tickets),
        volume_by_system=volume_by_system,
        open_by_severity=open_by_severity,
        auto_resolution_pct=auto_resolution_pct,
        avg_resolution_min=avg_resolution_min,
        sla_breaches=sla_breaches,
    )
