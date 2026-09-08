"""
api/dependencies.py
===================
Inyección de dependencias para la capa FastAPI.

En FastAPI, las dependencias se resuelven por request via `Depends()`.
Aquí centralizamos la construcción del grafo de objetos: qué adaptador
concreto se usa para cada puerto.

Para cambiar de SQLite a Postgres: solo modificar get_ticket_repo().
Para cambiar a Gemini API real: solo modificar get_llm_client().
La lógica de negocio en core/ no se toca.

Variables de entorno:
  GEMINI_API_KEY  : API key para GeminiLLMClient. Si no está, usa MockLLMClient.
  DB_PATH         : Ruta al archivo SQLite. Default: support_system.db
"""

from __future__ import annotations

import os
from functools import lru_cache

from adapters.chat_notifier import LocalChatNotifier
from adapters.queue import InMemoryEventQueue
from adapters.sqlite_repo import SQLiteTicketRepository
from core.classifier import EventClassifier
from core.ports import ChatNotifier, EventQueue, LLMClient, TicketRepository
from core.runbook_engine import RunbookEngine
from core.sla_job import SLAJob
from core.ticket_engine import TicketEngine


@lru_cache(maxsize=1)
def get_ticket_repo() -> SQLiteTicketRepository:
    """
    Singleton del repositorio SQLite.

    lru_cache garantiza que se crea una sola instancia por proceso,
    evitando abrir múltiples conexiones innecesariamente.
    En GCP: reemplazar por CloudSQLTicketRepository o FirestoreTicketRepository.
    """
    db_path = os.getenv("DB_PATH", "support_system.db")
    return SQLiteTicketRepository(db_path=db_path)


@lru_cache(maxsize=1)
def get_event_queue() -> InMemoryEventQueue:
    """
    Singleton de la cola de eventos en memoria.
    En GCP: reemplazar por PubSubEventQueue.
    """
    return InMemoryEventQueue()


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """
    Selecciona el cliente LLM según el entorno.

    Si GEMINI_API_KEY está definida → GeminiLLMClient real.
    Si no → MockLLMClient (seguro para tests y desarrollo local).
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if api_key:
        try:
            from adapters.llm_gemini import GeminiLLMClient
            return GeminiLLMClient(api_key=api_key)
        except ImportError:
            pass  # google-generativeai no instalada → fallback a mock
    from adapters.llm_mock import MockLLMClient
    return MockLLMClient()


@lru_cache(maxsize=1)
def get_notifier() -> LocalChatNotifier:
    """
    Singleton del notificador.
    En GCP: reemplazar por GoogleChatAPINotifier.
    """
    return LocalChatNotifier(log_to_console=True)


@lru_cache(maxsize=1)
def get_ticket_engine() -> TicketEngine:
    return TicketEngine(ticket_repo=get_ticket_repo())


@lru_cache(maxsize=1)
def get_classifier() -> EventClassifier:
    return EventClassifier(
        llm_client=get_llm_client(),
        ticket_repo=get_ticket_repo(),
    )


@lru_cache(maxsize=1)
def get_runbook_engine() -> RunbookEngine:
    return RunbookEngine(
        ticket_engine=get_ticket_engine(),
        notifier=get_notifier(),
    )


@lru_cache(maxsize=1)
def get_sla_job() -> SLAJob:
    return SLAJob(
        ticket_repo=get_ticket_repo(),
        ticket_engine=get_ticket_engine(),
        notifier=get_notifier(),
    )
