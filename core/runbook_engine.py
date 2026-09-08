"""
core/runbook_engine.py
======================
Motor de runbooks — automatización de incidencias recurrentes conocidas.

Un "runbook" en este sistema es una función que:
  1. Recibe un Ticket recién creado.
  2. Decide si puede resolverlo automáticamente.
  3. Si puede: ejecuta la acción de remediación, marca el ticket como resuelto
     con resolved_by_auto=True, notifica al espacio de chat.
  4. Si no puede: retorna None (el ticket sigue en flujo normal L1/L2).

Diseño del registro:
  Los runbooks se registran en un dict keyed por SystemTag.
  Cada entrada es una lista de RunbookHandler — se prueban en orden hasta
  que uno declara que puede manejar el ticket.
  Esto permite múltiples runbooks para el mismo sistema (ej. pipeline caído
  vs pipeline con latencia alta son acciones distintas).

Por qué esto es valioso para la defensa:
  - Demuestra que el sistema aprende de lo recurrente.
  - El % de resolved_by_auto es una métrica directa del ROI del sistema.
  - El patrón es extensible: agregar un runbook es registrar una función,
    no modificar lógica existente (Open/Closed Principle).

Runbooks incluidos (ejemplo mínimo pero completo):
  1. ingestion_pipeline_restart: reinicia el pipeline de ingesta cuando falla.
     En producción invocaría una Cloud Run Job o un endpoint de control.
     Aquí simula la acción con un log + delay mínimo.

  2. payments_latency_clear: para alertas de latencia en pagos (P2),
     ejecuta un "drain de cola" simulado y verifica que la latencia baja.
     Demuestra que los runbooks pueden tener lógica condicional.

Para agregar un runbook nuevo en v2:
  1. Definir una función (ticket: Ticket) -> bool | None
  2. Registrarla en RUNBOOK_REGISTRY con su SystemTag
  Eso es todo — el motor lo descubre automáticamente.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from core.models import Severity, SystemTag, Ticket
from core.ports import ChatNotifier
from core.ticket_engine import TicketEngine

logger = logging.getLogger(__name__)

# Tipo de un handler de runbook:
# Recibe el ticket, retorna True si lo resolvió, False si no pudo.
RunbookHandler = Callable[[Ticket], bool]


# ---------------------------------------------------------------------------
# Runbooks concretos
# ---------------------------------------------------------------------------


def _runbook_ingestion_pipeline_restart(ticket: Ticket) -> bool:
    """
    Runbook: Reinicio del pipeline de ingesta.

    Aplica a: SystemTag.INGESTION con severidad P1 o P2.
    Simula: detectar que el worker está caído y reiniciarlo.

    En producción invocaría:
      - Cloud Run API: run.jobs.run("restart-ingestion-pipeline")
      - O un endpoint interno de control del orquestador (Airflow/Prefect).

    Retorna True si la acción fue exitosa (siempre aquí, por ser simulación).
    En producción retornaría False si el restart falló (ticket escalaría a L1).
    """
    if ticket.severity not in (Severity.P1, Severity.P2):
        # Severidades extremas (P0) requieren intervención humana
        return False

    # Simulación de la acción de remediación
    logger.info(
        "[Runbook:ingestion_pipeline_restart] Ejecutando restart del pipeline "
        "para ticket %s (sistema=%s, severidad=%s)",
        ticket.ticket_id[:8],
        ticket.system.value,
        ticket.severity.value,
    )
    # En producción aquí iría la llamada HTTP al orchestrator
    # response = httpx.post(f"{ORCHESTRATOR_URL}/pipelines/ingestion/restart")
    # return response.status_code == 200

    logger.info(
        "[Runbook:ingestion_pipeline_restart] Pipeline reiniciado exitosamente. "
        "Ticket %s marcado para resolución automática.",
        ticket.ticket_id[:8],
    )
    return True


def _runbook_payments_latency_clear(ticket: Ticket) -> bool:
    """
    Runbook: Limpieza de cola de pagos por latencia elevada.

    Aplica a: SystemTag.PAYMENTS con severidad P2 (latencia, no fallo total).
    P1 en pagos requiere revisión humana (puede ser fraude o pérdida de dinero).

    Simula: trigger de un job de "drain" de la cola de pagos y verificación
    de que la latencia volvió a niveles normales.
    """
    if ticket.severity != Severity.P2:
        return False

    logger.info(
        "[Runbook:payments_latency_clear] Ejecutando drain de cola de pagos "
        "para ticket %s",
        ticket.ticket_id[:8],
    )
    # En producción:
    # 1. POST a /payments/queue/drain
    # 2. Esperar 30s y verificar que p99 < umbral
    # 3. Retornar False si el p99 sigue alto (escalar a L1)

    logger.info(
        "[Runbook:payments_latency_clear] Cola drenada. Latencia normalizada."
    )
    return True


# ---------------------------------------------------------------------------
# Registro de runbooks
# ---------------------------------------------------------------------------

RUNBOOK_REGISTRY: dict[SystemTag, list[RunbookHandler]] = {
    SystemTag.INGESTION: [
        _runbook_ingestion_pipeline_restart,
    ],
    SystemTag.PAYMENTS: [
        _runbook_payments_latency_clear,
    ],
    # Para agregar un runbook nuevo:
    # SystemTag.FRAUD: [_runbook_fraud_alert_acknowledge],
    # SystemTag.AUTH: [_runbook_auth_cache_flush],
}

# Nombre legible de cada handler (para logs y auditoría)
RUNBOOK_NAMES: dict[RunbookHandler, str] = {
    _runbook_ingestion_pipeline_restart: "runbook:ingestion_pipeline_restart",
    _runbook_payments_latency_clear: "runbook:payments_latency_clear",
}


# ---------------------------------------------------------------------------
# Resultado del motor de runbooks
# ---------------------------------------------------------------------------


@dataclass
class RunbookResult:
    """
    Resultado de intentar aplicar un runbook a un ticket.

    Campos
    ------
    ticket          : El ticket procesado.
    resolved        : True si algún runbook lo resolvió automáticamente.
    runbook_name    : Nombre del runbook que resolvió (None si no hubo match).
    no_runbook_found: True si no había runbook registrado para este sistema.
    """

    ticket: Ticket
    resolved: bool
    runbook_name: Optional[str] = None
    no_runbook_found: bool = False


# ---------------------------------------------------------------------------
# Motor de runbooks
# ---------------------------------------------------------------------------


class RunbookEngine:
    """
    Motor que evalúa si un ticket puede resolverse con un runbook registrado.

    Dependencias inyectadas:
      ticket_engine : Para marcar el ticket como resuelto.
      notifier      : Para notificar la resolución automática.
      registry      : Registro de runbooks (inyectable para tests).
    """

    def __init__(
        self,
        ticket_engine: TicketEngine,
        notifier: ChatNotifier,
        registry: dict[SystemTag, list[RunbookHandler]] | None = None,
    ) -> None:
        self._engine = ticket_engine
        self._notifier = notifier
        self._registry = registry if registry is not None else RUNBOOK_REGISTRY

    def try_auto_resolve(self, ticket: Ticket) -> RunbookResult:
        """
        Intenta resolver el ticket automáticamente usando un runbook registrado.

        Flujo:
          1. Buscar runbooks registrados para el sistema del ticket.
          2. Si no hay → retornar no_runbook_found=True (flujo normal L1).
          3. Probar cada handler en orden hasta que uno retorne True.
          4. Si un handler resuelve: marcar ticket, notificar, retornar resolved=True.
          5. Si ninguno resuelve: retornar resolved=False (flujo normal L1).

        Parámetros
        ----------
        ticket : Ticket recién creado (estado OPEN, nivel L1 o L2).

        Retorna
        -------
        RunbookResult con el resultado de la evaluación.
        """
        handlers = self._registry.get(ticket.system, [])

        if not handlers:
            logger.debug(
                "[RunbookEngine] Sin runbook para sistema '%s'. Flujo normal L1.",
                ticket.system.value,
            )
            return RunbookResult(
                ticket=ticket,
                resolved=False,
                no_runbook_found=True,
            )

        for handler in handlers:
            handler_name = RUNBOOK_NAMES.get(handler, handler.__name__)
            logger.info(
                "[RunbookEngine] Probando runbook '%s' para ticket %s",
                handler_name,
                ticket.ticket_id[:8],
            )

            try:
                success = handler(ticket)
            except Exception as exc:
                # Un fallo en el runbook no debe bloquear el flujo del ticket
                logger.error(
                    "[RunbookEngine] Error en runbook '%s': %s — ticket %s pasa a L1",
                    handler_name,
                    exc,
                    ticket.ticket_id[:8],
                )
                success = False

            if success:
                resolved_ticket = self._engine.resolve(
                    ticket,
                    resolved_by=handler_name,
                    auto=True,
                )
                self._notifier.send_resolution(resolved_ticket)
                logger.info(
                    "[RunbookEngine] Ticket %s resuelto automáticamente por '%s'",
                    ticket.ticket_id[:8],
                    handler_name,
                )
                return RunbookResult(
                    ticket=resolved_ticket,
                    resolved=True,
                    runbook_name=handler_name,
                )

        # Ningún runbook pudo resolver
        logger.info(
            "[RunbookEngine] Ningún runbook pudo resolver ticket %s — flujo normal.",
            ticket.ticket_id[:8],
        )
        return RunbookResult(ticket=ticket, resolved=False)
