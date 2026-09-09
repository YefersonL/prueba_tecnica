"""
api/main.py
===========
Aplicación FastAPI — capa de entrada HTTP del sistema de soporte.

Esta capa solo orquesta:
  1. Recibe el request HTTP.
  2. Llama al core con las dependencias inyectadas.
  3. Devuelve la respuesta.

No contiene lógica de negocio. Todo está en core/.

Endpoints:
  POST /webhook/google-chat    — recibe eventos de Google Chat
  POST /sla/run                — dispara el job de SLA manualmente
  GET  /dashboard              — retorna métricas del sistema
  GET  /tickets                — lista tickets (opcional, para debug)
  GET  /health                 — health check

Swagger UI disponible en: http://localhost:8000/docs
ReDoc en:                  http://localhost:8000/redoc
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.dependencies import (
    get_classifier,
    get_event_queue,
    get_notifier,
    get_runbook_engine,
    get_sla_job,
    get_ticket_engine,
    get_ticket_repo,
)
from core.ingestion import normalize_event
from core.metrics import compute_metrics
from core.system_logger import log_event


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Fintech Support System",
    description=(
        "Sistema de soporte automático para fintech. "
        "Clasifica mensajes de Google Chat, crea tickets con SLA, "
        "escala incidencias y genera métricas."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Montar frontend estático Bitrix-style
FRONT_DIR = Path(__file__).resolve().parent.parent / "front"
if FRONT_DIR.exists():
    app.mount("/front", StaticFiles(directory=str(FRONT_DIR)), name="front")

    @app.get("/", include_in_schema=False)
    def serve_frontend() -> FileResponse:
        """Sirve la interfaz web del Helpdesk / Dashboard en la raíz."""
        return FileResponse(FRONT_DIR / "index.html")



# ---------------------------------------------------------------------------
# Schemas de request/response (Pydantic — solo en la capa API)
# ---------------------------------------------------------------------------


class WebhookResponse(BaseModel):
    """Respuesta del endpoint de webhook."""

    event_id: str
    ticket_id: str
    ticket_created: bool
    severity: str
    system: str
    level: str
    resolved_automatically: bool
    runbook_used: str | None
    ack_message: str
    classified_by: str
    text: str | None = None  # Compatible con Google Chat App interactive response
    thread: dict[str, str] | None = None  # Responde en el mismo hilo del chat



class SLAJobResponse(BaseModel):
    """Respuesta del endpoint de SLA job."""

    run_at: str
    total_checked: int
    overdue_escalated: int
    stale_escalated: int
    already_overdue: int
    has_incidents: bool


class DashboardResponse(BaseModel):
    """Respuesta del endpoint de dashboard."""

    computed_at: str
    total_tickets: int
    total_open: int
    auto_resolution_pct: float
    avg_resolution_min: float | None
    sla_breaches_count: int
    volume_by_system: dict[str, int]
    open_by_severity: dict[str, int]
    sla_breaches: list[dict]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", tags=["Sistema"])
def health_check() -> dict[str, str]:
    """
    Health check del sistema.
    Retorna 200 si la API está operativa.
    """
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}


@app.get("/webhook/google-chat", tags=["Ingesta"], include_in_schema=False)
@app.get("/webhook", tags=["Ingesta"], include_in_schema=False)
@app.get("/webhook/", tags=["Ingesta"], include_in_schema=False)
def webhook_health() -> dict[str, str]:
    """Endpoint de verificación para Google Cloud Console."""
    return {"status": "ok", "message": "Google Chat webhook is ready"}


@app.post(
    "/webhook/google-chat",
    response_model=WebhookResponse,
    tags=["Ingesta"],
    summary="Recibir evento de Google Chat",
)
@app.post(
    "/webhook",
    response_model=WebhookResponse,
    tags=["Ingesta"],
    include_in_schema=False,
)
@app.post(
    "/webhook/",
    response_model=WebhookResponse,
    tags=["Ingesta"],
    include_in_schema=False,
)
async def receive_google_chat_event(
    request: Request,
    queue=Depends(get_event_queue),
    classifier=Depends(get_classifier),
    ticket_engine=Depends(get_ticket_engine),
    runbook_engine=Depends(get_runbook_engine),
    notifier=Depends(get_notifier),
) -> WebhookResponse:
    """
    Flujo completo de procesamiento de un evento de Google Chat:
      1. Parsear payload JSON.
      2. Normalizar a RawEvent (detectar HUMAN/MACHINE).
      3. Publicar en la cola de eventos.
      4. Clasificar (reglas o LLM).
      5. Crear o agrupar ticket.
      6. Intentar resolución automática via runbook.
      7. Enviar ACK al espacio de chat (LocalChatNotifier).
      8. Retornar respuesta con detalles.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        log_event("ERROR", "WEBHOOK", f"Payload JSON inválido: {exc}")
        raise HTTPException(status_code=400, detail="Payload JSON inválido.")

    log_event("INFO", "WEBHOOK", f"Petición recibida en {request.url.path}", {"keys": list(payload.keys())})

    # Evento de bienvenida al ser agregado a un espacio o DM en Google Chat
    event_type = payload.get("type", "")
    if event_type == "ADDED_TO_SPACE":
        log_event("INFO", "WEBHOOK", "Evento ADDED_TO_SPACE recibido. Enviando mensaje de bienvenida.")
        welcome_text = (
            "👋 *¡Hola! Soy Fintech Support Bot*\n\n"
            "Estoy listo para recibir reportes de usuarios y alertas de sistemas en este chat.\n"
            "• Clasificaré cada incidencia con IA y asignaré nivel (L1/L2).\n"
            "• Monitorearé los tiempos límite de SLA.\n"
            "• Ejecutaré runbooks de recuperación automática en fallas recurrentes."
        )
        return WebhookResponse(
            event_id="evt-welcome",
            ticket_id="N/A",
            ticket_created=False,
            severity="INFO",
            system="GENERAL",
            level="L1",
            resolved_automatically=False,
            runbook_used=None,
            ack_message=welcome_text,
            classified_by="system",
            text=welcome_text,
        )

    # 1. Normalizar
    try:
        event = normalize_event(payload)
        log_event(
            "SUCCESS",
            "INGESTION",
            f"Evento normalizado | Origen: {event.source.value.upper()} | Sender: {event.sender_id}",
            {"message": event.message[:120], "space": event.space_id},
        )
    except (KeyError, ValueError) as exc:
        log_event("ERROR", "INGESTION", f"Error de normalización: {exc}", {"payload": str(payload)[:200]})
        raise HTTPException(status_code=422, detail=str(exc))

    # 2. Publicar en cola (desacopla ingesta del procesamiento)
    queue.publish(event)
    consumed = queue.consume()  # Procesamiento síncrono para la prueba técnica

    # 3. Clasificar
    classification = classifier.classify(consumed)
    log_event(
        "INFO",
        "CLASSIFIER",
        f"Clasificado: {classification.severity.value} | Sistema: {classification.system.value} | Método: {classification.classified_by}",
        {"is_duplicate": classification.is_duplicate},
    )

    # 4. Motor de tickets
    engine_result = ticket_engine.process(consumed, classification)
    ticket = engine_result.ticket
    log_event(
        "SUCCESS",
        "TICKET",
        f"Ticket #{ticket.ticket_id[:8]} ({ticket.status.value}) | Nivel: {ticket.level.value} | Origen: {ticket.source.value.upper()}",
        {"created": engine_result.created, "escalated": ticket.escalated},
    )

    # 5. Intentar runbook (solo si es ticket nuevo, no duplicado)
    resolved_auto = False
    runbook_name = None
    if engine_result.created:
        rb_result = runbook_engine.try_auto_resolve(ticket)
        resolved_auto = rb_result.resolved
        runbook_name = rb_result.runbook_name
        if resolved_auto:
            log_event(
                "SUCCESS",
                "RUNBOOK",
                f"Ticket #{ticket.ticket_id[:8]} auto-resuelto por '{runbook_name}'",
            )

    # 6. ACK al chat (si no fue resuelto auto, la resolución la notifica el runbook)
    if not resolved_auto:
        notifier.send_ack(consumed, ticket)

    # Preparar mensaje de ACK para la respuesta
    last_msg = notifier.sent_messages[-1] if notifier.sent_messages else None
    ack_text = last_msg.text if last_msg else "Evento procesado."
    log_event("SUCCESS", "CHAT_API", f"ACK enviado al chat: {ack_text[:80]}...")

    # Preservar el hilo de Google Chat para responder en el mismo hilo
    thread_info = None
    msg_obj = payload.get("message", {})
    if isinstance(msg_obj, dict) and "thread" in msg_obj and isinstance(msg_obj["thread"], dict):
        thread_name = msg_obj["thread"].get("name")
        if thread_name:
            thread_info = {"name": thread_name}

    return WebhookResponse(
        event_id=consumed.event_id,
        ticket_id=ticket.ticket_id,
        ticket_created=engine_result.created,
        severity=ticket.severity.value,
        system=ticket.system.value,
        level=ticket.level.value,
        resolved_automatically=resolved_auto,
        runbook_used=runbook_name,
        ack_message=ack_text,
        classified_by=classification.classified_by,
        text=ack_text,
        thread=thread_info,
    )




