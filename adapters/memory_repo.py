"""
adapters/memory_repo.py
=======================
InMemoryTicketRepository — implementación del puerto TicketRepository en memoria.

USO PRINCIPAL: tests unitarios y demostración del sistema sin base de datos.

En producción esto se reemplaza por SQLiteTicketRepository (Paso 4) y
eventualmente por CloudSQLTicketRepository en GCP — sin cambiar el core.

Limitaciones documentadas (aceptables para tests):
  - Sin persistencia entre reinicios.
  - Sin concurrencia segura (sin locks).
  - Búsquedas O(n) — suficiente para el volumen de una prueba técnica.
"""

from __future__ import annotations

from typing import Optional

from core.models import SystemTag, Ticket, TicketStatus


class InMemoryTicketRepository:
    """
    Repositorio de tickets usando un dict en memoria keyed por ticket_id.
    """

    def __init__(self) -> None:
        self._store: dict[str, Ticket] = {}

    def save(self, ticket: Ticket) -> None:
        """Upsert — crea o sobreescribe el ticket con el mismo ticket_id."""
        self._store[ticket.ticket_id] = ticket

    def get_by_id(self, ticket_id: str) -> Optional[Ticket]:
        return self._store.get(ticket_id)

    def find_open_by_system(self, system: SystemTag) -> list[Ticket]:
        """Retorna tickets no resueltos/cerrados del sistema dado."""
        closed = {TicketStatus.RESOLVED, TicketStatus.CLOSED}
        return [
            t for t in self._store.values()
            if t.system == system and t.status not in closed
        ]

    def list_open(self) -> list[Ticket]:
        """Retorna todos los tickets no resueltos/cerrados."""
        closed = {TicketStatus.RESOLVED, TicketStatus.CLOSED}
        return [t for t in self._store.values() if t.status not in closed]

    def list_all(self) -> list[Ticket]:
        return list(self._store.values())
