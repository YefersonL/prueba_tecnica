"""
adapters/chat_notifier.py
=========================
LocalChatNotifier — implementación del puerto ChatNotifier.

En producción (GCP): Google Chat REST API (webhook entrante o bot OAuth2).
Aquí: genera el texto/payload correcto pero NO hace llamadas HTTP.

Por qué esto es válido para una prueba técnica:
  1. La lógica de qué decir y cuándo decirlo vive en el CORE, no aquí.
  2. El formato del mensaje es verificable en tests sin red.
  3. En GCP solo se cambia el método de envío (POST al webhook), no el contenido.

En producción el adaptador real haría:
  import httpx
  httpx.post(webhook_url, json={"text": payload["text"]})

Estructura del payload generado (compatible con Google Chat webhook):
  {
    "text": "...",            # Mensaje visible en el chat
    "thread": {"name": "..."},  # Para responder en hilo (si aplica)
  }

Los mensajes se acumulan en self.sent_messages para que los tests puedan
inspeccionar qué se "envió" sin hacer mocks adicionales.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.models import RawEvent, Ticket, TicketStatus


@dataclass
class SentMessage:
    """Registro de un mensaje que se habría enviado a Google Chat."""

    space_id: str
    text: str
    payload: dict[str, Any]
    sent_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    message_type: str = "generic"  # "ack" | "status_update" | "resolution"


class LocalChatNotifier:
    """
    ChatNotifier que genera payloads de Google Chat sin enviarlos.

    Útil para:
      - Tests: inspeccionas self.sent_messages para verificar el contenido.
      - Desarrollo local: logs en consola en vez de red.
      - Defensa de la prueba: demuestra el contrato del notificador.

    En GCP este adaptador se reemplaza por GoogleChatAPINotifier que hace
    el POST real — misma interfaz, distinta implementación.
    """

    def __init__(self, log_to_console: bool = False) -> None:
        """
        Parámetros
        ----------
        log_to_console : Si True, imprime los mensajes en stdout.
                         Útil para demos interactivas, desactivado en tests.
        """
        self._log = log_to_console
        self.sent_messages: list[SentMessage] = []

    def send_ack(self, event: RawEvent, ticket: Ticket) -> None:
        """
        Confirmación de recepción al espacio donde llegó el mensaje original.

        Formato: "✅ Ticket #<8chars> creado — <P0> — Sistema: <system> — Asignado a <L1|L2>."
        """
        text = (
            f"✅ *Ticket #{ticket.ticket_id[:8]} creado*\n"
            f"• Severidad: *{ticket.severity.value}*\n"
            f"• Sistema: {ticket.system.value}\n"
            f"• Nivel asignado: {ticket.level.value}\n"
            f"• SLA deadline: {ticket.sla_deadline.strftime('%Y-%m-%d %H:%M UTC') if ticket.sla_deadline else 'N/A'}\n"
            f"• Resumen: _{ticket.summary[:120]}_"
        )
        payload = {
            "text": text,
            "thread": {"name": f"{event.space_id}/threads/{ticket.ticket_id}"},
        }
        self._record(
            space_id=event.space_id,
            text=text,
            payload=payload,
            message_type="ack",
        )

    def send_status_update(self, ticket: Ticket, message: str) -> None:
        """
        Notifica cambio de estado del ticket al espacio original.

        Incluye siempre el ticket_id para que el equipo pueda correlacionar.
        """
        text = (
            f"🔄 *Actualización Ticket #{ticket.ticket_id[:8]}*\n"
            f"{message}\n"
            f"• Estado actual: {ticket.status.value}\n"
            f"• Nivel: {ticket.level.value}"
        )
        payload = {
            "text": text,
            "thread": {"name": f"{ticket.space_id}/threads/{ticket.ticket_id}"},
        }
        self._record(
            space_id=ticket.space_id,
            text=text,
            payload=payload,
            message_type="status_update",
        )

    def send_resolution(self, ticket: Ticket) -> None:
        """
        Notifica cierre del ticket con resumen de resolución.

        Incluye: tiempo total, resuelto por quién/qué, y si fue automático.
        """
        duration_str = "N/A"
        if ticket.resolved_at and ticket.created_at:
            delta = ticket.resolved_at - ticket.created_at
            total_minutes = int(delta.total_seconds() / 60)
            if total_minutes < 60:
                duration_str = f"{total_minutes} min"
            else:
                hours = total_minutes // 60
                mins = total_minutes % 60
                duration_str = f"{hours}h {mins}m"

        auto_tag = " 🤖 *(resuelto automáticamente)*" if ticket.resolved_by_auto else ""
        resolver = ticket.resolved_by or "desconocido"

        text = (
            f"✅ *Ticket #{ticket.ticket_id[:8]} RESUELTO*{auto_tag}\n"
            f"• Duración total: {duration_str}\n"
            f"• Resuelto por: {resolver}\n"
            f"• Resumen: _{ticket.summary[:120]}_"
        )
        payload = {
            "text": text,
            "thread": {"name": f"{ticket.space_id}/threads/{ticket.ticket_id}"},
        }
        self._record(
            space_id=ticket.space_id,
            text=text,
            payload=payload,
            message_type="resolution",
        )

    def _record(
        self,
        space_id: str,
        text: str,
        payload: dict,
        message_type: str,
    ) -> None:
        """Registra el mensaje en sent_messages y opcionalmente lo imprime."""
        msg = SentMessage(
            space_id=space_id,
            text=text,
            payload=payload,
            message_type=message_type,
        )
        self.sent_messages.append(msg)
        if self._log:
            print(f"\n[ChatNotifier → {space_id}]\n{text}\n")

    def clear(self) -> None:
        """Limpia el historial de mensajes. Útil entre tests."""
        self.sent_messages.clear()
