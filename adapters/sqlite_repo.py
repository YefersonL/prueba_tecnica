"""
adapters/sqlite_repo.py
=======================
SQLiteTicketRepository — implementación del puerto TicketRepository sobre SQLite.

Por qué SQLite:
  - Stdlib de Python (sin dependencias extra).
  - Reproduce el contrato SQL que en producción sería Postgres/Cloud SQL.
  - Archivo único → fácil de inspeccionar, versionar y limpiar entre pruebas.
  - El adaptador es el único cambio necesario para ir a Postgres: la interfaz
    que consume el core (TicketRepository) es idéntica.

Estrategia de serialización:
  Usamos una tabla plana con columnas tipadas explícitamente.
  Los enums se guardan como TEXT (su .value) para que sean legibles sin ORM.
  Los datetimes se guardan como TEXT ISO 8601 con timezone (+00:00) — esto
  evita bugs de zona horaria al leer de vuelta.
  Los booleans como INTEGER (0/1) — convención SQLite.

Decisión consciente: no usamos SQLAlchemy para no agregar dependencias.
  En producción con Cloud SQL, SQLAlchemy + asyncpg sería la elección correcta.
  Aquí el adaptador SQLite demuestra el patrón sin la complejidad del ORM.

Migración a GCP:
  Crear adapters/cloud_sql_repo.py implementando el mismo TicketRepository Protocol.
  La lógica SQL es casi idéntica (ajustes de tipos: BOOLEAN nativo, TIMESTAMPTZ, etc.).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Generator, Optional

from core.models import (
    Severity,
    SupportLevel,
    SystemTag,
    Ticket,
    TicketStatus,
)

# ---------------------------------------------------------------------------
# DDL — esquema de la tabla tickets
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id        TEXT PRIMARY KEY,
    source_event_id  TEXT NOT NULL,
    space_id         TEXT NOT NULL,
    system           TEXT NOT NULL,
    severity         TEXT NOT NULL,
    status           TEXT NOT NULL,
    level            TEXT NOT NULL,
    summary          TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'human',
    escalated        INTEGER NOT NULL DEFAULT 0,
    resolved_by_auto INTEGER NOT NULL DEFAULT 0,
    resolved_by      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    sla_deadline     TEXT,
    resolved_at      TEXT
);
"""

# Índices para las consultas más frecuentes
_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_tickets_system_status ON tickets(system, status);",
    "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);",
]


# ---------------------------------------------------------------------------
# Helpers de serialización / deserialización
# ---------------------------------------------------------------------------


def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    """Convierte datetime UTC a string ISO 8601. Retorna None si dt es None."""
    if dt is None:
        return None
    # Asegurar que siempre sea UTC-aware antes de serializar
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _str_to_dt(s: Optional[str]) -> Optional[datetime]:
    """Parsea string ISO 8601 a datetime UTC-aware. Retorna None si s es None."""
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _ticket_to_row(ticket: Ticket) -> dict:
    """Convierte un Ticket a un dict listo para insertar en SQLite."""
    return {
        "ticket_id": ticket.ticket_id,
        "source_event_id": ticket.source_event_id,
        "space_id": ticket.space_id,
        "system": ticket.system.value,
        "severity": ticket.severity.value,
        "status": ticket.status.value,
        "level": ticket.level.value,
        "summary": ticket.summary,
        "source": ticket.source.value if hasattr(ticket, "source") and ticket.source else "human",
        "escalated": int(ticket.escalated),
        "resolved_by_auto": int(ticket.resolved_by_auto),
        "resolved_by": ticket.resolved_by,
        "created_at": _dt_to_str(ticket.created_at),
        "updated_at": _dt_to_str(ticket.updated_at),
        "sla_deadline": _dt_to_str(ticket.sla_deadline),
        "resolved_at": _dt_to_str(ticket.resolved_at),
    }


