/**
 * front/app.js
 * ============
 * Controlador reactivo para FintechDesk (Frontend Bitrix-Style).
 * Conecta con la API de FastAPI:
 *   - GET  /dashboard
 *   - GET  /tickets
 *   - POST /webhook/google-chat
 *   - POST /sla/run
 */

// Estado global de la aplicación
const state = {
  tickets: [],
  metrics: null,
  activeFilter: 'all',
  activeSource: 'all',
  searchTerm: '',
  autoRefresh: true,
  timerId: null,
};

// ---------------------------------------------------------------------------
// Inicialización
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  setupNavigation();
  setupFilters();
  setupSimulator();
  setupModal();
  setupGlobalActions();
  setupLogActions();

  // Carga inicial
  fetchAllData();

  // Polling automático cada 5 segundos
  state.timerId = setInterval(() => {
    if (state.autoRefresh) {
      fetchAllData(true);
    }
  }, 5000);
});

// ---------------------------------------------------------------------------
// Navegación de Pestañas (Estilo Bitrix24)
// ---------------------------------------------------------------------------
function setupNavigation() {
  const navButtons = document.querySelectorAll('.nav-item');
  const panes = document.querySelectorAll('.tab-pane');
  const viewTitle = document.getElementById('view-title');
  const viewBreadcrumb = document.getElementById('view-breadcrumb');

  const titles = {
    'tab-dashboard': {
      title: 'Tablero General',
      desc: 'Monitoreo de incidencias y métricas en tiempo real',
      target: 'tab-tickets', // En modo dashboard muestra tickets y KPIs
    },
    'tab-tickets': {
      title: 'Casos & Mesa de Ayuda',
      desc: 'Listado completo de tickets asignados a Nivel 1 y Nivel 2',
      target: 'tab-tickets',
    },
    'tab-simulator': {
      title: 'Simulador Interactivo de Chat',
      desc: 'Prueba el comportamiento de la IA y los runbooks con eventos de Google Chat',
      target: 'tab-simulator',
    },
    'tab-runbooks': {
      title: 'Catálogo de Runbooks & Automatización',
      desc: 'Solución autónoma de incidencias recurrentes de infraestructura',
      target: 'tab-runbooks',
    },
    'tab-metrics': {
      title: 'Métricas de Soporte & SLAs',
      desc: 'Volumen por componente bancario e historial de cumplimiento',
      target: 'tab-metrics',
    },
    'tab-logs': {
      title: 'Consola de Logs del Sistema en Vivo',
      desc: 'Depuración en tiempo real de peticiones, clasificación con IA y ejecución de runbooks',
      target: 'tab-logs',
    },
  };


  navButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      const tabId = btn.getAttribute('data-tab');
      navButtons.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      const config = titles[tabId] || { title: 'Mesa de Ayuda', desc: '', target: tabId };
      viewTitle.textContent = config.title;
      viewBreadcrumb.textContent = config.desc;

      panes.forEach(pane => {
        pane.classList.toggle('active', pane.id === config.target);
      });
    });
  });
}

// ---------------------------------------------------------------------------
// Filtros y Búsqueda
// ---------------------------------------------------------------------------
function setupFilters() {
  // Filtro por Estado
  const statusChips = document.querySelectorAll('#status-filters .chip');
  statusChips.forEach(chip => {
    chip.addEventListener('click', () => {
      statusChips.forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      state.activeFilter = chip.getAttribute('data-filter');
      renderTicketsTable();
    });
  });

  // Filtro por Origen (Humano / Máquina)
  const sourceChips = document.querySelectorAll('#source-filters .chip');
  sourceChips.forEach(chip => {
    chip.addEventListener('click', () => {
      sourceChips.forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      state.activeSource = chip.getAttribute('data-source');
      renderTicketsTable();
    });
  });

  // Buscador de tickets
  const searchInput = document.getElementById('ticket-search');
  searchInput.addEventListener('input', (e) => {
    state.searchTerm = e.target.value.toLowerCase().trim();
    renderTicketsTable();
  });
}

