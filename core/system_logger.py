"""
core/system_logger.py
=====================
Sistema centralizado de logs para el sistema de soporte fintech.
Registra eventos en:
  1. Consola (stdout) con timestamps y emojis legibles.
  2. Archivo local support_system.log.
  3. Buffer en memoria para consulta en tiempo real desde la API y el Frontend.
"""

import logging
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOG_FILE_PATH = Path("support_system.log")
MAX_BUFFER_SIZE = 200

# Buffer circular en memoria para consumo del dashboard/frontend
_LOG_BUFFER: deque[dict[str, Any]] = deque(maxlen=MAX_BUFFER_SIZE)

# Configuración del Logger nativo de Python
_logger = logging.getLogger("fintech.support")
_logger.setLevel(logging.INFO)

# Evitar duplicación de handlers si se recarga el módulo
if not _logger.handlers:
    _formatter = logging.Formatter(
        "[%(asctime)s UTC] %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # File Handler
    try:
        _file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
        _file_handler.setFormatter(_formatter)
        _logger.addHandler(_file_handler)
    except Exception:
        pass

    # Console Handler
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_formatter)
    _logger.addHandler(_console_handler)


def log_event(level: str, category: str, message: str, details: Any = None) -> None:
    """
    Registra un evento estructurado en el canal interno del backend.
    
    Aisla estrictamente los detalles técnicos y trazas para que nunca
    se expongan en respuestas directas hacia Google Chat o el usuario final.
    """
    now = datetime.now(UTC)
    entry = {
        "timestamp": now.isoformat(),
        "time_str": now.strftime("%H:%M:%S"),
        "level": level.upper(),
        "category": category.upper(),
        "message": message,
        "details": details,
    }

    _LOG_BUFFER.append(entry)

    # Emoji según nivel/categoría
    emoji_map = {
        "INFO": "ℹ️",
        "SUCCESS": "✅",
        "WARNING": "⚠️",
        "ERROR": "❌",
    }
    emo = emoji_map.get(level.upper(), "📝")
    log_text = f"{emo} [{category.upper()}] {message}"
    if details:
        log_text += f" | {details}"

    # Loguear con nivel adecuado en logger nativo
    lvl = level.upper()
    if lvl == "ERROR":
        _logger.error(log_text)
    elif lvl == "WARNING":
        _logger.warning(log_text)
    else:
        _logger.info(log_text)


def get_recent_logs(limit: int = 100) -> list[dict[str, Any]]:
    """Retorna los logs más recientes almacenados en el buffer de memoria."""
    return list(_LOG_BUFFER)[-limit:]


def clear_logs() -> None:
    """Limpia el buffer de logs en memoria."""
    _LOG_BUFFER.clear()
