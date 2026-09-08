"""
core/sla_job.py
===============
Job de SLA — detecta incumplimientos y tickets estancados.

Diseño: función invocable, no un scheduler.
  En producción (GCP): Cloud Scheduler dispara un endpoint HTTP cada 5 minutos
  que llama a SLAJob.run(). Aquí se puede llamar manualmente o desde un test.
  Esto es más testeable que un background thread y más observable en producción.

El job ejecuta dos chequeos por ticket abierto:

  1. SLA de resolución vencido (is_overdue):
     El ticket superó su deadline de resolución (P0=1h, P1=4h, etc.).
     Acción: escalar a L2 si no lo está ya, notificar.

  2. Estancamiento (inactividad L1):
     El ticket está en L1 y no tuvo actividad en el umbral de su severidad
     (P1=60min, P2=240min). Diferente del SLA de resolución: el ticket puede
     estar dentro del deadline pero sin movimiento.
     Acción: escalar a L2, notificar.

Resultado del job:
  SLAJobReport con la lista de tickets afectados en cada categoría.
  Útil para métricas, logs y testing.

Dependencias inyectadas:
  ticket_repo : TicketRepository — para leer tickets abiertos.
  ticket_engine: TicketEngine     — para escalar tickets.
  notifier     : ChatNotifier     — para notificar al espacio de chat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from core.models import Ticket
from core.ports import ChatNotifier, TicketRepository
from core.ticket_engine import TicketEngine


# ---------------------------------------------------------------------------
# Reporte del job
# ---------------------------------------------------------------------------


@dataclass
class SLAJobReport:
    """
    Resultado de una ejecución del SLA job.

    Campos
    ------
    run_at            : Momento en que se ejecutó el job.
    overdue_escalated : Tickets escalados por SLA de resolución vencido.
    stale_escalated   : Tickets escalados por inactividad (estancamiento).
    already_overdue   : Tickets ya vencidos que seguían sin resolver
                        (para métricas de acumulación de deuda).
    total_checked     : Total de tickets abiertos revisados.
    """

    run_at: datetime
    overdue_escalated: list[Ticket] = field(default_factory=list)
    stale_escalated: list[Ticket] = field(default_factory=list)
    already_overdue: list[Ticket] = field(default_factory=list)
    total_checked: int = 0

    @property
    def total_escalated(self) -> int:
        """Total de tickets escalados en esta ejecución."""
        return len(self.overdue_escalated) + len(self.stale_escalated)

    @property
    def has_incidents(self) -> bool:
        """True si el job encontró algún problema que requiere atención."""
        return self.total_escalated > 0 or len(self.already_overdue) > 0


# ---------------------------------------------------------------------------
# SLA Job
# ---------------------------------------------------------------------------


class SLAJob:
    """
    Job de detección de incumplimientos de SLA y tickets estancados.

    Parámetros
    ----------
    ticket_repo   : Repositorio para leer tickets abiertos.
    ticket_engine : Motor de tickets para ejecutar escalamientos.
    notifier      : ChatNotifier para enviar notificaciones.
    """

    def __init__(
        self,
        ticket_repo: TicketRepository,
        ticket_engine: TicketEngine,
        notifier: ChatNotifier,
    ) -> None:
        self._repo = ticket_repo
        self._engine = ticket_engine
        self._notifier = notifier

    def run(self, now: datetime | None = None) -> SLAJobReport:
        """
        Ejecuta el job de SLA.

        Parámetros
        ----------
        now : Timestamp de referencia para los chequeos. Usar en tests para
              controlar el tiempo de forma determinística sin side effects.
              Si es None, usa datetime.now(UTC).

        Retorna
        -------
        SLAJobReport con el detalle de lo encontrado y ejecutado.

        Flujo por ticket:
          1. Ya vencido (overdue) → si no es L2, escalar y notificar.
          2. Estancado (stale L1) → escalar y notificar.
          Los dos chequeos son mutuamente excluyentes por ticket en esta ejecución:
          si un ticket se escala por overdue, no lo reescalamos por stale también.
        """
        check_time = now or datetime.now(UTC)
        report = SLAJobReport(run_at=check_time)

        open_tickets = self._repo.list_open()
        report.total_checked = len(open_tickets)

        # Obtener candidatos de estancamiento del engine (ya filtra por nivel y umbral)
        stale_candidates = {
            t.ticket_id
            for t in self._engine.get_escalation_candidates(now=check_time)
        }

        for ticket in open_tickets:
            self._process_ticket(
                ticket=ticket,
                check_time=check_time,
                stale_ids=stale_candidates,
                report=report,
            )

        return report

    def _process_ticket(
        self,
        ticket: Ticket,
        check_time: datetime,
        stale_ids: set[str],
        report: SLAJobReport,
    ) -> None:
        """
        Evalúa un ticket individual y aplica las acciones correspondientes.

        Separado de run() para facilitar testing de casos individuales.
        """
        is_overdue = ticket.is_overdue(now=check_time)
        is_stale = ticket.ticket_id in stale_ids

        if is_overdue:
            report.already_overdue.append(ticket)

            if not ticket.escalated:
                # Primer vencimiento → escalar y notificar
                reason = (
                    f"SLA de resolución vencido. "
                    f"Deadline era {ticket.sla_deadline.isoformat() if ticket.sla_deadline else 'N/A'}."
                )
                self._engine.escalate_to_l2(ticket, reason=reason)
                self._notifier.send_status_update(
                    ticket,
                    message=(
                        f"⚠️ SLA VENCIDO — Ticket #{ticket.ticket_id[:8]} "
                        f"({ticket.severity.value}/{ticket.system.value}) "
                        f"escalado a L2 automáticamente."
                    ),
                )
                report.overdue_escalated.append(ticket)

        elif is_stale:
            # Estancado pero dentro del deadline → escalar por inactividad
            reason = (
                f"Sin actividad en L1 por más del umbral permitido "
                f"para severidad {ticket.severity.value}."
            )
            self._engine.escalate_to_l2(ticket, reason=reason)
            self._notifier.send_status_update(
                ticket,
                message=(
                    f"🔔 ESTANCADO — Ticket #{ticket.ticket_id[:8]} "
                    f"({ticket.severity.value}/{ticket.system.value}) "
                    f"sin actividad — escalado a L2."
                ),
            )
            report.stale_escalated.append(ticket)
