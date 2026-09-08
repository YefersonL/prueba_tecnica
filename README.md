# Fintech Support System — Prueba Técnica

Sistema de soporte automático que reemplaza el soporte manual por Google Chat en una fintech. Clasifica mensajes, crea tickets con SLA, escala incidencias y genera métricas — todo con arquitectura hexagonal lista para migrar a GCP.

---

## Arquitectura

```
core/           ← Lógica de negocio pura. Sin imports de SQLite, FastAPI ni LLMs.
  models.py     ← Entidades: RawEvent, Ticket (dataclasses + enums)
  ports.py      ← Interfaces (Protocol): EventQueue, TicketRepository, LLMClient, ChatNotifier

adapters/       ← Implementaciones concretas de los puertos
  queue.py      ← InMemoryEventQueue  (→ Cloud Pub/Sub en GCP)
  sqlite_repo.py← SQLiteTicketRepository (→ Cloud SQL/Postgres en GCP)
  llm_mock.py   ← MockLLMClient       (→ Vertex AI / Anthropic en GCP)
  chat_notifier.py ← LocalChatNotifier (→ Google Chat REST API en GCP)

api/            ← Capa HTTP (FastAPI). Solo orquesta, sin lógica de negocio.
  main.py       ← App FastAPI + endpoints
  dependencies.py ← Inyección de dependencias

tests/          ← Tests con Pytest (un test por unidad de trabajo)
fixtures/       ← Mocks y datos de prueba (JSON de webhooks simulados)
```

### Patrón Puertos-y-Adaptadores (Hexagonal)

La lógica de negocio en `core/` **depende únicamente de interfaces** (los `Protocol` en `ports.py`). Los adaptadores concretos se inyectan por constructor. Esto permite:

- **Testear** el core sin base de datos ni llamadas HTTP.
- **Migrar a GCP** cambiando solo el adaptador, sin tocar el core.

### Mapa de migración a GCP

| Adaptador local | Reemplazar por | Servicio GCP |
|---|---|---|
| `InMemoryEventQueue` | `PubSubEventQueue` | Cloud Pub/Sub |
| `SQLiteTicketRepository` | `CloudSQLTicketRepository` | Cloud SQL (Postgres) |
| `GeminiLLMClient` (Gemini API) | `VertexAILLMClient` | Vertex AI (Gemini) — mismo modelo, distinta autenticación |
| `LocalChatNotifier` | `GoogleChatAPINotifier` | Google Chat REST API |
| `ManualSLAJob` (función) | Cloud Scheduler + Cloud Run Job | Cloud Scheduler |

---

## Qué está mockeado y por qué

| Componente | Mock | Razón |
|---|---|---|
| Google Chat webhook | `fixtures/mock_google_chat_webhook.json` | Sin credenciales de workspace real |
| LLM (clasificación) | **Gemini API real** (`adapters/llm_gemini.py`) | Se usará `google-generativeai` con API key en `.env` |
| Google Chat API (notificaciones) | `LocalChatNotifier` genera texto, no llama HTTP | Sin OAuth bot configurado |
| Scheduler SLA | Función invocable manualmente | Sin Cloud Scheduler / APScheduler en scope |

**Principio aplicado**: los mocks están en los adaptadores, nunca en el core. El core no sabe si está hablando con un mock o con producción.

---

## Cómo correr el proyecto

### Requisitos
- Python 3.11+
- pip (o uv)

### Instalación

```bash
# Clonar e instalar dependencias de desarrollo
pip install -e ".[dev]"
```

### Ejecutar tests

```bash
pytest
# Con cobertura:
pytest --cov=core --cov=adapters --cov-report=term-missing
```

### Levantar la API

```bash
uvicorn api.main:app --reload
# Swagger UI: http://localhost:8000/docs
```

### Probar el webhook manualmente

```bash
curl -X POST http://localhost:8000/webhook/google-chat \
  -H "Content-Type: application/json" \
  -d @fixtures/mock_google_chat_webhook.json
```

---

## Plan de construcción (estado actual)

- [x] **Paso 1**: Estructura base + interfaces (modelos, puertos)
- [x] **Paso 2**: Ingesta y distinción humano/máquina
- [x] **Paso 3**: Clasificador (reglas + Gemini API) + de-duplicación
- [x] **Paso 4**: Motor de tickets (creación, asignación, escalamiento) + SQLite
- [x] **Paso 5**: Job de SLA (detección de estancamiento)
- [x] **Paso 6**: Comunicación con el usuario (ChatNotifier) — incluido en Paso 5
- [x] **Paso 7**: Automatización de lo recurrente (RunbookEngine)
- [x] **Paso 8**: Dashboard/métricas (endpoint JSON) + API FastAPI completa

---

## Lo que quedaría para una v2

Estas decisiones están fuera del scope de la prueba técnica pero son defendibles en la entrevista:

- **Autenticación del webhook**: Verificación de la firma HMAC de Google Chat (header `X-Goog-Signature`).
- **Multi-workspace**: Soporte para múltiples organizaciones/spaces con configuración por tenant.
- **Scheduler real**: Reemplazar la función manual de SLA por Cloud Scheduler → Cloud Run Job.
- **UI de dashboard**: Frontend React o Looker Studio conectado a BigQuery.
- **Dead Letter Queue**: Para eventos que fallan clasificación repetidamente.
- **Runbooks adicionales**: Hoy solo hay un ejemplo concreto de automatización; en v2 serían configurables por YAML.
- **Tracing distribuido**: OpenTelemetry para correlacionar eventos → tickets → notificaciones.
- **Rate limiting**: En el endpoint de webhook para protegerse de floods.

---

## Decisiones de diseño destacadas

1. **`Protocol` vs ABC**: Se usa `Protocol` (PEP 544) para las interfaces porque permite duck typing estructural — los adaptadores no necesitan heredar explícitamente, lo que facilita mocks sin boilerplate.

2. **`dataclass` vs Pydantic para modelos de dominio**: Los modelos de `core/` usan `dataclass` (stdlib) para mantener el core sin dependencias externas. Pydantic se usa en la capa API para validación de requests/responses.

3. **Severidad como P0-P3 vs "critical/high/medium/low"**: La escala Px es estándar en SRE y fintech — todos los ingenieros la entienden inmediatamente, y mapea directamente a tiempos de SLA.

4. **SQLite en vez de Postgres local**: Postgres requeriría Docker en el entorno de prueba; SQLite es stdlib y reproduce el mismo contrato SQL. El adaptador es el único cambio necesario para ir a producción.
