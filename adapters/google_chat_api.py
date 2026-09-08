"""
adapters/google_chat_api.py
===========================
Implementación real de ChatNotifier para Google Chat API.

Autentica con la Service Account de GCP (OAuth2) y publica mensajes
directamente en los espacios y threads de Google Chat usando la REST API v1.

Endpoints utilizados:
  POST https://chat.googleapis.com/v1/{space_id}/messages
  Scopes requeridos: https://www.googleapis.com/auth/chat.bot
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from adapters.chat_notifier import LocalChatNotifier, SentMessage
from core.models import RawEvent, Ticket

logger = logging.getLogger(__name__)

# Scopes requeridos por la API de Google Chat para bots
GOOGLE_CHAT_SCOPES = ["https://www.googleapis.com/auth/chat.bot"]
DEFAULT_CREDENTIALS_PATH = Path("credentials/service_account.json")


class GoogleChatApiNotifier(LocalChatNotifier):
    """
    Notificador real que envía mensajes a Google Chat vía REST API.
    
    Hereda de LocalChatNotifier para mantener en memoria `sent_messages`
    (lo que permite inspeccionar y testear sin red), pero ejecuta
    las llamadas HTTP reales a Google Chat si las credenciales están presentes.
    """

    def __init__(
        self,
        credentials_path: str | Path | None = None,
        log_to_console: bool = True,
    ) -> None:
        super().__init__(log_to_console=log_to_console)
        self.credentials_path = (
            Path(credentials_path)
            if credentials_path
            else Path(os.getenv("GOOGLE_APPLICATION_CREDENTIALS", str(DEFAULT_CREDENTIALS_PATH)))
        )
        self._credentials = None
        self._init_credentials()

    def _init_credentials(self) -> None:
        """Inicializa las credenciales de la cuenta de servicio si el archivo existe."""
        if not self.credentials_path.exists():
            logger.warning(
                f"[GoogleChatApiNotifier] Archivo de credenciales no encontrado en: {self.credentials_path}. "
                "Operando en modo emulación local."
            )
            return

        try:
            from google.oauth2 import service_account

            self._credentials = service_account.Credentials.from_service_account_file(
                str(self.credentials_path),
                scopes=GOOGLE_CHAT_SCOPES,
            )
            logger.info(
                f"[GoogleChatApiNotifier] Credenciales cargadas exitosamente para: "
                f"{getattr(self._credentials, 'service_account_email', 'Service Account')}"
            )
        except Exception as exc:
            logger.error(f"[GoogleChatApiNotifier] Error cargando credenciales: {exc}")
            self._credentials = None

    def is_connected(self) -> bool:
        """Retorna True si tiene credenciales válidas cargadas."""
        return self._credentials is not None

    def _get_access_token(self) -> str | None:
        """Obtiene o refresca el token de acceso OAuth2."""
        if not self._credentials:
            return None

        try:
            from google.auth.transport.requests import Request

            if not self._credentials.valid or not self._credentials.token:
                self._credentials.refresh(Request())
            return self._credentials.token
        except Exception as exc:
            logger.error(f"[GoogleChatApiNotifier] Error al refrescar token OAuth2: {exc}")
            return None

    def _send_to_google_chat(self, space_id: str, text: str, thread_name: str | None = None) -> bool:
        """
        Envía un mensaje HTTP POST a la API REST de Google Chat.
        
        Endpoint: POST https://chat.googleapis.com/v1/{space_id}/messages
        """
        token = self._get_access_token()
        if not token:
            return False

        # Si el space_id no tiene el formato estándar 'spaces/XXXX', no es un space real de Google
        if not space_id or not space_id.startswith("spaces/"):
            logger.debug(f"[GoogleChatApiNotifier] space_id '{space_id}' no es un espacio real de Google Chat.")
            return False

        import requests

        url = f"https://chat.googleapis.com/v1/{space_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        
        payload: dict[str, Any] = {"text": text}
        if thread_name:
            payload["thread"] = {"name": thread_name}
            params = {"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"}
        else:
            params = {}

        try:
            resp = requests.post(url, headers=headers, json=payload, params=params, timeout=10.0)
            if resp.status_code in (200, 201):
                logger.info(f"[GoogleChatApiNotifier] Mensaje enviado a {space_id}: {resp.status_code}")
                return True
            else:
                logger.warning(
                    f"[GoogleChatApiNotifier] Google Chat API respondió {resp.status_code}: {resp.text}"
                )
                return False
        except Exception as exc:
            logger.error(f"[GoogleChatApiNotifier] Error de red enviando a Google Chat: {exc}")
            return False

    def send_ack(self, event: RawEvent, ticket: Ticket) -> None:
        super().send_ack(event, ticket)
        thread_name = f"{event.space_id}/threads/{ticket.ticket_id}" if event.space_id else None
        last_msg = self.sent_messages[-1]
        self._send_to_google_chat(event.space_id, last_msg.text, thread_name=thread_name)

    def send_status_update(self, ticket: Ticket, message: str) -> None:
        super().send_status_update(ticket, message)
        thread_name = f"{ticket.space_id}/threads/{ticket.ticket_id}" if ticket.space_id else None
        last_msg = self.sent_messages[-1]
        self._send_to_google_chat(ticket.space_id, last_msg.text, thread_name=thread_name)

    def send_resolution(self, ticket: Ticket) -> None:
        super().send_resolution(ticket)
        thread_name = f"{ticket.space_id}/threads/{ticket.ticket_id}" if ticket.space_id else None
        last_msg = self.sent_messages[-1]
        self._send_to_google_chat(ticket.space_id, last_msg.text, thread_name=thread_name)