// ---------------------------------------------------------------------------
// Conexión y Peticiones a la API
// ---------------------------------------------------------------------------
async function fetchAllData(isBackground = false) {
  try {
    const [dashboardRes, ticketsRes, logsRes] = await Promise.all([
      fetch('/dashboard'),
      fetch('/tickets'),
      fetch('/api/logs?limit=100'),
    ]);

    if (dashboardRes.ok) {
      state.metrics = await dashboardRes.json();
      renderKPIs(state.metrics);
      renderMetricsTab(state.metrics);
    }

    if (ticketsRes.ok) {
      const data = await ticketsRes.json();
      state.tickets = Array.isArray(data) ? data : (data.tickets || []);
      updateFilterCounts();
      renderTicketsTable();
    }

    if (logsRes.ok) {
      const logs = await logsRes.json();
      renderLogs(logs);
    }
  } catch (err) {
    if (!isBackground) {
      showToast('Error conectando con la API del servidor', 'error');
    }
  }
}


// ---------------------------------------------------------------------------
// Renderizado de KPIs (Widgets Bitrix Style)
// ---------------------------------------------------------------------------
function renderKPIs(metrics) {
  if (!metrics) return;

  document.getElementById('kpi-total-tickets').textContent = metrics.total_tickets || 0;
  document.getElementById('kpi-open-tickets').textContent = metrics.total_open || 0;
  document.getElementById('nav-open-count').textContent = metrics.total_open || 0;

  // Desglose severidad abiertos
  const bySev = metrics.open_by_severity || {};
  document.getElementById('kpi-open-p0').textContent = `${bySev.P0 || 0} P0`;
  document.getElementById('kpi-open-p1').textContent = `${bySev.P1 || 0} P1`;
  document.getElementById('kpi-open-p2').textContent = `${bySev.P2 || 0} P2`;
  document.getElementById('kpi-open-p3').textContent = `${bySev.P3 || 0} P3`;

  // Auto-resolución
  const autoPct = (metrics.auto_resolution_pct || 0).toFixed(1);
  document.getElementById('kpi-auto-pct').textContent = `${autoPct}%`;

  // SLA Breaches
  const slaCount = metrics.sla_breaches_count || 0;
  document.getElementById('kpi-sla-breaches').textContent = slaCount;
  document.getElementById('kpi-sla-subtext').textContent =
    slaCount === 1 ? '1 caso fuera de SLA' : `${slaCount} casos fuera de SLA`;

  // Tiempo promedio
  const avgMin = metrics.avg_resolution_min;
  if (avgMin !== null && avgMin !== undefined) {
    document.getElementById('kpi-avg-time').textContent = `${avgMin.toFixed(1)} min`;
  } else {
    document.getElementById('kpi-avg-time').textContent = '--';
  }
}

// ---------------------------------------------------------------------------
// Renderizado de la Tabla de Tickets
// ---------------------------------------------------------------------------
function updateFilterCounts() {
  const all = state.tickets.length;
  const open = state.tickets.filter(t => t.status === 'open' || t.status === 'in_progress').length;
  const escalated = state.tickets.filter(t => t.level === 'L2' || t.escalated || t.status === 'escalated').length;
  const auto = state.tickets.filter(t => t.resolved_by_auto).length;
  const p0 = state.tickets.filter(t => t.severity === 'P0').length;

  document.getElementById('count-all').textContent = all;
  document.getElementById('count-open').textContent = open;
  document.getElementById('count-escalated').textContent = escalated;
  document.getElementById('count-auto').textContent = auto;
  document.getElementById('count-p0').textContent = p0;
}

