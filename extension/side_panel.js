/**
 * Flow Agent — Side Panel
 * Displays live connection status, metrics, and request log.
 */

// ── Type label map ───────────────────────────────────────────

const TYPE_LABELS = {
  // Worker request types
  GENERATE_IMAGE:           'GEN IMAGE',
  REGENERATE_IMAGE:         'REGEN IMAGE',
  EDIT_IMAGE:               'EDIT IMAGE',
  GENERATE_CHARACTER_IMAGE: 'GEN REF',
  REGENERATE_CHARACTER_IMAGE: 'REGEN REF',
  EDIT_CHARACTER_IMAGE:     'EDIT REF',
  GENERATE_VIDEO:           'GEN VIDEO',
  GENERATE_VIDEO_REFS:      'GEN VIDEO FROM REFS',
  UPSCALE_VIDEO:            'UPSCALE VIDEO',
  // Captcha action types
  IMAGE_GENERATION:         'GEN IMAGE',
  VIDEO_GENERATION:         'GEN VIDEO',
  // Extension-classified API types
  GEN_IMG:                  'GEN IMAGE',
  GEN_VID:                  'GEN VIDEO',
  GEN_VID_REF:              'GEN VIDEO FROM REFS',
  UPSCALE:                  'UPSCALE VIDEO',
  UPS_IMG:                  'UPSCALE IMAGE',
  POLL:                     'CHECK GEN VIDEO',
  CREDITS:                  'CHECK CREDIT',
  CREATE_PROJECT:           'CREATE PROJECT',
  UPLOAD:                   'UPLOAD IMAGE',
  MEDIA:                    'READ MEDIA',
  TRACKING:                 'GOOGLE FLOW TRACK',
  URL_REFRESH:              'URL REFRESH',
  TRPC:                     'TRPC',
  API:                      'API',
};

function formatType(type) {
  if (!type) return '—';
  return TYPE_LABELS[type] || type.slice(0, 5).toUpperCase();
}

// ── Toast notifications ──────────────────────────────────────

function toast(message, kind = 'info', duration = 2600) {
  const wrap = document.getElementById('toast-wrap');
  if (!wrap) return;
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => {
    el.classList.add('hide');
    setTimeout(() => el.remove(), 220);
  }, duration);
}

// ── Runtime message wrapper (unwraps chrome errors) ──────────

function sendMessage(payload) {
  return new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendMessage(payload, (res) => {
        const err = chrome.runtime.lastError;
        if (err) return reject(new Error(err.message || 'Extension not responding'));
        if (res && res.error) return reject(new Error(res.error));
        resolve(res || {});
      });
    } catch (e) {
      reject(e);
    }
  });
}

// ── Time formatting ──────────────────────────────────────────

function formatTime(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    return `${hh}:${mm}:${ss}`;
  } catch {
    return '—';
  }
}

// ── Status update ────────────────────────────────────────────

let _status = null;
let _statusAt = 0;

function updateStatus(data) {
  if (!data) return;
  _status = data;
  _statusAt = Date.now();

  const connected = data.agentConnected;

  // Connection dot
  const dot = document.getElementById('conn-dot');
  dot.className = connected ? 'on' : '';

  // Toggle state
  const toggle = document.getElementById('main-toggle');
  const toggleLabel = document.getElementById('toggle-label');
  const isOn = data.state !== 'off';
  toggle.checked = isOn;
  toggleLabel.textContent = isOn ? 'ON' : 'OFF';

  // Activity strip — display state
  // disconnected → off; connected-but-off → idle (alive/watching); else raw state
  let displayState;
  if (connected === false) {
    displayState = 'off';
  } else if (data.state === 'running') {
    displayState = 'running';
  } else {
    displayState = 'idle';
  }
  const activity = document.getElementById('activity');
  const activityState = document.getElementById('activity-state');
  if (activity) activity.dataset.state = displayState;
  if (activityState) {
    activityState.textContent =
      displayState === 'running' ? 'Working' :
      displayState === 'idle' ? 'Watching' : 'Off';
  }

  // Token status
  const tokenEl = document.getElementById('token-status');
  if (data.flowKeyPresent) {
    const ageMs = data.tokenAge || 0;
    const ageMin = Math.round(ageMs / 60000);
    if (ageMs > 3600000) {
      tokenEl.textContent = `token expired — open Flow to refresh`;
      tokenEl.className = 'warn';
    } else {
      tokenEl.textContent = `token synced ${ageMin}m`;
      tokenEl.className = 'ok';
    }
    // Auto-refresh when token age > 55 min and connected
    if (ageMs > 3300000 && data.agentConnected) {
      chrome.runtime.sendMessage({ type: 'REFRESH_TOKEN' });
    }
  } else {
    tokenEl.textContent = 'no token';
    tokenEl.className = 'bad';
  }

  // Metrics
  const m = data.metrics || {};
  setMetric('m-total', m.requestCount || 0);
  setMetric('m-success', m.successCount || 0);
  setMetric('m-failed', m.failedCount || 0);
}

