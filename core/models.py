"""
core/models.py
==============
Modelos de dominio del sistema de soporte.

Regla de oro: este módulo NO importa nada de adapters/, api/, SQLite, FastAPI, ni
ningún cliente externo. Es Python puro + stdlib. Esto garantiza que la lógica de
negocio pueda probarse de forma aislada y migrarse a GCP sin cambios.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumeraciones
# ---------------------------------------------------------------------------


class EventSource(str, Enum):
    """Origen del evento recibido desde Google Chat."""

    HUMAN = "human"       # Analista de riesgo / comercial / finanzas
    MACHINE = "machine"   # Bot de monitoreo / pipeline automatizado


class Severity(str, Enum):
    """
    Severidad del ticket, alineada con prioridades estándar SRE.

    P0 → producción caída, impacto crítico e inmediato.
    P1 → degradación severa o pérdida de datos.
    P2 → funcionalidad afectada pero con workaround.
    P3 → bajo impacto, informativo.
    """

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class TicketStatus(str, Enum):
    """Ciclo de vida del ticket."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    CLOSED = "closed"


class SupportLevel(str, Enum):
    """Nivel de soporte asignado al ticket."""

    L1 = "L1"  # Atención inicial / triaje
    L2 = "L2"  # Ingeniería / escalamiento


class SystemTag(str, Enum):
    """
    Sistemas monitoreados identificados en la fintech.
    Valor `UNKNOWN` cuando el clasificador no puede determinar el sistema.
    """

    TRANSACTIONS = "transactions"
    PAYMENTS = "payments"
    FRAUD = "fraud"
    REPORTING = "reporting"
    AUTH = "auth"
    INGESTION = "ingestion"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# RawEvent — evento normalizado tal como llega del webhook de Google Chat
# ---------------------------------------------------------------------------


@dataclass
class RawEvent:
    """
    Representación canónica de un mensaje recibido desde Google Chat.

    El adaptador de ingesta (adapters/ingestion.py) es responsable de
    transformar el payload JSON crudo de Google Chat a esta estructura.
    Una vez aquí, la lógica de negocio trabaja con RawEvent, no con JSON.

    Campos
    ------
    event_id    : Identificador único del evento (generado si el webhook no lo provee).
    message     : Texto original del mensaje.
    sender_id   : ID del remitente según Google Chat (usuario o bot).
    space_id    : ID del espacio/chat donde se publicó.
    source      : HUMAN o MACHINE (resuelto por la función de ingesta).
    received_at : Timestamp UTC de recepción (no de creación del mensaje).
    raw_payload : Payload JSON original para trazabilidad / auditoría.
    """

    message: str
    sender_id: str
    space_id: str
    source: EventSource
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    raw_payload: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Ticket — entidad central del sistema de soporte
# ---------------------------------------------------------------------------


@dataclass
class Ticket:
    """
    Ticket de soporte derivado de uno o más RawEvents.

    Diseñado para persistirse en SQLite (adaptador local) o Cloud SQL /
    Firestore en GCP. Los campos de timestamp son todos UTC-aware para
    evitar bugs de zona horaria en los cálculos de SLA.

    Campos de negocio
    -----------------
    ticket_id       : UUID v4, clave primaria.
    source_event_id : Referencia al RawEvent que originó el ticket.
    space_id        : Espacio de Google Chat donde se originó.
    system          : Sistema afectado (enum SystemTag).
    severity        : Prioridad P0–P3.
    status          : Estado en el ciclo de vida.
    level           : Nivel de soporte actual (L1 / L2).
    summary         : Resumen generado por el clasificador.
    escalated       : True si fue escalado a L2 en algún momento.
    resolved_by_auto: True si fue resuelto sin intervención humana (runbook).
    resolved_by     : ID de usuario/sistema que cerró el ticket (opcional).

    Campos de tiempo (todos UTC)
    ----------------------------
    created_at      : Momento de creación del ticket.
    updated_at      : Última modificación.
    sla_deadline    : Deadline calculado según severidad.
    resolved_at     : Momento de resolución (None si aún abierto).
    """

    source_event_id: str
    space_id: str
    system: SystemTag
    severity: Severity
    summary: str
    source: EventSource = EventSource.HUMAN
    ticket_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: TicketStatus = TicketStatus.OPEN

    level: SupportLevel = SupportLevel.L1
    escalated: bool = False
    resolved_by_auto: bool = False
    resolved_by: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sla_deadline: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    def touch(self) -> None:
        """Actualiza updated_at al momento actual. Llamar en cada transición de estado."""
        self.updated_at = datetime.now(UTC)

    def is_overdue(self, now: Optional[datetime] = None) -> bool:
        """
        Retorna True si el ticket superó su SLA deadline y no está resuelto.

        Recibe `now` como parámetro para facilitar pruebas determinísticas
        (en vez de capturar datetime.utcnow() internamente).
        """
        if self.sla_deadline is None:
            return False
        if self.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            return False
        check_time = now or datetime.utcnow()
        return check_time > self.sla_deadline
