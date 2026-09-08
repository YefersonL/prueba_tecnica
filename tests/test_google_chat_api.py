"""
tests/test_google_chat_api.py
==============================
Tests unitarios para el adaptador GoogleChatApiNotifier.
Verifica:
  - Inicialización con y sin archivo de credenciales.
  - Generación de payloads de mensaje compatibles con Google Chat REST API.
  - Llamadas HTTP POST con Authorization Bearer mockeadas.
  - Resiliencia ante errores de red o espacios no encontrados.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from adapters.google_chat_api import GoogleChatApiNotifier
from core.models import EventSource, RawEvent, Severity, SupportLevel, SystemTag, Ticket, TicketStatus


def _make_ticket(
    ticket_id: str = "TICK-001",
    status: TicketStatus = TicketStatus.OPEN,
    level: SupportLevel = SupportLevel.L1,
    severity: Severity = Severity.P2,
    space_id: str = "spaces/AAAA1111",
) -> Ticket:
    return Ticket(
        ticket_id=ticket_id,
        source_event_id="evt-001",
        summary="Problema en pasarela de pagos",
        severity=severity,
        system=SystemTag.PAYMENTS,
        status=status,
        level=level,
        space_id=space_id,
        created_at=datetime.now(UTC),
    )


def _make_event(space_id: str = "spaces/AAAA1111") -> RawEvent:
    return RawEvent(
        event_id="evt-001",
        source=EventSource.HUMAN,
        sender_id="users/analyst-1",
        message="Error en pagos",
        space_id=space_id,
        received_at=datetime.now(UTC),
    )


def test_init_without_credentials():
    notifier = GoogleChatApiNotifier(credentials_path="non_existent.json", log_to_console=False)
    assert not notifier.is_connected()
    assert notifier.sent_messages == []


def test_init_with_real_credentials_if_present():
    real_path = Path("credentials/service_account.json")
    if real_path.exists():
        notifier = GoogleChatApiNotifier(credentials_path=real_path, log_to_console=False)
        assert notifier.is_connected()


def test_send_ack_records_and_attempts_send():
    notifier = GoogleChatApiNotifier(credentials_path="non_existent.json", log_to_console=False)
    ticket = _make_ticket()
    event = _make_event()

    with patch.object(notifier, "_send_to_google_chat", return_value=True) as mock_send:
        notifier.send_ack(event, ticket)
        assert len(notifier.sent_messages) == 1
        assert "Ticket #" in notifier.sent_messages[0].text
        mock_send.assert_called_once()


def test_send_status_update_calls_send():
    notifier = GoogleChatApiNotifier(credentials_path="non_existent.json", log_to_console=False)
    ticket = _make_ticket(status=TicketStatus.ESCALATED, level=SupportLevel.L2)

    with patch.object(notifier, "_send_to_google_chat", return_value=True) as mock_send:
        notifier.send_status_update(ticket, "Escalado a Nivel 2 por SLA")
        assert len(notifier.sent_messages) == 1
        assert "Actualización" in notifier.sent_messages[0].text
        mock_send.assert_called_once()


def test_send_resolution_calls_send():
    notifier = GoogleChatApiNotifier(credentials_path="non_existent.json", log_to_console=False)
    ticket = _make_ticket(status=TicketStatus.RESOLVED)
    ticket.resolved_at = datetime.now(UTC)
    ticket.resolved_by = "runbook:test"
    ticket.resolved_by_auto = True

    with patch.object(notifier, "_send_to_google_chat", return_value=True) as mock_send:
        notifier.send_resolution(ticket)
        assert len(notifier.sent_messages) == 1
        assert "RESUELTO" in notifier.sent_messages[0].text
        assert "automáticamente" in notifier.sent_messages[0].text
        mock_send.assert_called_once()


def test_send_to_google_chat_success():
    notifier = GoogleChatApiNotifier(credentials_path="non_existent.json", log_to_console=False)
    
    with patch.object(notifier, "_get_access_token", return_value="fake-token"), \
         patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        result = notifier._send_to_google_chat("spaces/AAAA1111", "Hola", "spaces/AAAA1111/threads/th1")
        assert result is True
        mock_post.assert_called_once()


def test_send_to_google_chat_invalid_space_skips():
    notifier = GoogleChatApiNotifier(credentials_path="non_existent.json", log_to_console=False)
    with patch.object(notifier, "_get_access_token", return_value="fake-token"):
        # Espacio local o no de google (e.g. "soporte-general")
        result = notifier._send_to_google_chat("soporte-general", "Hola")
        assert result is False