function renderTicketsTable() {
  const tbody = document.getElementById('tickets-table-body');
  if (!tbody) return;

  // Filtrado reactivo
  const filtered = state.tickets.filter(ticket => {
    // Filtro por Estado
    if (state.activeFilter === 'open' && ticket.status !== 'open' && ticket.status !== 'in_progress') return false;
    if (state.activeFilter === 'escalated' && ticket.level !== 'L2' && !ticket.escalated && ticket.status !== 'escalated') return false;
    if (state.activeFilter === 'auto' && !ticket.resolved_by_auto) return false;
    if (state.activeFilter === 'p0' && ticket.severity !== 'P0') return false;

    // Filtro por Origen
    const isMachine = (ticket.source === 'machine');
    if (state.activeSource === 'machine' && !isMachine) return false;
    if (state.activeSource === 'human' && isMachine) return false;

    // Búsqueda por texto
    if (state.searchTerm) {
      const matchId = ticket.ticket_id.toLowerCase().includes(state.searchTerm);
      const matchSummary = ticket.summary.toLowerCase().includes(state.searchTerm);
      const matchSystem = ticket.system.toLowerCase().includes(state.searchTerm);
      if (!matchId && !matchSummary && !matchSystem) return false;
    }

    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="10" class="text-center py-6 text-muted">
          No se encontraron incidencias con los filtros aplicados.
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = filtered.map(t => {
    const isMachine = (t.source === 'machine');

    const sourceTag = isMachine
      ? `<span class="tag tag-machine">🤖 Máquina</span>`
      : `<span class="tag tag-human">👤 Humano</span>`;


    const sevClass = `tag-${t.severity.toLowerCase()}`;
    const levelClass = t.level === 'L2' ? 'level-l2' : 'level-l1';
    const statusClass = t.status === 'resolved' ? 'status-resolved' : (t.level === 'L2' ? 'status-escalated' : 'status-open');

    const createdTime = formatTime(t.created_at);

    let resCol = '<span class="text-muted">En atención</span>';
    if (t.resolved_by_auto) {
      resCol = `<span class="tag tag-machine" title="${t.resolved_by || 'Runbook'}">🤖 Runbook</span>`;
    } else if (t.status === 'resolved') {
      resCol = `<span class="tag tag-human">👨‍💻 Manual</span>`;
    }

    return `
      <tr>
        <td><strong class="modal-id">${t.ticket_id.substring(0, 8)}</strong></td>
        <td>${sourceTag}</td>
        <td>
          <div style="font-weight: 500; max-width: 320px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(t.summary)}">
            ${escapeHtml(t.summary)}
          </div>
        </td>
        <td><span class="tag tag-system">${t.system}</span></td>
        <td><span class="tag ${sevClass}">${t.severity}</span></td>
        <td><span class="badge-level ${levelClass}">${t.level}</span></td>
        <td><span class="status-badge ${statusClass}">${t.status}</span></td>
        <td>${resCol}</td>
        <td><small class="text-muted">${createdTime}</small></td>
        <td>
          <button class="btn btn-secondary btn-sm" onclick="openTicketModal('${t.ticket_id}')">
            Ver
          </button>
        </td>
      </tr>
    `;
  }).join('');
}

// ---------------------------------------------------------------------------
// Renderizado Pestaña Métricas & SLAs
// ---------------------------------------------------------------------------
function renderMetricsTab(metrics) {
  if (!metrics) return;

  // 1. MTTR por Severidad
  const mttr = metrics.mttr_by_severity || {};
  ['p0', 'p1', 'p2', 'p3'].forEach(k => {
    const el = document.getElementById(`mttr-${k}`);
    if (el) {
      const val = mttr[k.toUpperCase()];
      el.textContent = val !== null && val !== undefined ? `${val} min` : '--';
    }
  });

  // 2. Sistemas con incidentes recurrentes
  const recTbody = document.getElementById('recurrent-systems-tbody');
  if (recTbody) {
    const list = metrics.recurrent_systems || [];
    if (list.length === 0) {
      recTbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">Sin incidentes registrados aún.</td></tr>';
    } else {
      recTbody.innerHTML = list.map(item => {
        let riskBadge = '<span class="tag tag-p3">Bajo</span>';
        if (item.p0_p1_count >= 2 || item.total_incidents >= 5) {
          riskBadge = '<span class="tag tag-p0">Alto Riesgo</span>';
        } else if (item.p0_p1_count >= 1 || item.total_incidents >= 2) {
          riskBadge = '<span class="tag tag-p1">Medio</span>';
        }
        return `
          <tr>
            <td><span class="tag tag-system">${item.system}</span></td>
            <td><strong>${item.total_incidents}</strong></td>
            <td><span class="tag ${item.p0_p1_count > 0 ? 'tag-p0' : 'text-muted'}">${item.p0_p1_count}</span></td>
            <td><span class="badge ${item.open_count > 0 ? 'status-open' : 'status-resolved'}">${item.open_count}</span></td>
            <td>${riskBadge}</td>
          </tr>
        `;
      }).join('');
    }
  }

  // 3. Picos de volumen por hora (24 Horas)
  const hourlyContainer = document.getElementById('hourly-bars-container');
  if (hourlyContainer) {
    const hours = metrics.volume_by_hour || {};
    const hourEntries = Object.entries(hours);
    const maxHourly = Math.max(...hourEntries.map(([, v]) => v), 1);
    hourlyContainer.innerHTML = hourEntries.map(([hr, count]) => {
      const heightPct = count > 0 ? Math.max(Math.round((count / maxHourly) * 100), 10) : 4;
      const isPeak = count === maxHourly && count > 0;
      const bg = isPeak ? '#ef4444' : (count > 0 ? '#3b82f6' : '#1e293b');
      return `
        <div style="flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end;" title="${hr}: ${count} incidencias">
          ${count > 0 ? `<span style="font-size: 9px; color: #94a3b8; margin-bottom: 2px;">${count}</span>` : ''}
          <div style="width: 100%; max-width: 14px; height: ${heightPct}%; background-color: ${bg}; border-radius: 3px 3px 0 0; transition: height 0.3s ease;"></div>
        </div>
      `;
    }).join('');
  }

  // 4. Barras de volumen por sistema (si existe el contenedor secundario)
  const container = document.getElementById('system-bars-container');
  if (container) {
    const volume = metrics.volume_by_system || {};
    const entries = Object.entries(volume);
    const maxVal = Math.max(...entries.map(([, val]) => val), 1);

    if (entries.length === 0) {
      container.innerHTML = '<div class="text-muted">Aún no hay datos de volumen.</div>';
    } else {
      container.innerHTML = entries.map(([sys, count]) => {
        const pct = Math.round((count / maxVal) * 100);
        return `
          <div class="sys-bar-row">
            <div class="sys-bar-meta">
              <span><code>${sys.toUpperCase()}</code></span>
              <span><strong>${count}</strong> casos (${Math.round((count / metrics.total_tickets) * 100 || 0)}%)</span>
            </div>
            <div class="sys-bar-track">
              <div class="sys-bar-fill" style="width: ${pct}%;"></div>
            </div>
          </div>
        `;
      }).join('');
    }
  }

  // 5. Tabla de Breaches
  const breachesBody = document.getElementById('sla-breaches-tbody');
  const breaches = metrics.sla_breaches || [];

  if (breaches.length === 0) {
    breachesBody.innerHTML = `
      <tr>
        <td colspan="5" class="text-center text-muted" style="padding: 18px;">
          ✅ Excelente: Todos los tickets están dentro de su SLA objetivo.
        </td>
      </tr>
    `;
  } else {
    breachesBody.innerHTML = breaches.map(b => `
      <tr>
        <td><code>${(b.ticket_id || '').substring(0, 8)}</code></td>
        <td><span class="tag tag-${(b.severity || 'p2').toLowerCase()}">${b.severity}</span></td>
        <td><span class="tag tag-system">${b.system}</span></td>
        <td><strong class="highlight-red">+${b.minutes_overdue} min</strong></td>
        <td><span class="status-badge status-escalated">VENCIDO</span></td>
      </tr>
    `).join('');
  }
}

// ---------------------------------------------------------------------------
// Simulador & Escenarios (1-Clic)
// ---------------------------------------------------------------------------
function setupSimulator() {
  const buttons = document.querySelectorAll('.btn-sim');
  buttons.forEach(btn => {
    btn.addEventListener('click', async () => {
      const scenario = btn.getAttribute('data-scenario');
      btn.disabled = true;
      btn.textContent = 'Procesando...';

      try {
        await executeScenario(scenario);
      } finally {
        btn.disabled = false;
        btn.textContent = btn.getAttribute('data-scenario') === 'run-sla'
          ? '▶ Correr Job de SLA'
          : `▶ Disparar ${scenario.includes('machine') ? 'Alerta Máquina' : (scenario.includes('human') ? 'Reporte Humano' : 'Incidente P0')}`;
      }
    });
  });

  // Formulario Custom
  const form = document.getElementById('form-custom-sim');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const type = document.getElementById('sim-sender-type').value;
    const name = document.getElementById('sim-sender-name').value;
    const space = document.getElementById('sim-space-name').value;
    const text = document.getElementById('sim-message-text').value.trim();

    if (!text) {
      showToast('Por favor escribe un mensaje para simular', 'warning');
      return;
    }

    const payload = {
      type: "MESSAGE",
      eventTime: new Date().toISOString(),
      space: { name: space, type: "ROOM", displayName: "Soporte Fintech" },
      message: {
        name: `${space}/messages/msg-${Date.now()}`,
        sender: { name: `users/${type.toLowerCase()}-custom`, displayName: name, type: type },
        createTime: new Date().toISOString(),
        text: text,
      },
    };

    try {
      const res = await fetch('/webhook/google-chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      const data = await res.json();
      displaySimResult(data, res.ok);
      showToast(`Evento procesado: Ticket #${data.ticket_id.substring(0, 8)}`, 'success');
      fetchAllData();
    } catch (err) {
      showToast('Error enviando evento al webhook', 'error');
    }
  });
}

async function executeScenario(scenario) {
  if (scenario === 'run-sla') {
    const res = await fetch('/sla/run', { method: 'POST' });
    const data = await res.json();
    showToast(`Job SLA completado: ${data.total_checked} revisados, ${data.overdue_escalated + data.stale_escalated} escalados a L2`, 'info');
    fetchAllData();
    return;
  }

  let payload = null;

  if (scenario === 'machine-ingestion') {
    // Alerta de máquina -> Resuelta por Runbook Engine
    payload = {
      type: "MESSAGE",
      eventTime: new Date().toISOString(),
      space: { name: "spaces/FINTECH_SUPPORT", type: "ROOM", displayName: "Soporte Infra" },
      message: {
        name: `spaces/FINTECH_SUPPORT/messages/bot-${Date.now()}`,
        sender: { name: "users/monitoring-bot", displayName: "Monitoring Bot 24/7", type: "BOT" },
        createTime: new Date().toISOString(),
        text: "❌ Falló el pipeline de ingesta de datos — 03:14 AM",
      },
    };
  } else if (scenario === 'human-transactions') {
    // Reporte humano -> Asignado a L1 con SLA 30m
    payload = {
      type: "MESSAGE",
      eventTime: new Date().toISOString(),
      space: { name: "spaces/FINTECH_SUPPORT", type: "ROOM", displayName: "Soporte Finanzas" },
      message: {
        name: `spaces/FINTECH_SUPPORT/messages/human-${Date.now()}`,
        sender: { name: "users/analyst-maria", displayName: "María López (Riesgos)", type: "HUMAN" },
        createTime: new Date().toISOString(),
        text: "Las transacciones del batch nocturno no se procesaron. Llevo 2 horas esperando y nada. Esto afecta el balance contable.",
      },
    };
  } else if (scenario === 'critical-p0') {
    // Incidente P0 -> Escalamiento inmediato a L2
    payload = {
      type: "MESSAGE",
      eventTime: new Date().toISOString(),
      space: { name: "spaces/FINTECH_SUPPORT", type: "ROOM", displayName: "War Room" },
      message: {
        name: `spaces/FINTECH_SUPPORT/messages/lead-${Date.now()}`,
        sender: { name: "users/lead-oncall", displayName: "Lead On-Call", type: "HUMAN" },
        createTime: new Date().toISOString(),
        text: "ALERTA P0: Caída total en pasarela de pagos con balance afectado y transacciones rechazadas masivamente.",
      },
    };
  }

  if (payload) {
    const res = await fetch('/webhook/google-chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    displaySimResult(data, res.ok);

    if (data.resolved_automatically) {
      showToast(`🤖 ¡Auto-Resuelto por ${data.runbook_used}!`, 'success');
    } else if (data.level === 'L2') {
      showToast(`🚨 Incidente P0 escalado de inmediato a Nivel 2`, 'warning');
    } else {
      showToast(`✅ Ticket #${data.ticket_id.substring(0, 8)} asignado a Nivel 1`, 'success');
    }

    fetchAllData();
  }
}

function displaySimResult(data, isSuccess) {
  const box = document.getElementById('sim-result-box');
  const badge = document.getElementById('sim-result-badge');
  const jsonPre = document.getElementById('sim-result-json');

  box.classList.remove('hidden');
  badge.textContent = isSuccess ? (data.resolved_automatically ? 'AUTO-RESUELTO' : 'TICKET CREADO') : 'ERROR';
  badge.className = `badge ${isSuccess ? (data.resolved_automatically ? 'tag-machine' : 'tag-human') : 'tag-p0'}`;
  jsonPre.textContent = JSON.stringify(data, null, 2);
}

// ---------------------------------------------------------------------------
// Modal de Detalle de Ticket y Seguimiento
// ---------------------------------------------------------------------------
let activeModalTicketId = null;

function setupModal() {
  const modal = document.getElementById('ticket-modal');
  const closeBtn = document.getElementById('modal-close');

  closeBtn.addEventListener('click', () => {
    modal.classList.add('hidden');
    activeModalTicketId = null;
  });
  modal.addEventListener('click', (e) => {
    if (e.target === modal) {
      modal.classList.add('hidden');
      activeModalTicketId = null;
    }
  });

  // Listener para agregar nuevo comentario
  const submitBtn = document.getElementById('btn-submit-comment');
  if (submitBtn) {
    submitBtn.addEventListener('click', async () => {
      if (!activeModalTicketId) return;

      const authorInput = document.getElementById('modal-comment-author');
      const statusSelect = document.getElementById('modal-comment-status');
      const commentInput = document.getElementById('modal-comment-text');

      const author = (authorInput ? authorInput.value.trim() : '') || 'Agente Mesa TI';
      const status = statusSelect && statusSelect.value ? statusSelect.value : null;
      const text = commentInput ? commentInput.value.trim() : '';

      if (!text) {
        showToast('Por favor escribe un comentario para guardar.', 'warning');
        return;
      }

      submitBtn.disabled = true;
      submitBtn.textContent = 'Enviando...';

      try {
        const res = await fetch(`/tickets/${activeModalTicketId}/comments`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            author: author,
            comment: text,
            new_status: status,
          }),
        });

        if (!res.ok) {
          const errData = await res.json();
          throw new Error(errData.detail || 'Error al guardar comentario');
        }

        const result = await res.json();
        commentInput.value = '';
        showToast('✅ Comentario registrado y notificado a Google Chat', 'success');

        // Actualizar UI del modal
        document.getElementById('modal-status').textContent = result.new_status.toUpperCase();
        document.getElementById('modal-status').className = `badge status-${result.new_status}`;
        renderModalComments(result.comments);

        // Refrescar datos de fondo
        fetchAllData(true);
      } catch (err) {
        showToast(`Error: ${err.message}`, 'error');
      } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<span>💬</span> Guardar Comentario & Notificar al Chat';
      }
    });
  }
}

