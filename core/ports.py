"""
core/ports.py
=============
Puertos (interfaces) del sistema — patrón hexagonal / puertos-y-adaptadores.

Cada interfaz define el CONTRATO que la lógica de negocio espera.
Los adaptadores concretos viven en adapters/ e implementan estos contratos.

Por qué `Protocol` en vez de ABC:
  - Protocol permite duck typing estructural: no hace falta heredar explícitamente.
  - Los adaptadores pueden implementar la interfaz sin conocer este módulo (útil para mocks).
  - Compatible con mypy --strict para verificación estática de tipos.

Mapa de adaptadores (local → GCP):
  EventQueue       : InMemoryEventQueue      → Cloud Pub/Sub
  TicketRepository : SQLiteTicketRepository  → Cloud SQL (Postgres) o Firestore
  LLMClient        : MockLLMClient           → Vertex AI / Anthropic / OpenAI API
  ChatNotifier     : LocalChatNotifier       → Google Chat REST API
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from core.models import RawEvent, Severity, SystemTag, Ticket


# ---------------------------------------------------------------------------
# Puerto 1: EventQueue
# ---------------------------------------------------------------------------


@runtime_checkable
class EventQueue(Protocol):
    """
    Cola de eventos que desacopla la ingesta del procesamiento.

    En producción (GCP): Cloud Pub/Sub.
    Local: implementación en memoria (adapters/queue.py).

    El uso de una cola garantiza que un spike de mensajes en Google Chat
    no bloquee el clasificador — el principio de backpressure.
    """

    def publish(self, event: RawEvent) -> None:
        """Publica un evento en la cola. Fire-and-forget desde el punto de vista del productor."""
        ...

    def consume(self) -> Optional[RawEvent]:
        """
        Consume el siguiente evento de la cola.

        Retorna None si la cola está vacía.
        El consumidor es responsable de hacer ack/nack (en Pub/Sub).
        """
        ...

    def size(self) -> int:
        """Número de eventos pendientes de procesar. Útil para métricas."""
        ...


# ---------------------------------------------------------------------------
# Puerto 2: TicketRepository
# ---------------------------------------------------------------------------


@runtime_checkable
class TicketRepository(Protocol):
    """
    Repositorio de tickets — abstracción de la capa de persistencia.

    En producción (GCP): Cloud SQL (Postgres) vía SQLAlchemy o Firestore.
    Local: SQLite (adapters/sqlite_repo.py).

    Solo expone operaciones de dominio, no SQL crudo. Esto garantiza que
    el motor de tickets no acople su lógica a detalles de almacenamiento.
    """

    def save(self, ticket: Ticket) -> None:
        """Persiste o actualiza un ticket (upsert)."""
        ...

    def get_by_id(self, ticket_id: str) -> Optional[Ticket]:
        """Recupera un ticket por su ID. Retorna None si no existe."""
        ...

    def find_open_by_system(self, system: SystemTag) -> list[Ticket]:
        """
        Retorna tickets abiertos (no resueltos ni cerrados) del sistema dado.
        Usado por el clasificador para de-duplicación.
        """
        ...

    def list_open(self) -> list[Ticket]:
        """
        Retorna todos los tickets que no están en estado RESOLVED ni CLOSED.
        Usado por el job de SLA para detectar estancamiento.
        """
        ...

    def list_all(self) -> list[Ticket]:
        """
        Retorna todos los tickets (cualquier estado).
        Usado por el dashboard de métricas.
        """
        ...


# ---------------------------------------------------------------------------
# Puerto 3: LLMClient
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    """
    Cliente de modelo de lenguaje para clasificación de texto libre.

    En producción: Vertex AI (Gemini) / Anthropic / OpenAI.
    Local: MockLLMClient (adapters/llm_mock.py) con respuestas fijas.

    Decisión de diseño: el clasificador llama a classify_text() y recibe
    (severity, system, summary). El prompt engineering vive en el adaptador,
    no en la lógica de negocio — así se puede cambiar el modelo sin tocar el core.
    """

    def classify_text(
        self,
        text: str,
    ) -> tuple[Severity, SystemTag, str]:
        """
        Clasifica texto libre y retorna (severidad, sistema, resumen).

        Parámetros
        ----------
        text : Mensaje del analista a clasificar.

        Retorna
        -------
        severity : Prioridad estimada del problema.
        system   : Sistema afectado identificado.
        summary  : Resumen de 1-2 oraciones generado por el LLM.

        Contrato: NUNCA lanza excepción por fallo del LLM. En caso de error
        debe retornar (Severity.P3, SystemTag.UNKNOWN, "Clasificación no disponible").
        Esto evita que un fallo del LLM tumbe el flujo de ingesta.
        """
        ...


# ---------------------------------------------------------------------------
# Puerto 4: ChatNotifier
# ---------------------------------------------------------------------------


@runtime_checkable
class ChatNotifier(Protocol):
    """
    Notificador hacia Google Chat.

    En producción: Google Chat REST API (webhook o bot OAuth).
    Local: LocalChatNotifier (adapters/chat_notifier.py) que solo genera
    el texto/payload sin hacer llamadas HTTP.

    Mantener esta interfaz permite testear la lógica de notificaciones
    sin necesidad de credenciales ni conexión de red.
    """

    def send_ack(self, event: RawEvent, ticket: Ticket) -> None:
        """
        Envía confirmación de recepción al espacio de chat original.
        Payload típico: "✅ Ticket #<id> creado — severidad <P> — asignado a L1."
        """
        ...

    def send_status_update(self, ticket: Ticket, message: str) -> None:
        """
        Notifica un cambio de estado del ticket al espacio original.
        Ejemplo: escalamiento a L2, SLA en riesgo, resolución automática.
        """
        ...

    def send_resolution(self, ticket: Ticket) -> None:
        """
        Notifica el cierre del ticket con un resumen de la resolución.
        Incluye tiempo total de resolución y si fue automático o manual.
        """
        ...
