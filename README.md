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

## Matriz de SLAs y Tiempos de Respuesta

El sistema implementa una política estricta de SLAs diseñada para operaciones críticas fintech, equilibrando velocidad de respuesta humana/automática con escalamiento preventivo ante inactividad:

| Severidad | Nivel Inicial | Tiempo 1ª Respuesta (ACK) | Límite Estancamiento (`is_stale`) | Límite Máximo de Resolución (`sla_deadline`) | Acción al Vencer / Estancarse |
|---|:---:|:---:|:---:|:---:|---|
| **P0 (Crítico)** | **L2** (Senior / SRE) | Inmediata (&le; 15 min) | N/A (Asignado directo a L2) | **1 hora** | Alerta roja en Chat + Escalado a Guardia SRE |
| **P1 (Alto)** | **L1** | Inmediata (&le; 30 min) | **45 minutos** sin `updated_at` | **4 horas** | Escala automático de L1 a **L2** + Notificación |
| **P2 (Medio)** | **L1** | &le; 2 horas | **4 horas** sin `updated_at` | **24 horas** (1 día) | Escala automático de L1 a **L2** + Notificación |
| **P3 (Bajo)** | **L1** | &le; 4 horas | Sin escalamiento automático | **72 horas** (3 días) | Permanece en L1 hasta cierre o priorización |

---

### Mecánica del Job de SLA (`core/sla_job.py`)

El job de evaluación periódica (diseñado para ejecutarse cada 5 minutos mediante **Cloud Scheduler** en GCP o vía `POST /sla/run`) recorre todos los tickets abiertos y ejecuta dos reglas deterministas basadas en timestamps UTC:

1. **Detección de Vencimiento (`is_overdue`)**:
   - Se evalúa contra el límite de resolución global del ticket:
     $$\text{is\_overdue} \iff \text{now}() > \text{ticket.sla\_deadline} \quad \wedge \quad \text{ticket.status} \notin \{\text{RESOLVED}, \text{CLOSED}\}$$
   - Si el ticket venció y permanece en L1, el job ejecuta `ticket.escalate_to_l2(reason="SLA vencido...")` y despacha un mensaje de alerta roja al espacio de Google Chat mediante `ChatNotifier`.

2. **Detección de Estancamiento (`is_stale`)**:
   - Evita que incidencias queden olvidadas en la cola sin interacción humana, monitoreando la última marca de tiempo de actividad:
     $$\Delta t = \text{now}() - \text{ticket.updated\_at}$$
   - Si $\Delta t > \text{threshold}$ (45 min para P1, 4 horas para P2) y el ticket aún está en L1, el job lo detecta como candidato a escalamiento proactivo.
   - Escala el ticket a L2 **antes de que venza el SLA definitivo**, permitiendo que un ingeniero senior intervenga a tiempo.

3. **Garantía de Idempotencia y Actualización de Estado**:
   - Cada transición o comentario de agente invoca `ticket.touch()`, renovando `updated_at = datetime.now(UTC)` y reiniciando la ventana de estancamiento.
   - El job no re-escala tickets que ya alcanzaron el nivel L2 (`ticket.level == SupportLevel.L2`).

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