function renderModalComments(comments) {
  const container = document.getElementById('modal-comments-timeline');
  const countBadge = document.getElementById('modal-comments-count');
  if (!container) return;

  const list = comments || [];
  if (countBadge) countBadge.textContent = `${list.length} ${list.length === 1 ? 'nota' : 'notas'}`;

  if (list.length === 0) {
    container.innerHTML = '<div class="text-muted" style="font-size: 13px;">Sin notas de seguimiento registradas.</div>';
    return;
  }

  container.innerHTML = list.map(c => {
    const timeStr = c.created_at ? formatTime(c.created_at) : '';
    const statusTag = c.new_status ? `<span class="badge status-${c.new_status}" style="font-size: 10px; margin-left: 6px;">➜ ${c.new_status.toUpperCase()}</span>` : '';
    return `
      <div class="comment-item" style="background: rgba(30, 41, 59, 0.7); border-left: 3px solid #3b82f6; border-radius: 6px; padding: 8px 12px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
          <div>
            <strong style="font-size: 12px; color: #f8fafc;">${escapeHtml(c.author)}</strong>
            ${statusTag}
          </div>
          <small class="text-muted" style="font-size: 11px;">${timeStr}</small>
        </div>
        <div style="font-size: 13px; color: #cbd5e1; white-space: pre-wrap; line-height: 1.4;">${escapeHtml(c.content)}</div>
      </div>
    `;
  }).join('');
}

