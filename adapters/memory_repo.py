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

from core.models import SystemTag, Ticket, TicketComment, TicketStatus


class InMemoryTicketRepository:
    """
    Repositorio de tickets usando un dict en memoria keyed por ticket_id.
    """

    def __init__(self) -> None:
        self._store: dict[str, Ticket] = {}
        self._comments: dict[str, list[TicketComment]] = {}

    def save(self, ticket: Ticket) -> None:
        """Upsert — crea o sobreescribe el ticket con el mismo ticket_id."""
        self._store[ticket.ticket_id] = ticket

    def add_comment(self, comment: TicketComment) -> None:
        """Agrega un comentario al historial del ticket en memoria."""
        if comment.ticket_id not in self._comments:
            self._comments[comment.ticket_id] = []
        self._comments[comment.ticket_id].append(comment)
        if comment.ticket_id in self._store:
            ticket = self._store[comment.ticket_id]
            if not any(c.comment_id == comment.comment_id for c in ticket.comments):
                ticket.comments.append(comment)

    def get_comments(self, ticket_id: str) -> list[TicketComment]:
        """Obtiene la lista de comentarios de un ticket."""
        return list(self._comments.get(ticket_id, []))

    def get_by_id(self, ticket_id: str) -> Optional[Ticket]:
        ticket = self._store.get(ticket_id)
        if ticket:
            ticket.comments = self.get_comments(ticket_id)
        return ticket

    def find_open_by_system(self, system: SystemTag) -> list[Ticket]:
        """Retorna tickets no resueltos/cerrados del sistema dado."""
        closed = {TicketStatus.RESOLVED, TicketStatus.CLOSED}
        tickets = [
            t for t in self._store.values()
            if t.system == system and t.status not in closed
        ]
        for t in tickets:
            t.comments = self.get_comments(t.ticket_id)
        return tickets

    def list_open(self) -> list[Ticket]:
        """Retorna todos los tickets no resueltos/cerrados."""
        closed = {TicketStatus.RESOLVED, TicketStatus.CLOSED}
        tickets = [t for t in self._store.values() if t.status not in closed]
        for t in tickets:
            t.comments = self.get_comments(t.ticket_id)
        return tickets

    def list_all(self) -> list[Ticket]:
        tickets = list(self._store.values())
        for t in tickets:
            t.comments = self.get_comments(t.ticket_id)
        return tickets