@app.post(
    "/sla/run",
    response_model=SLAJobResponse,
    tags=["SLA"],
    summary="Ejecutar job de SLA manualmente",
    description=(
        "Dispara el job de SLA que detecta tickets vencidos y estancados. "
        "En producción esto lo haría Cloud Scheduler cada 5 minutos."
    ),
)
def run_sla_job(
    sla_job=Depends(get_sla_job),
) -> SLAJobResponse:
    """Ejecuta el SLA job y retorna el reporte de lo encontrado."""
    report = sla_job.run()
    return SLAJobResponse(
        run_at=report.run_at.isoformat(),
        total_checked=report.total_checked,
        overdue_escalated=len(report.overdue_escalated),
        stale_escalated=len(report.stale_escalated),
        already_overdue=len(report.already_overdue),
        has_incidents=report.has_incidents,
    )


@app.get(
    "/dashboard",
    response_model=DashboardResponse,
    tags=["Métricas"],
    summary="Dashboard de métricas del sistema",
    description=(
        "Retorna métricas agregadas: volumen por sistema, % resuelto automáticamente, "
        "tiempo promedio de resolución e incumplimientos de SLA actuales."
    ),
)
def get_dashboard(
    repo=Depends(get_ticket_repo),
) -> DashboardResponse:
    """Calcula y retorna las métricas del sistema en tiempo real."""
    metrics = compute_metrics(repo)
    return DashboardResponse(
        computed_at=metrics.computed_at.isoformat(),
        total_tickets=metrics.total_tickets,
        total_open=metrics.total_open,
        auto_resolution_pct=metrics.auto_resolution_pct,
        avg_resolution_min=metrics.avg_resolution_min,
        sla_breaches_count=len(metrics.sla_breaches),
        volume_by_system=metrics.volume_by_system,
        open_by_severity=metrics.open_by_severity,
        sla_breaches=metrics.sla_breaches,
    )


