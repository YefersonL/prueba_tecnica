"""
core/classifier.py
==================
Clasificador de eventos — corazón del sistema de triaje.

Dos caminos de clasificación:
  1. Reglas determinísticas (MACHINE events):
     Patrones regex sobre el texto de la alerta → severidad + sistema.
     Ventaja: sin latencia de API, 100% predecible, auditable.
     Ideal para el 80% de alertas automáticas que son recurrentes y conocidas.

  2. LLMClient (HUMAN events / texto libre):
     Delegamos al modelo de lenguaje la clasificación de texto ambiguo.
     La interfaz LLMClient (core/ports.py) es intercambiable:
       - Tests       → MockLLMClient (sin red, determinístico)
       - Dev/Prod    → GeminiLLMClient (Google Generative AI API)
       - GCP Prod    → VertexAILLMClient (misma API, distinta auth)

De-duplicación:
  Si ya existe un ticket OPEN o IN_PROGRESS del mismo sistema creado en los
  últimos N minutos (ventana configurable), el clasificador retorna el ticket
  existente en vez de crear uno nuevo. Esto evita spam de tickets para pipelines
  que fallan repetidamente mientras están siendo atendidos.

Nota de diseño: el clasificador NO persiste tickets. Solo clasifica y detecta
duplicados consultando el repositorio. La creación de tickets es responsabilidad
del motor de tickets (core/ticket_engine.py — Paso 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Optional

from core.models import EventSource, RawEvent, Severity, SystemTag, Ticket
from core.ports import LLMClient, TicketRepository

# ---------------------------------------------------------------------------
# Configuración de ventana de de-duplicación
# ---------------------------------------------------------------------------

DEFAULT_DEDUP_WINDOW_MINUTES: int = 30
"""
Ventana de de-duplicación en minutos.
Si hay un ticket abierto del mismo sistema creado en este período,
se considera duplicado y no se crea ticket nuevo.

