"""
core/system_logger.py
=====================
Sistema centralizado de logs para el sistema de soporte fintech.
Registra eventos en:
  1. Consola (stdout) con timestamps y emojis legibles.
  2. Archivo local support_system.log.
  3. Buffer en memoria para consulta en tiempo real desde la API y el Frontend.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOG_FILE_PATH = Path("support_system.log")
MAX_BUFFER_SIZE = 200

# Buffer circular en memoria
_LOG_BUFFER: deque[dict[str, Any]] = deque(maxlen=MAX_BUFFER_SIZE)


def log_event(level: str, category: str, message: str, details: Any = None) -> None:
    """
    Registra un evento estructurado.
    
    Parámetros
    ----------
    level    : INFO | SUCCESS | WARNING | ERROR
    category : WEBHOOK | INGESTION | CLASSIFIER | TICKET | RUNBOOK | CHAT_API | SLA
    message  : Mensaje descriptivo
    details  : Diccionario o string con información técnica complementaria
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
    line = f"[{entry['time_str']} UTC] {emo} [{category.upper()}] {message}"
    if details:
        line += f" | {details}"

    # 1. Stdout
    print(line, flush=True)

    # 2. Archivo local
    try:
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_recent_logs(limit: int = 100) -> list[dict[str, Any]]:
    """Retorna los logs más recientes almacenados en el buffer de memoria."""
    return list(_LOG_BUFFER)[-limit:]


def clear_logs() -> None:
    """Limpia el buffer de logs en memoria."""
    _LOG_BUFFER.clear()
