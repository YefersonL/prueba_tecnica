"""
adapters/queue.py
=================
Implementación concreta del puerto EventQueue usando una lista en memoria.

Cuándo usar este adaptador:
  - Desarrollo local y tests (sin infraestructura externa).
  - Prueba técnica: simula Cloud Pub/Sub con la misma interfaz.

Para migrar a Cloud Pub/Sub en GCP:
  Crear adapters/pubsub_queue.py con la misma interfaz. El core no cambia.

Limitaciones conocidas (aceptables para prueba técnica):
  - No es thread-safe. En producción, Pub/Sub maneja concurrencia por diseño.
  - No persiste entre reinicios. En producción, Pub/Sub garantiza durabilidad.
  - No tiene ack/nack. En producción, mensajes sin ack se reencolan (at-least-once).
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from core.models import RawEvent


class InMemoryEventQueue:
    """
    Cola FIFO en memoria que implementa el puerto EventQueue.

    Usa collections.deque para garantizar O(1) en append y popleft —
    más eficiente que list para uso como cola.
    """

    def __init__(self) -> None:
        self._queue: deque[RawEvent] = deque()

    def publish(self, event: RawEvent) -> None:
        """
        Encola el evento al final de la cola.
        Equivalente a `publisher.publish(topic, data)` en Cloud Pub/Sub.
        """
        self._queue.append(event)

    def consume(self) -> Optional[RawEvent]:
        """
        Extrae y retorna el evento más antiguo de la cola (FIFO).
        Retorna None si la cola está vacía.

        En Cloud Pub/Sub esto sería `subscriber.pull()` seguido de `acknowledge()`.
        Aquí el "ack" es implícito al hacer popleft — diseño simplificado consciente.
        """
        if not self._queue:
            return None
        return self._queue.popleft()

    def size(self) -> int:
        """Número de eventos pendientes. Útil para health checks y métricas."""
        return len(self._queue)

    def consume_all(self) -> list[RawEvent]:
        """
        Drena la cola completa y retorna todos los eventos.
        Útil para el job de SLA que procesa en batch, y para tests.

        No es parte del puerto EventQueue pero es conveniente como método
        de utilidad del adaptador concreto.
        """
        events = list(self._queue)
        self._queue.clear()
        return events