// ── Metric count-up + bump ───────────────────────────────────

function setMetric(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  const current = parseInt(el.textContent, 10) || 0;
  if (current === value) return;

  const start = current;
  const delta = value - start;
  const duration = 400;
  const t0 = performance.now();

  function step(now) {
    const p = Math.min(1, (now - t0) / duration);
    el.textContent = Math.round(start + delta * p);
    if (p < 1) {
      requestAnimationFrame(step);
    } else {
      el.textContent = value;
      el.classList.add('bump');
      setTimeout(() => el.classList.remove('bump'), 200);
    }
  }
  requestAnimationFrame(step);
}

// ── Live tickers (guarantee something moves each second) ─────

function renderTicker() {
  const ticker = document.getElementById('activity-ticker');
  if (!ticker) return;
  const state = document.getElementById('activity')?.dataset.state;

  if (state === 'running') {
    const dots = '.'.repeat((Math.floor(performance.now() / 400) % 3) + 1);
    ticker.textContent = `working${dots}`;
    return;
  }
  if (state === 'off') {
    ticker.textContent = '';
    return;
  }
  // idle → last activity from newest log entry
  const newest = _logEntries[0];
  const iso = newest && (newest.time || newest.timestamp || newest.createdAt);
  const ts = iso ? Date.parse(iso) : NaN;
  if (!isNaN(ts)) {
    ticker.textContent = `last: ${agoLabel(Date.now() - ts)}`;
  } else {
    ticker.textContent = 'watching';
  }
}