window.openTicketModal = async function(ticketId) {
  activeModalTicketId = ticketId;
  const cachedTicket = state.tickets.find(t => t.ticket_id === ticketId);
  if (!cachedTicket) return;

  // Llenar datos inmediatos con caché
  fillModalFields(cachedTicket);

  document.getElementById('ticket-modal').classList.remove('hidden');

  // Fetch fresco para asegurar los comentarios más recientes
  try {
    const res = await fetch(`/tickets/${ticketId}`);
    if (res.ok) {
      const freshTicket = await res.json();
      fillModalFields(freshTicket);
    }
  } catch (e) {
    // Si falla el fetch fresco, nos quedamos con el cached
  }
};

function fillModalFields(ticket) {
  document.getElementById('modal-ticket-id').textContent = `TICKET #${ticket.ticket_id.substring(0, 8)}`;
  document.getElementById('modal-ticket-summary').textContent = ticket.summary;

  document.getElementById('modal-status').textContent = ticket.status.toUpperCase();
  document.getElementById('modal-status').className = `badge status-${ticket.status}`;

  document.getElementById('modal-severity').textContent = ticket.severity;
  document.getElementById('modal-severity').className = `badge tag-${ticket.severity.toLowerCase()}`;

  document.getElementById('modal-level').textContent = ticket.level;
  document.getElementById('modal-level').className = `badge ${ticket.level === 'L2' ? 'level-l2' : 'level-l1'}`;

  document.getElementById('modal-system').textContent = ticket.system.toUpperCase();

  document.getElementById('modal-sla-deadline').textContent = ticket.sla_deadline ? formatFullDate(ticket.sla_deadline) : 'N/A';
  document.getElementById('modal-created-at').textContent = formatFullDate(ticket.created_at);
  const reqEl = document.getElementById('modal-requester');
  if (reqEl) {
    reqEl.textContent = ticket.requester_id || 'users/desconocido';
  }
  document.getElementById('modal-space').textContent = ticket.space_id || 'spaces/FINTECH_SUPPORT';
  document.getElementById('modal-auto-res').textContent = ticket.resolved_by_auto ? `Sí (${ticket.resolved_by || 'Runbook'})` : 'No';
  document.getElementById('modal-resolved-by').textContent = ticket.resolved_by || 'En progreso';

  // Renderizar comentarios
  renderModalComments(ticket.comments);

  // Mensaje de simulación de Google Chat
  const autoTag = ticket.resolved_by_auto ? ' 🤖 (resuelto automáticamente)' : '';
  const chatMsg = `🎫 *Ticket #${ticket.ticket_id.substring(0, 8)}*${autoTag}
• Severidad: *${ticket.severity}*
• Sistema: ${ticket.system}
• Nivel Asignado: ${ticket.level}
• Estado: ${ticket.status.toUpperCase()}
• Resumen: ${ticket.summary}`;

  document.getElementById('modal-chat-message').textContent = chatMsg;
}

