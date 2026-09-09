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
    mttr_by_severity: dict[str, Optional[float]] = field(default_factory=dict)
    recurrent_systems: list[dict] = field(default_factory=list)
    volume_by_hour: dict[str, int] = field(default_factory=dict)


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

    # --- MTTR por severidad (minutos) ---
    mttr_by_severity: dict[str, Optional[float]] = {sev.value: None for sev in Severity}
    for sev in Severity:
        sev_resolved = [
            t for t in resolved_tickets
            if t.severity == sev and t.resolved_at and t.created_at
        ]
        if sev_resolved:
            durations = [(t.resolved_at - t.created_at).total_seconds() / 60 for t in sev_resolved]
            mttr_by_severity[sev.value] = round(sum(durations) / len(durations), 1)

    # --- Sistemas recurrentes (ordenados por volumen e impacto P0/P1) ---
    system_stats = []
    for tag in SystemTag:
        sys_tickets = [t for t in all_tickets if t.system == tag]
        p0_p1 = sum(1 for t in sys_tickets if t.severity in (Severity.P0, Severity.P1))
        open_c = sum(1 for t in sys_tickets if t.status not in (TicketStatus.RESOLVED, TicketStatus.CLOSED))
        system_stats.append({
            "system": tag.value,
            "total_incidents": len(sys_tickets),
            "p0_p1_count": p0_p1,
            "open_count": open_c,
        })
    recurrent_systems = sorted(
        system_stats,
        key=lambda x: (x["total_incidents"], x["p0_p1_count"]),
        reverse=True,
    )

    # --- Volumen por hora (distribución 00:00 a 23:00 UTC) ---
    volume_by_hour: dict[str, int] = {f"{h:02d}:00": 0 for h in range(24)}
    for ticket in all_tickets:
        if ticket.created_at:
            hour_key = f"{ticket.created_at.hour:02d}:00"
            volume_by_hour[hour_key] = volume_by_hour.get(hour_key, 0) + 1

    return DashboardMetrics(
        computed_at=check_time,
        total_tickets=len(all_tickets),
        total_open=len(open_tickets),
        volume_by_system=volume_by_system,
        open_by_severity=open_by_severity,
        auto_resolution_pct=auto_resolution_pct,
        avg_resolution_min=avg_resolution_min,
        sla_breaches=sla_breaches,
        mttr_by_severity=mttr_by_severity,
        recurrent_systems=recurrent_systems,
        volume_by_hour=volume_by_hour,
    )