function agoLabel(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ago`;
}

function renderTokenTicker() {
  if (!_status || !_status.flowKeyPresent) return;
  const tokenEl = document.getElementById('token-status');
  if (!tokenEl || tokenEl.className === 'warn') return;
  // tokenAge is a snapshot; advance it live using time since last status
  const baseAge = _status.tokenAge || 0;
  const elapsed = _statusAt ? (Date.now() - _statusAt) : 0;
  const ageMs = baseAge + elapsed;
  if (ageMs > 3600000) {
    tokenEl.textContent = 'token expired — open Flow to refresh';
    tokenEl.className = 'warn';
  } else {
    tokenEl.textContent = `token synced ${Math.round(ageMs / 60000)}m`;
  }
}

function startTickers() {
  setInterval(() => {
    renderTicker();
    renderTokenTicker();
  }, 1000);
}

// ── Request log ──────────────────────────────────────────────

function updateRequestLog(entries) {
  const tbody = document.getElementById('log-body');
  const countEl = document.getElementById('log-count');
  const liveEl = document.getElementById('live-count');

  if (!entries || entries.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="log-empty">No requests yet</td></tr>';
    if (countEl) countEl.textContent = '0';
    if (liveEl) liveEl.hidden = true;
    _logEntries = [];
    return;
  }

  if (countEl) countEl.textContent = entries.length;
  _logEntries = entries;

  let liveCount = 0;

  // Render newest first (entries already sorted DESC by background.js)
  const rows = entries.map((entry) => {
    const shortId = entry.id ? String(entry.id).slice(0, 8) : '—';
    const type   = formatType(entry.type || entry.method);
    const time   = formatTime(entry.time || entry.timestamp || entry.createdAt);
    const status = entry.status || entry.state || 'pending';
    const error  = entry.error || '';

    const done = status === 'COMPLETED' || status === 'success';
    const failed = status === 'FAILED' || status === 'failed' || (typeof status === 'number' && status >= 400);
    const inflight = !done && !failed;
    if (inflight) liveCount++;

    let badgeHtml;
    if (done) {
      badgeHtml = '<span class="badge badge-ok">&#10003; done</span>';
    } else if (failed) {
      badgeHtml = '<span class="badge badge-fail">&#10007; fail</span>';
    } else if (status === 'PROCESSING') {
      badgeHtml = '<span class="badge badge-proc">&#9203; gen...</span>';
    } else {
      badgeHtml = '<span class="badge badge-proc">&#9203; sent</span>';
    }

    const errorDisplay = error
      ? `<td class="td-error" title="${escHtml(error)}">${escHtml(truncate(error, 28))}</td>`
      : `<td class="td-error empty">—</td>`;

    return `<tr class="${inflight ? 'inflight' : ''}">
      <td class="td-id" data-request-id="${escHtml(entry.id || '')}">${escHtml(shortId)}</td>
      <td class="td-type">${escHtml(type)}</td>
      <td class="td-time">${escHtml(time)}</td>
      <td>${badgeHtml}</td>
      ${errorDisplay}
    </tr>`;
  });

  tbody.innerHTML = rows.join('');

  if (liveEl) {
    if (liveCount > 0) {
      liveEl.textContent = `${liveCount} live`;
      liveEl.hidden = false;
    } else {
      liveEl.hidden = true;
    }
  }

  // Attach click handlers to ID cells
  tbody.querySelectorAll('.td-id[data-request-id]').forEach(td => {
    td.addEventListener('click', () => {
      const reqId = td.getAttribute('data-request-id');
      if (reqId) showRequestDetail(reqId);
    });
  });
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function truncate(str, len) {
  if (!str || str.length <= len) return str;
  return str.slice(0, len) + '…';
}

// ── Request detail modal ────────────────────────────────────

let _logEntries = [];

function showRequestDetail(reqId) {
  const entry = _logEntries.find(e => e.id === reqId);
  if (!entry) return;

  const overlay = document.getElementById('detail-overlay');
  const title = document.getElementById('detail-title');
  const body = document.getElementById('detail-body');

  title.textContent = `Request ${String(reqId).slice(0, 12)}`;

  const fields = [
    ['ID', entry.id],
    ['Type', formatType(entry.type || entry.method)],
    ['Time', formatTime(entry.time || entry.timestamp || entry.createdAt)],
    ['Status', entry.status || entry.state || 'pending'],
    ['HTTP', entry.httpStatus || '—'],
    ['URL', entry.url || '—'],
    ['Payload', entry.payloadSummary || '—'],
    ['Response', entry.responseSummary || '—'],
    ['Error', entry.error || '—'],
  ];

  body.innerHTML = fields.map(([label, value]) => {
    let cls = 'detail-value';
    if (label === 'Error' && value && value !== '—') cls += ' error';
    if (label === 'Status' && (value === 'COMPLETED' || value === 'success')) cls += ' ok';
    return `<div class="detail-row">
      <div class="detail-label">${escHtml(label)}</div>
      <div class="${cls}">${escHtml(String(value || '—'))}</div>
    </div>`;
  }).join('');

  overlay.classList.add('open');
}

document.getElementById('detail-close').addEventListener('click', () => {
  document.getElementById('detail-overlay').classList.remove('open');
});

document.getElementById('detail-overlay').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) {
    e.currentTarget.classList.remove('open');
  }
});

// ── Initial data fetch ───────────────────────────────────────

function fetchStatus() {
  chrome.runtime.sendMessage({ type: 'STATUS' }, (data) => {
    if (chrome.runtime.lastError) return;
    updateStatus(data);
  });
}

function fetchLog() {
  chrome.runtime.sendMessage({ type: 'REQUEST_LOG' }, (data) => {
    if (chrome.runtime.lastError) return;
    if (data && data.log) updateRequestLog(data.log);
  });
}

// ── Message listener (push updates) ─────────────────────────

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'STATUS_PUSH') {
    fetchStatus();
  }
  if (msg.type === 'REQUEST_LOG_UPDATE') {
    if (msg.log) updateRequestLog(msg.log);
  }
});

// ── Toggle (connect / disconnect) ───────────────────────────

document.getElementById('main-toggle').addEventListener('change', async (e) => {
  const on = e.target.checked;
  try {
    await sendMessage({ type: on ? 'RECONNECT' : 'DISCONNECT' });
    toast(on ? 'Agent connecting…' : 'Agent disconnected', on ? 'ok' : 'info');
    setTimeout(fetchStatus, 400);
  } catch (err) {
    e.target.checked = !on; // revert visual state
    toast(`Toggle failed: ${err.message}`, 'err');
  }
});

// ── Action buttons ───────────────────────────────────────────

document.getElementById('btn-flow').addEventListener('click', async () => {
  const btn = document.getElementById('btn-flow');
  btn.disabled = true;
  try {
    await sendMessage({ type: 'OPEN_FLOW_TAB' });
    toast('Flow tab opened', 'ok');
  } catch (err) {
    toast(`Could not open Flow: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('btn-token').addEventListener('click', async () => {
  const btn = document.getElementById('btn-token');
  const label = btn.textContent;
  btn.textContent = 'Refreshing…';
  btn.disabled = true;
  try {
    await sendMessage({ type: 'REFRESH_TOKEN' });
    toast('Token refresh triggered', 'ok');
    setTimeout(fetchStatus, 600);
  } catch (err) {
    toast(`Refresh failed: ${err.message}`, 'err');
  } finally {
    btn.textContent = label;
    btn.disabled = false;
  }
});

// ── Init ─────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  fetchStatus();
  fetchLog();
  startTickers();
});