def _row_to_ticket(row: sqlite3.Row) -> Ticket:
    """Reconstruye un Ticket desde una fila de SQLite."""
    from core.models import EventSource
    source_val = row["source"] if "source" in row.keys() else "human"
    try:
        source_enum = EventSource(source_val)
    except Exception:
        source_enum = EventSource.HUMAN

    return Ticket(
        ticket_id=row["ticket_id"],
        source_event_id=row["source_event_id"],
        space_id=row["space_id"],
        system=SystemTag(row["system"]),
        severity=Severity(row["severity"]),
        status=TicketStatus(row["status"]),
        level=SupportLevel(row["level"]),
        summary=row["summary"],
        source=source_enum,
        escalated=bool(row["escalated"]),
        resolved_by_auto=bool(row["resolved_by_auto"]),
        resolved_by=row["resolved_by"],
        created_at=_str_to_dt(row["created_at"]),
        updated_at=_str_to_dt(row["updated_at"]),
        sla_deadline=_str_to_dt(row["sla_deadline"]),
        resolved_at=_str_to_dt(row["resolved_at"]),
    )



# ---------------------------------------------------------------------------
# Repositorio
# ---------------------------------------------------------------------------


class SQLiteTicketRepository:
    """
    Implementación del puerto TicketRepository sobre SQLite.

    Parámetros
    ----------
    db_path : Ruta al archivo .db. Usa ":memory:" para tests en RAM.
              Default: "support_system.db" en el directorio de trabajo.

    Nota sobre ":memory:": SQLite in-memory no comparte estado entre conexiones.
    Para ":memory:" mantenemos una conexión persistente única para que la tabla
    creada en _init_db() sea visible en todas las operaciones subsecuentes.
    Para archivos en disco, cada operación abre/cierra su propia conexión
    (más robusto frente a errores y compatible con acceso multi-proceso).
    """

    def __init__(self, db_path: str | Path = "support_system.db") -> None:
        self._db_path = str(db_path)
        self._is_memory = self._db_path == ":memory:"
        # Para :memory:, conexión única y persistente
        self._memory_conn: sqlite3.Connection | None = None
        if self._is_memory:
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        """Crea la tabla e índices si no existen. Idempotente."""
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE_SQL)
            for idx_sql in _CREATE_INDEXES_SQL:
                conn.execute(idx_sql)
            # Migración segura si la tabla ya existía sin la columna 'source'
            try:
                conn.execute("ALTER TABLE tickets ADD COLUMN source TEXT NOT NULL DEFAULT 'human';")
            except sqlite3.OperationalError:
                pass

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager que provee la conexión SQLite.

        Para ":memory:": reutiliza la conexión persistente (sin commit/close).
        Para archivo en disco: abre, commitea y cierra en cada operación.
        """
        if self._is_memory:
            # Conexión persistente para :memory: — no cerramos entre operaciones
            try:
                yield self._memory_conn
                self._memory_conn.commit()
            except Exception:
                self._memory_conn.rollback()
                raise
        else:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def save(self, ticket: Ticket) -> None:
        """
        Upsert de un ticket.

        Usa INSERT OR REPLACE para idempotencia — si el ticket ya existe
        lo sobreescribe completamente. Esto es correcto para nuestro modelo
        donde el ticket_id es inmutable y el resto puede mutar.
        """
        row = _ticket_to_row(ticket)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tickets (
                    ticket_id, source_event_id, space_id, system, severity,
                    status, level, summary, source, escalated, resolved_by_auto,
                    resolved_by, created_at, updated_at, sla_deadline, resolved_at
                ) VALUES (
                    :ticket_id, :source_event_id, :space_id, :system, :severity,
                    :status, :level, :summary, :source, :escalated, :resolved_by_auto,
                    :resolved_by, :created_at, :updated_at, :sla_deadline, :resolved_at
                )
                """,
                row,
            )

    def get_by_id(self, ticket_id: str) -> Optional[Ticket]:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            )
            row = cursor.fetchone()
        return _row_to_ticket(row) if row else None

    def find_open_by_system(self, system: SystemTag) -> list[Ticket]:
        closed = (TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value)
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM tickets WHERE system = ? AND status NOT IN (?, ?)",
                (system.value, *closed),
            )
            rows = cursor.fetchall()
        return [_row_to_ticket(r) for r in rows]

    def list_open(self) -> list[Ticket]:
        closed = (TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value)
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM tickets WHERE status NOT IN (?, ?)", closed
            )
            rows = cursor.fetchall()
        return [_row_to_ticket(r) for r in rows]

    def list_all(self) -> list[Ticket]:
        with self._connect() as conn:
            cursor = conn.execute("SELECT * FROM tickets ORDER BY created_at DESC")
            rows = cursor.fetchall()
        return [_row_to_ticket(r) for r in rows]