@app.get(
    "/tickets",
    tags=["Tickets"],
    summary="Listar todos los tickets",
    description="Retorna la lista completa de tickets. Útil para debugging.",
)
def list_tickets(
    repo=Depends(get_ticket_repo),
) -> list[dict[str, Any]]:
    """Lista todos los tickets con sus campos principales."""
    tickets = repo.list_all()
    return [
        {
            "ticket_id": t.ticket_id,
            "system": t.system.value,
            "severity": t.severity.value,
            "status": t.status.value,
            "level": t.level.value,
            "source": t.source.value if hasattr(t, "source") and t.source else "human",
            "escalated": t.escalated,
            "resolved_by_auto": t.resolved_by_auto,
            "resolved_by": t.resolved_by,
            "space_id": t.space_id,
            "source_event_id": t.source_event_id,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "sla_deadline": t.sla_deadline.isoformat() if t.sla_deadline else None,
            "summary": t.summary,
        }
        for t in tickets
    ]


@app.get(
    "/api/logs",
    tags=["Sistema"],
    summary="Obtener logs del sistema en tiempo real",
)
def get_system_logs(limit: int = 100) -> list[dict[str, Any]]:
    """Retorna los logs recientes para visualización en tiempo real en el frontend."""
    from core.system_logger import get_recent_logs
    return get_recent_logs(limit=limit)


@app.delete(
    "/api/logs",
    tags=["Sistema"],
    summary="Limpiar logs del sistema",
)
def clear_system_logs() -> dict[str, str]:
    """Limpia el buffer de logs en memoria."""
    from core.system_logger import clear_logs
    clear_logs()
    return {"status": "ok", "message": "Logs limpiados"}