// ---------------------------------------------------------------------------
// Acciones Globales
// ---------------------------------------------------------------------------
function setupGlobalActions() {
  document.getElementById('btn-refresh').addEventListener('click', () => {
    fetchAllData();
    showToast('Datos actualizados', 'info');
  });

  document.getElementById('btn-eval-sla').addEventListener('click', async () => {
    const res = await fetch('/sla/run', { method: 'POST' });
    const data = await res.json();
    showToast(`SLA Check: ${data.total_checked} tickets evaluados`, 'info');
    fetchAllData();
  });

  document.getElementById('chk-auto-refresh').addEventListener('change', (e) => {
    state.autoRefresh = e.target.checked;
    showToast(state.autoRefresh ? 'Auto-refresco activado (5s)' : 'Auto-refresco pausado', 'info');
  });
}

// ---------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------
function formatTime(isoStr) {
  if (!isoStr) return '--';
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return isoStr;
  }
}

function formatFullDate(isoStr) {
  if (!isoStr) return '--';
  try {
    const d = new Date(isoStr);
    return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  } catch {
    return isoStr;
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `<span>${message}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(8px)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// ---------------------------------------------------------------------------
// Consola de Logs en Vivo
// ---------------------------------------------------------------------------
function renderLogs(logs) {
  const consoleEl = document.getElementById('logs-console');
  if (!consoleEl) return;

  if (!logs || logs.length === 0) {
    consoleEl.innerHTML = '<div class="text-muted">Aún no hay eventos registrados en el log.</div>';
    return;
  }

  consoleEl.innerHTML = logs.map(l => {
    let color = '#38bdf8'; // info cyan
    if (l.level === 'SUCCESS') color = '#4ade80'; // green
    if (l.level === 'WARNING') color = '#fbbf24'; // yellow
    if (l.level === 'ERROR') color = '#f87171'; // red

    const detailsStr = l.details ? ` <span style="color: #94a3b8;">${escapeHtml(typeof l.details === 'object' ? JSON.stringify(l.details) : String(l.details))}</span>` : '';
    return `<div style="margin-bottom: 4px; border-bottom: 1px solid rgba(255,255,255,0.05); padding-bottom: 2px;">
      <span style="color: #64748b;">[${l.time_str} UTC]</span>
      <strong style="color: ${color}; padding: 1px 4px; border-radius: 3px; font-size: 11px;">[${l.category}]</strong>
      <span style="color: #f1f5f9;">${escapeHtml(l.message)}</span>
      ${detailsStr}
    </div>`;
  }).join('');
}

function setupLogActions() {
  const clearBtn = document.getElementById('btn-clear-logs');
  if (clearBtn) {
    clearBtn.addEventListener('click', async () => {
      await fetch('/api/logs', { method: 'DELETE' });
      const consoleEl = document.getElementById('logs-console');
      if (consoleEl) consoleEl.innerHTML = '<div class="text-muted">Logs limpiados.</div>';
      showToast('Logs limpiados', 'info');
    });
  }

  const refreshBtn = document.getElementById('btn-refresh-logs');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      const res = await fetch('/api/logs?limit=100');
      if (res.ok) {
        renderLogs(await res.json());
        showToast('Logs actualizados', 'info');
      }
    });
  }
}

