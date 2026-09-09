"""
adapters/llm_gemini.py
======================
GeminiLLMClient — implementación del puerto LLMClient usando Google Generative AI.

Usa el modelo gemini-2.0-flash (rápido, económico, suficiente para clasificación).

Configuración:
  Requiere la variable de entorno GEMINI_API_KEY (o GOOGLE_API_KEY).
  En GCP, se puede reemplazar por autenticación via Application Default Credentials
  (ADC) con Vertex AI — el cambio es solo en __init__, la lógica de prompt no cambia.

Migración a Vertex AI (GCP):
  Cambiar el import y el constructor:
    from vertexai.generative_models import GenerativeModel
    model = GenerativeModel("gemini-2.0-flash-001")
  El prompt y el parsing de respuesta son idénticos.

Prompt engineering:
  El prompt es estructurado (JSON output) para que el parsing sea robusto.
  No usamos function calling por simplicidad — con JSON mode es suficiente
  para una prueba técnica y más fácil de depurar.

Contrato del puerto:
  - Nunca lanza excepción al caller. Errores → (P3, UNKNOWN, mensaje de error).
  - Latencia esperada: 1-3s por clasificación (modelo flash).
"""

from __future__ import annotations

import json
import os

from core.models import Severity, SystemTag

# Import condicional para que el módulo sea importable sin la librería instalada
# (los tests usan MockLLMClient y no necesitan google-generativeai)
try:
    import google.generativeai as genai
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

_SYSTEM_TAG_VALUES = [t.value for t in SystemTag]
_SEVERITY_VALUES = [s.value for s in Severity]

_CLASSIFICATION_PROMPT = """\
Eres un sistema experto de clasificación de incidencias para una fintech.
Analiza el siguiente mensaje y clasifícalo.

MENSAJE:
{text}

Responde ÚNICAMENTE con un objeto JSON válido con esta estructura exacta:
{{
  "severity": "<P0|P1|P2|P3>",
  "system": "<{systems}>",
  "summary": "<resumen de 1-2 oraciones en español>"
}}

Guía de severidad:
- P0: producción completamente caída, pérdida de datos activa, impacto crítico e inmediato
- P1: degradación severa, pipeline de datos caído, funcionalidad core afectada sin workaround
- P2: funcionalidad afectada pero existe workaround, latencia elevada, impacto parcial
- P3: informativo, bajo impacto, puede atenderse en horario laboral

Sistemas disponibles: {systems}
Si no puedes determinar el sistema, usa "unknown".
"""


class GeminiLLMClient:
    """
    Cliente real de Gemini API para clasificación de texto libre.

    Parámetros
    ----------
    api_key : API key de Google Generative AI. Si no se pasa, se lee de
              la variable de entorno GEMINI_API_KEY o GOOGLE_API_KEY.
    model   : Nombre del modelo a usar. Default: gemini-2.0-flash.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        if not _GENAI_AVAILABLE:
            raise ImportError(
                "La librería 'google-generativeai' no está instalada. "
                "Ejecuta: pip install google-generativeai"
            )

        resolved_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Se requiere GEMINI_API_KEY. "
                "Defínela en una variable de entorno o pásala al constructor."
            )

        resolved_model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        genai.configure(api_key=resolved_key)
        self._model = genai.GenerativeModel(resolved_model)
        self._model_name = resolved_model

    def classify_text(self, text: str) -> tuple[Severity, SystemTag, str]:
        """
        Clasifica texto libre usando Gemini y retorna (severity, system, summary).

        El LLM responde en JSON estructurado. Si la respuesta no es parseable
        o el modelo falla, retorna (P3, UNKNOWN, mensaje de error) — cumpliendo
        el contrato del puerto de nunca propagar excepciones al caller.
        """
        systems_str = "|".join(_SYSTEM_TAG_VALUES)
        prompt = _CLASSIFICATION_PROMPT.format(text=text, systems=systems_str)

        try:
            response = self._model.generate_content(
                prompt,
                generation_config={
                    "temperature": 0.1,        # baja temperatura = más determinístico
                    "max_output_tokens": 1024, # suficiente espacio para tokens de razonamiento + JSON
                    "response_mime_type": "application/json",
                },
            )
            raw = response.text.strip()
            return self._parse_response(raw)

        except Exception as exc:  # noqa: BLE001 — captura intencionada (contrato del puerto)
            return (
                Severity.P3,
                SystemTag.UNKNOWN,
                f"[Gemini error] Clasificación no disponible: {type(exc).__name__}",
            )

    def _parse_response(self, raw: str) -> tuple[Severity, SystemTag, str]:
        """
        Parsea la respuesta JSON del modelo y valida los valores.

        Si el JSON es inválido o los valores están fuera de los enums,
        retorna (P3, UNKNOWN, ...) en vez de lanzar excepción.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return (
                Severity.P3,
                SystemTag.UNKNOWN,
                f"[Gemini parse error] Respuesta no válida: {raw[:100]}",
            )

        try:
            severity = Severity(data.get("severity", "P3"))
        except ValueError:
            severity = Severity.P3

        try:
            system = SystemTag(data.get("system", "unknown"))
        except ValueError:
            system = SystemTag.UNKNOWN

        summary = data.get("summary", "Sin resumen disponible")[:500]

        return severity, system, summary
