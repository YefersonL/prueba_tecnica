"""
core/ticket_engine.py
=====================
Motor de tickets — transforma ClassificationResults en Tickets persistidos.

Responsabilidades de este módulo:
  1. Crear tickets con los metadatos correctos (severidad, sistema, nivel).
  2. Calcular el SLA deadline según la severidad (tabla configurable).
  3. Asignar el nivel inicial (L1 por defecto, L2 directo para P0).
  4. Escalar tickets a L2 cuando la severidad o el tiempo lo requieren.
  5. Marcar tickets como resueltos (manualmente o por runbook).

Lo que NO hace este módulo:
  - No clasifica eventos (eso es core/classifier.py).
  - No envía notificaciones (eso es ChatNotifier en Paso 6).
  - No chequea SLAs periódicamente (eso es el job de SLA en Paso 5).

Reglas de asignación de nivel (decisión de diseño):
  P0 → L2 directo. Un incidente de producción caída nunca pasa por L1.
  P1 → L1, con escalamiento automático si no hay actividad en SLA_ESCALATION_MINUTES.
  P2 → L1.
  P3 → L1.

Tabla de SLA (tiempos de respuesta desde creación del ticket):
  P0 → 15 min (primera respuesta), 1 hora (resolución)
  P1 → 1 hora (primera respuesta), 4 horas (resolución)
  P2 → 4 horas (primera respuesta), 24 horas (resolución)
  P3 → 24 horas (primera respuesta), 72 horas (resolución)

El SLA deadline que guardamos es el de RESOLUCIÓN (el más restrictivo).
La detección de incumplimiento del SLA de primera respuesta la hace el Job de SLA (Paso 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Optional

from core.classifier import ClassificationResult
from core.models import (
    RawEvent,
    Severity,
    SupportLevel,
    SystemTag,
    Ticket,
    TicketComment,
    TicketStatus,
)
from core.ports import TicketRepository

# ---------------------------------------------------------------------------
# Configuración de SLA por severidad (resolución deadline)
# ---------------------------------------------------------------------------

SLA_RESOLUTION_HOURS: dict[Severity, float] = {
    Severity.P0: 1.0,    # 1 hora
    Severity.P1: 4.0,    # 4 horas
    Severity.P2: 24.0,   # 24 horas
    Severity.P3: 72.0,   # 72 horas
}

# Tiempo máximo sin actividad antes de escalar automáticamente (en minutos)
# Solo aplica a P1 que empieza en L1. P0 ya va a L2 directamente.
SLA_ESCALATION_MINUTES: dict[Severity, int] = {
    Severity.P1: 60,   # 1 hora sin actividad → L2
    Severity.P2: 240,  # 4 horas sin actividad → L2
}


def _compute_sla_deadline(severity: Severity, from_time: datetime) -> datetime:
    """Calcula el deadline de resolución según la severidad."""
    hours = SLA_RESOLUTION_HOURS[severity]
    return from_time + timedelta(hours=hours)


def _initial_level(severity: Severity) -> SupportLevel:
    """
    P0 va directo a L2. Todo lo demás empieza en L1.

    Justificación: un P0 requiere ingeniería senior inmediata.
    Hacer pasar un P0 por L1 agrega latencia que en producción costaría dinero.
    """
    return SupportLevel.L2 if severity == Severity.P0 else SupportLevel.L1


# ---------------------------------------------------------------------------
# Resultado del motor de tickets
# ---------------------------------------------------------------------------


@dataclass
class TicketEngineResult:
    """
    Resultado de procesar un evento en el motor de tickets.

    Campos
    ------
    ticket       : El ticket creado o el ticket existente (si duplicado).
    created      : True si se creó un ticket nuevo; False si era duplicado.
    was_escalated: True si el ticket fue escalado a L2 durante esta operación.
    """

    ticket: Ticket
    created: bool
    was_escalated: bool = False


# ---------------------------------------------------------------------------
# Motor de tickets
# ---------------------------------------------------------------------------


class TicketEngine:
    """
    Motor de tickets: crea, asigna y escala tickets de soporte.

    Dependencias inyectadas:
      ticket_repo: TicketRepository — para persistir y consultar tickets.

    Stateless entre llamadas (toda la persistencia va al repo).
    """

    def __init__(
        self,
        ticket_repo: TicketRepository | None = None,
        repo: TicketRepository | None = None,
    ) -> None:
        self._repo = ticket_repo or repo
        if not self._repo:
            raise ValueError("ticket_repo is required")

    def process(
        self, event: RawEvent, classification: ClassificationResult
    ) -> TicketEngineResult:
        """
        Procesa un ClassificationResult y crea o agrupa el ticket correspondiente.

        Si el clasificador detectó duplicado: retorna el ticket existente sin crear uno nuevo.
        Si no: crea un ticket nuevo, lo persiste y retorna el resultado.

        Flujo:
          1. ¿Es duplicado? → retornar ticket existente.
          2. Crear nuevo ticket con severidad, sistema y nivel inicial.
          3. Calcular SLA deadline.
          4. Si P0: ya está en L2, marcar como escalado.
          5. Persistir y retornar.
        """
        if classification.is_duplicate and classification.existing_ticket:
            return TicketEngineResult(
                ticket=classification.existing_ticket,
                created=False,
                was_escalated=False,
            )

        now = event.received_at
        level = _initial_level(classification.severity)
        sla_deadline = _compute_sla_deadline(classification.severity, from_time=now)

        ticket = Ticket(
            source_event_id=event.event_id,
            space_id=event.space_id,
            system=classification.system,
            severity=classification.severity,
            summary=classification.summary,
            source=event.source,
            requester_id=event.sender_id,
            level=level,
            created_at=now,
            updated_at=now,
            sla_deadline=sla_deadline,
            # P0 nace ya escalado — va directo a L2
            escalated=(level == SupportLevel.L2),
            status=TicketStatus.OPEN,
        )

        self._repo.save(ticket)

        return TicketEngineResult(
            ticket=ticket,
            created=True,
            was_escalated=(level == SupportLevel.L2),
        )

    def escalate_to_l2(self, ticket: Ticket, reason: str = "") -> Ticket:
        """
        Escala un ticket a L2 y persiste el cambio.

        Puede ser llamado por:
          - El Job de SLA (SLA vencido o estancamiento).
          - Un analista L1 que determina que excede su capacidad.
          - Automáticamente si la severidad es P0 (ya en process()).

        Parámetros
        ----------
        ticket : Ticket a escalar. Debe estar en estado OPEN o IN_PROGRESS.
        reason : Motivo del escalamiento (para auditoría en summary).

        Retorna el ticket modificado (mismo objeto, mutado in-place + persistido).
        """
        if ticket.level == SupportLevel.L2:
            return ticket  # Ya estaba en L2, idempotente

        ticket.level = SupportLevel.L2
        ticket.escalated = True
        ticket.status = TicketStatus.ESCALATED
        if reason:
            ticket.summary = f"{ticket.summary}\n[Escalado L2] {reason}"
        ticket.touch()
        self._repo.save(ticket)
        return ticket

    def resolve(
        self,
        ticket: Ticket,
        resolved_by: str,
        auto: bool = False,
    ) -> Ticket:
        """
        Marca un ticket como resuelto y persiste el cambio.

        Parámetros
        ----------
        ticket      : Ticket a resolver.
        resolved_by : ID del usuario o sistema que resolvió (ej. "runbook:restart_pipeline").
        auto        : True si fue resuelto por automatización (runbook), False si manual.

        Retorna el ticket modificado.
        """
        now = datetime.now(UTC)
        ticket.status = TicketStatus.RESOLVED
        ticket.resolved_by = resolved_by
        ticket.resolved_by_auto = auto
        ticket.resolved_at = now
        ticket.touch()
        self._repo.save(ticket)
        return ticket

    def update_status(self, ticket: Ticket, new_status: TicketStatus) -> Ticket:
        """
        Actualiza el estado de un ticket (ej. OPEN → IN_PROGRESS).

        Usado por L1 cuando toma el ticket ("estoy trabajando en esto").
        """
        ticket.status = new_status
        ticket.touch()
        self._repo.save(ticket)
        return ticket

    def agregar_comentario(
        self,
        ticket_id: str,
        comentario: str,
        actor: str,
        nuevo_estado: Optional[TicketStatus] = None,
    ) -> tuple[Ticket, TicketComment]:
        """
        Registra un comentario de seguimiento y transiciona el estado del ticket automáticamente.

        Parámetros
        ----------
        ticket_id    : ID del ticket al que se agregará el comentario.
        comentario   : Texto del seguimiento o mensaje para el usuario.
        actor        : Nombre del agente, usuario o sistema que comenta (ej. 'Agente Carlos').
        nuevo_estado : Estado al que debe transicionar el ticket (ej. IN_PROGRESS, WAITING_USER).
                       Si es None, conserva el estado actual.

        Retorna
        -------
        Tupla (ticket_actualizado, comentario_creado).

        Lanza
        -----
        ValueError: Si el ticket no existe o el comentario está vacío.
        """
        comentario_limpio = (comentario or "").strip()
        if not comentario_limpio:
            raise ValueError("El comentario no puede estar vacío.")

        ticket = self._repo.get_by_id(ticket_id)
        if not ticket:
            raise ValueError(f"No se encontró el ticket con ID '{ticket_id}'.")

        if nuevo_estado is not None:
            ticket.status = nuevo_estado
            if nuevo_estado in (TicketStatus.RESOLVED, TicketStatus.CLOSED) and not ticket.resolved_at:
                ticket.resolved_at = datetime.now(UTC)
                ticket.resolved_by = actor
        elif ticket.status == TicketStatus.OPEN:
            ticket.status = TicketStatus.IN_PROGRESS

        ticket.touch()

        comment = TicketComment(
            ticket_id=ticket_id,
            author=actor.strip() or "Agente",
            content=comentario_limpio,
            created_at=ticket.updated_at,
            new_status=ticket.status,
        )

        ticket.comments.append(comment)
        self._repo.save(ticket)
        self._repo.add_comment(comment)

        return ticket, comment

    def get_escalation_candidates(self, now: Optional[datetime] = None) -> list[Ticket]:
        """
        Retorna tickets que deberían escalarse por tiempo sin actividad.

        Candidatos: tickets en L1 con updated_at anterior al umbral de escalación
        para su severidad. Solo P1 y P2 tienen umbral definido (P0 ya es L2, P3 no escala).

        Este método es usado por el Job de SLA (Paso 5) para detectar estancamiento.
        Aquí solo identifica candidatos — el Job decide si escalar.
        """
        check_time = now or datetime.now(UTC)
        candidates: list[Ticket] = []

        for ticket in self._repo.list_open():
            if ticket.level != SupportLevel.L1:
                continue
            if ticket.severity not in SLA_ESCALATION_MINUTES:
                continue

            threshold_minutes = SLA_ESCALATION_MINUTES[ticket.severity]
            inactivity_threshold = check_time - timedelta(minutes=threshold_minutes)

            if ticket.updated_at <= inactivity_threshold:
                candidates.append(ticket)

        return candidates