Justificación del valor: 30 min es suficiente para que L1 haga triaje
sin que la ventana sea tan larga que oculte nuevas ocurrencias reales.
"""

# ---------------------------------------------------------------------------
# Reglas determinísticas para alertas de máquina
# ---------------------------------------------------------------------------

# Cada regla: (patrón regex, sistema detectado, severidad asignada)
# El orden importa: la primera regla que haga match gana.
# Se compilan una sola vez al importar el módulo (performance).
_MACHINE_RULES: list[tuple[re.Pattern, SystemTag, Severity]] = [
    # P0 — Producción completamente caída
    (
        re.compile(r"producción\s+ca[íi]da|sistema\s+ca[íi]do|down\s+completo", re.I),
        SystemTag.UNKNOWN,  # sistema específico se infiere por contexto
        Severity.P0,
    ),
    # Pipeline de transacciones
    (
        re.compile(r"pipeline.*(transacci[oó]n|transaction)", re.I),
        SystemTag.TRANSACTIONS,
        Severity.P1,
    ),
    # Ingesta de datos
    (
        re.compile(r"(ingesta|ingestion|ingest).*(fall[oó]|fail|error)", re.I),
        SystemTag.INGESTION,
        Severity.P1,
    ),
    # Pagos / latencia alta
    (
        re.compile(r"(pago|payment).*(latencia|latency|lento|slow|timeout)", re.I),
        SystemTag.PAYMENTS,
        Severity.P2,
    ),
    # Pagos / fallo general
    (
        re.compile(r"(pago|payment).*(fall[oó]|fail|error|ca[íi]do)", re.I),
        SystemTag.PAYMENTS,
        Severity.P1,
    ),
    # Fraude
    (
        re.compile(r"(fraude?|fraud).*(ca[íi]do|fail|error|bloqu)", re.I),
        SystemTag.FRAUD,
        Severity.P1,
    ),
    # Autenticación
    (
        re.compile(r"(auth|autenticaci[oó]n|login).*(fall[oó]|fail|error)", re.I),
        SystemTag.AUTH,
        Severity.P1,
    ),
    # Reportes
    (
        re.compile(r"(reporte?|report).*(fall[oó]|fail|error)", re.I),
        SystemTag.REPORTING,
        Severity.P2,
    ),
    # Catchall: cualquier fallo con emoji ❌ o ⚠️ en alertas de bot
    (
        re.compile(r"[❌⚠️]\s*(fall[oó]|fail|error|ca[íi]do)", re.I),
        SystemTag.UNKNOWN,
        Severity.P2,
    ),
]


def _classify_by_rules(text: str) -> Optional[tuple[Severity, SystemTag, str]]:
    """
    Intenta clasificar el texto con reglas determinísticas.

    Retorna (severity, system, summary) si alguna regla hace match.
    Retorna None si ninguna regla aplica.
    """
    for pattern, system, severity in _MACHINE_RULES:
        if pattern.search(text):
            # Resumen generado localmente — no requiere LLM
            summary = f"[Auto] {text[:120]}{'...' if len(text) > 120 else ''}"
            return severity, system, summary
    return None


# ---------------------------------------------------------------------------
# Resultado de clasificación
# ---------------------------------------------------------------------------


@dataclass
class ClassificationResult:
    """
    Resultado de clasificar un RawEvent.

    Campos
    ------
    severity   : Prioridad P0–P3 asignada.
    system     : Sistema afectado identificado.
    summary    : Resumen de 1-2 oraciones.
    is_duplicate : True si existe un ticket abierto reciente del mismo sistema.
    existing_ticket : Ticket existente si is_duplicate es True.
    classified_by : "rules" | "llm" — para métricas y auditoría.
    """

    severity: Severity
    system: SystemTag
    summary: str
    is_duplicate: bool = False
    existing_ticket: Optional[Ticket] = None
    classified_by: str = "rules"


# ---------------------------------------------------------------------------
# Clasificador principal
# ---------------------------------------------------------------------------


class EventClassifier:
    """
    Clasificador de eventos que combina reglas determinísticas y LLM.

    Dependencias inyectadas por constructor (patrón hexagonal):
      - llm_client: LLMClient — para clasificar texto libre humano.
      - ticket_repo: TicketRepository — para consultar duplicados.

    El clasificador no tiene estado propio: es stateless entre llamadas.
    Esto lo hace trivialmente testeable y seguro para uso concurrente.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        ticket_repo: TicketRepository,
        dedup_window_minutes: int = DEFAULT_DEDUP_WINDOW_MINUTES,
    ) -> None:
        self._llm = llm_client
        self._repo = ticket_repo
        self._dedup_window = timedelta(minutes=dedup_window_minutes)

    def classify(self, event: RawEvent) -> ClassificationResult:
        """
        Clasifica un RawEvent y detecta duplicados.

        Flujo:
          MACHINE → reglas determinísticas primero → LLM si no hay match
          HUMAN   → LLM directamente (texto libre no sigue patrones fijos)

        En ambos casos, después de clasificar se chequea de-duplicación.
        """
        if event.source == EventSource.MACHINE:
            result_tuple = _classify_by_rules(event.message)
            if result_tuple:
                severity, system, summary = result_tuple
                classified_by = "rules"
            else:
                # Alerta de máquina con formato no reconocido → LLM
                severity, system, summary = self._llm.classify_text(event.message)
                classified_by = "llm"
        else:
            # HUMAN: siempre LLM — el texto libre es semánticamente complejo
            severity, system, summary = self._llm.classify_text(event.message)
            classified_by = "llm"

        # De-duplicación: buscar ticket abierto reciente del mismo sistema
        existing = self._find_duplicate(system=system, now=event.received_at)

        return ClassificationResult(
            severity=severity,
            system=system,
            summary=summary,
            is_duplicate=existing is not None,
            existing_ticket=existing,
            classified_by=classified_by,
        )

    def _find_duplicate(
        self, system: SystemTag, now: datetime
    ) -> Optional[Ticket]:
        """
        Busca un ticket abierto del mismo sistema dentro de la ventana de de-dup.

        No buscamos duplicados para SystemTag.UNKNOWN — sería demasiado agresivo
        y agruparía incidencias no relacionadas.
        """
        if system == SystemTag.UNKNOWN:
            return None

        cutoff = now - self._dedup_window
        open_tickets = self._repo.find_open_by_system(system)

        for ticket in open_tickets:
            if ticket.created_at >= cutoff:
                return ticket

        return None
