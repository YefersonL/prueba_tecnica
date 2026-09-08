"""
adapters/llm_mock.py
====================
MockLLMClient — implementación determinística del puerto LLMClient.

USO: exclusivamente en tests. No hace llamadas de red.

Las respuestas están basadas en palabras clave del texto para que los tests
sean expresivos (el test puede controlar la clasificación via el texto del mensaje)
sin depender de un servicio externo. Esto garantiza que la suite de tests
sea ejecutable sin API keys en cualquier entorno (CI, máquina nueva, etc.).

Contrato respetado:
  - Nunca lanza excepciones (retorna P3/UNKNOWN en caso de error).
  - Retorna siempre (Severity, SystemTag, str).
"""

from __future__ import annotations

from core.models import Severity, SystemTag


class MockLLMClient:
    """
    Cliente LLM mock con clasificación basada en palabras clave.

    Diseñado para ser predecible en tests: dado un texto con la palabra
    "fraude" → devuelve P1/FRAUD; con "producción caída" → P0/UNKNOWN, etc.

    En producción esto es reemplazado por GeminiLLMClient sin cambiar el core.
    """

    # Mapeo de palabras clave → (Severity, SystemTag)
    # En orden de prioridad (primero en hacer match gana)
    _KEYWORD_MAP: list[tuple[list[str], Severity, SystemTag]] = [
        (["producción caída", "sistema caído", "down completo"], Severity.P0, SystemTag.UNKNOWN),
        (["fraude", "fraud", "detección de fraude"], Severity.P1, SystemTag.FRAUD),
        (["transacci", "batch nocturno", "cierre contable"], Severity.P1, SystemTag.TRANSACTIONS),
        (["pago", "payment", "pasarela"], Severity.P1, SystemTag.PAYMENTS),
        (["ingesta", "pipeline", "ingestion"], Severity.P1, SystemTag.INGESTION),
        (["autenticaci", "login", "auth"], Severity.P1, SystemTag.AUTH),
        (["reporte", "report", "dashboard"], Severity.P2, SystemTag.REPORTING),
    ]

    def classify_text(self, text: str) -> tuple[Severity, SystemTag, str]:
        """
        Clasifica texto por palabras clave. Case-insensitive.

        Retorna (Severity.P3, SystemTag.UNKNOWN, ...) si no hay match —
        igual que haría el LLM real ante un mensaje ambiguo de bajo impacto.
        """
        lower = text.lower()
        for keywords, severity, system in self._KEYWORD_MAP:
            if any(kw.lower() in lower for kw in keywords):
                summary = f"[Mock LLM] {text[:100]}{'...' if len(text) > 100 else ''}"
                return severity, system, summary

        return (
            Severity.P3,
            SystemTag.UNKNOWN,
            f"[Mock LLM] Clasificación no determinada: {text[:80]}",
        )
