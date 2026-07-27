const TYPE_LABELS = {
  GENERATE_IMAGE:           'GEN IMAGE',
  REGENERATE_IMAGE:         'REGEN IMAGE',
  EDIT_IMAGE:               'EDIT IMAGE',
  GENERATE_CHARACTER_IMAGE: 'GEN REF',
  REGENERATE_CHARACTER_IMAGE: 'REGEN REF',
  EDIT_CHARACTER_IMAGE:     'EDIT REF',
  GENERATE_VIDEO:           'GEN VIDEO',
  GENERATE_VIDEO_REFS:      'GEN VIDEO FROM REFS',
  UPSCALE_VIDEO:            'UPSCALE VIDEO',
  GEN_IMG:                  'GEN IMAGE',
  GEN_VID:                  'GEN VIDEO',
  GEN_VID_REF:              'GEN VIDEO FROM REFS',
  UPSCALE:                  'UPSCALE VIDEO',
  TRACKING:                 'TRACKING',
  URL_REFRESH:              'URL REFRESH',
};

function formatType(type) {
  if (!type) return '—';
  return TYPE_LABELS[type] || type.slice(0, 12).toUpperCase();
}

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

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function badgeHtml(status) {
  if (status === 'COMPLETED' || status === 'success') {
    return '<span class="badge badge-ok">&#10003; done</span>';
  } else if (status === 'FAILED' || status === 'failed' || (typeof status === 'number' && status >= 400)) {
    return '<span class="badge badge-fail">&#10007; fail</span>';
  } else if (status === 'PROCESSING') {
    return '<span class="badge badge-proc">&#9203; gen...</span>';
  } else {
    return '<span class="badge badge-proc">&#9203; sent</span>';
  }
}

function renderLog(entries) {
  const list = document.getElementById('log-list');
  const countEl = document.getElementById('log-count');

  if (!entries || entries.length === 0) {
    list.innerHTML = '<div class="log-empty">No requests yet</div>';
    countEl.textContent = '0';
    return;
  }

  countEl.textContent = entries.length;

  list.innerHTML = entries.map((entry, i) => {
    const shortId = entry.id ? String(entry.id).slice(0, 8) : '—';
    const type = formatType(entry.type || entry.method);
    const time = formatTime(entry.time || entry.timestamp);
    const status = entry.status || 'pending';
    const error = entry.error || '';

    const urlDisplay = entry.url
      ? `<div class="detail-section">
           <div class="detail-label">URL</div>
           <div class="detail-value url" title="${escHtml(entry.url)}">${escHtml(entry.url)}</div>
         </div>`
      : '';

    const payloadDisplay = entry.payloadSummary
      ? `<div class="detail-section">
           <div class="detail-label">Payload</div>
           <div class="detail-value">${escHtml(entry.payloadSummary)}</div>
         </div>`
      : '';

    const responseDisplay = entry.responseSummary
      ? `<div class="detail-section">
           <div class="detail-label">Response${entry.httpStatus ? ` (${entry.httpStatus})` : ''}</div>
           <div class="detail-value">${escHtml(entry.responseSummary)}</div>
         </div>`
      : '';

    const errorDisplay = error
      ? `<div class="detail-section">
           <div class="detail-label">Error</div>
           <div class="detail-value detail-error">${escHtml(error)}</div>
         </div>`
      : '';

    const hasDetails = entry.url || entry.payloadSummary || entry.responseSummary || error;

    return `<div class="entry" data-idx="${i}">
      <div class="entry-row">
        <span class="entry-id">${escHtml(shortId)}</span>
        <span class="entry-type">${escHtml(type)}</span>
        <span class="entry-time">${escHtml(time)}</span>
        ${badgeHtml(status)}
        ${hasDetails ? '<span class="expand-icon">&#9654;</span>' : '<span class="expand-icon" style="visibility:hidden">&#9654;</span>'}
      </div>
      ${hasDetails ? `<div class="entry-details">${urlDisplay}${payloadDisplay}${responseDisplay}${errorDisplay}</div>` : ''}
    </div>`;
  }).join('');

  // Toggle expand on row click
  list.querySelectorAll('.entry-row').forEach((row) => {
    row.addEventListener('click', () => {
      const entry = row.closest('.entry');
      if (entry.querySelector('.entry-details')) {
        entry.classList.toggle('open');
      }
    });
  });
}

function selectTab(name) {
  document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach((panel) => panel.classList.toggle('active', panel.id === `panel-${name}`));
  if (name === 'media') loadMedia();
}

async function loadMedia() {
  const grid = document.getElementById('media-grid');
  grid.innerHTML = '<div class="media-empty">Loading media…</div>';
  try {
    const [data, base] = await Promise.all([apiJson('/v1/history'), getBackendBase()]);
    const items = Array.isArray(data.history) ? data.history : [];
    if (!items.length) {
      grid.innerHTML = '<div class="media-empty">No generated media yet</div>';
      return;
    }
    grid.innerHTML = items.map((item) => {
      const rawUrl = String(item.url || '');
      const url = escHtml(rawUrl.replace(/^http:\/\/(localhost|127\.0\.0\.1):8001/i, base));
      const prompt = escHtml(item.prompt || 'Generated media');
      const preview = item.type === 'video'
        ? `<video src="${url}" controls preload="metadata"></video>`
        : `<img src="${url}" alt="${prompt}" loading="lazy">`;
      const date = item.timestamp ? new Date(item.timestamp * 1000).toLocaleDateString() : '';
      return `<article class="media-item"><a href="${url}" target="_blank" rel="noreferrer">${preview}</a><div class="media-info"><div class="media-copy"><strong>Generation ready</strong><span title="${prompt}">${prompt} · ${escHtml(date)}</span></div><a class="media-view" href="${url}" target="_blank" rel="noreferrer">View</a></div></article>`;
    }).join('');
    grid.querySelectorAll('img,video').forEach((media) => {
      media.addEventListener('error', () => media.closest('.media-item')?.remove(), { once: true });
    });
  } catch (error) {
    grid.innerHTML = `<div class="media-empty">Could not load media: ${escHtml(error.message)}</div>`;
  }
}

let monitorEnabled = false;
function runtimeMessage(payload) {
  return new Promise((resolve, reject) => chrome.runtime.sendMessage(payload, (response) => {
    if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
    else if (response?.error) reject(new Error(response.error));
    else resolve(response || {});
  }));
}

let toastTimer;
function showToast(message, kind = 'ok') {
  const toast = document.getElementById('toast');
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = `show ${kind}`;
  toastTimer = setTimeout(() => { toast.className = ''; }, 2600);
}

async function refreshMonitor() {
  try {
    const status = await runtimeMessage({ type: 'STATUS' });
    monitorEnabled = status.state !== 'off';
    const connected = !!status.agentConnected;
    document.getElementById('monitor-dot').classList.toggle('on', connected);
    document.getElementById('monitor-state').textContent = !connected ? 'Disconnected' : status.state === 'running' ? 'Generating…' : 'Ready';
    const toggle = document.getElementById('agent-toggle');
    toggle.textContent = monitorEnabled ? 'ON' : 'OFF';
    toggle.classList.toggle('on', monitorEnabled);
    document.getElementById('metric-total').textContent = status.metrics?.requestCount || 0;
    document.getElementById('metric-success').textContent = status.metrics?.successCount || 0;
    document.getElementById('metric-failed').textContent = status.metrics?.failedCount || 0;
    const age = status.tokenAge == null ? null : Math.round(status.tokenAge / 60000);
    document.getElementById('monitor-token').textContent = status.flowKeyPresent ? `Token ${age || 0}m` : 'No token';
    document.getElementById('monitor-client').textContent = (status.clientId || '—').replace(/^client-/, '');
  } catch {
    document.getElementById('monitor-state').textContent = 'Extension offline';
  }
}

document.querySelectorAll('.tab').forEach((tab) => tab.addEventListener('click', () => selectTab(tab.dataset.tab)));
document.getElementById('agent-toggle').addEventListener('click', async () => {
  await runtimeMessage({ type: monitorEnabled ? 'DISCONNECT' : 'RECONNECT' });
  setTimeout(refreshMonitor, 350);
});
document.getElementById('open-flow').addEventListener('click', () => runtimeMessage({ type: 'OPEN_FLOW_TAB' }));
document.getElementById('refresh-token').addEventListener('click', async () => {
  const button = document.getElementById('refresh-token');
  button.textContent = 'Refreshing…';
  try {
    await runtimeMessage({ type: 'REFRESH_TOKEN' });
    showToast('Flow token refreshed successfully');
  } catch (error) {
    showToast(`Token refresh failed: ${error.message}`, 'error');
  } finally {
    button.textContent = 'Refresh Token';
    setTimeout(refreshMonitor, 500);
  }
});
chrome.storage.local.get(['customServerIp', 'clientId'], (data) => {
  document.getElementById('setting-server').value = data.customServerIp || CONFIG.DEFAULT_SERVER_HOST;
  document.getElementById('setting-client').value = data.clientId || '';
});

document.getElementById('save-settings').addEventListener('click', async () => {
  await chrome.storage.local.set({
    customServerIp: document.getElementById('setting-server').value.trim(),
    clientId: document.getElementById('setting-client').value.trim(),
  });
  chrome.runtime.sendMessage({ type: 'SETTINGS_UPDATED' });
  selectTab('create');
  refreshQuickStatus();
});

document.getElementById('clear-history').addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'CLEAR_REQUEST_LOG' }, () => renderLog([]));
});
document.getElementById('refresh-media').addEventListener('click', loadMedia);

chrome.runtime.sendMessage({ type: 'REQUEST_LOG' }, (data) => {
  if (chrome.runtime.lastError) return;
  if (data && data.log) renderLog(data.log);
});

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === 'REQUEST_LOG_UPDATE' && message.log) renderLog(message.log);
});

// ── Quick generation ────────────────────────────────────────

function getBackendBase() {
  return new Promise((resolve) => {
    chrome.storage.local.get(['customServerIp'], (data) => {
      const raw = String(data.customServerIp || CONFIG.DEFAULT_SERVER_HOST).trim().replace(/\/$/, '');
      if (/^https?:\/\//i.test(raw)) return resolve(raw);
      const local = /^(localhost|127\.0\.0\.1)(:|$)/i.test(raw);
      resolve(`${local ? 'http' : 'https'}://${raw}`);
    });
  });
}

async function apiJson(path, options = {}) {
  const base = await getBackendBase();
  const stored = await chrome.storage.local.get(['clientId']);
  const response = await fetch(`${base}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(stored.clientId ? { 'X-Client-Id': stored.clientId } : {}),
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!response.ok) {
    const detail = data.detail || data.error || `HTTP ${response.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

async function refreshQuickStatus() {
  const credits = document.getElementById('quick-credits');
  try {
    let creditData;
    try {
      creditData = await runtimeMessage({ type: 'GET_CLIENT_CREDITS' });
    } catch (backgroundError) {
      console.warn('[Flow Agent] Background credits failed, trying direct API:', backgroundError);
      creditData = await apiJson('/v1/credits');
    }
    const balance = creditData.data?.credits ?? creditData.credits ?? '—';
    credits.textContent = String(balance);
    credits.title = `Current client credits: ${balance}`;
  } catch (error) {
    credits.textContent = 'ERR';
    credits.title = error.message;
    console.error('[Flow Agent] Credits unavailable:', error);
  }
}

function updateQuickFields() {
  const isVideo = document.getElementById('quick-type').value === 'video';
  const aspect = document.getElementById('quick-aspect');
  document.getElementById('model-field').hidden = isVideo;
  document.getElementById('duration-field').hidden = !isVideo;
  const square = document.querySelector('[data-input="quick-aspect"] [data-value="square"]');
  square.disabled = isVideo;
  if (isVideo && aspect.value === 'square') {
    aspect.value = 'landscape';
    const widget = document.querySelector('[data-input="quick-aspect"]');
    widget.querySelector('.select-trigger').textContent = 'Landscape';
    widget.querySelectorAll('.select-choice').forEach((choice) => choice.classList.toggle('selected', choice.dataset.value === 'landscape'));
  }
}

document.querySelectorAll('.custom-select').forEach((widget) => {
  const trigger = widget.querySelector('.select-trigger');
  trigger.addEventListener('click', () => {
    document.querySelectorAll('.custom-select').forEach((item) => item !== widget && item.classList.remove('open'));
    widget.classList.toggle('open');
  });
  widget.querySelectorAll('.select-choice').forEach((choice) => choice.addEventListener('click', () => {
    document.getElementById(widget.dataset.input).value = choice.dataset.value;
    trigger.textContent = choice.textContent;
    widget.querySelectorAll('.select-choice').forEach((item) => item.classList.toggle('selected', item === choice));
    widget.classList.remove('open');
  }));
});
document.addEventListener('click', (event) => {
  if (!event.target.closest('.custom-select')) document.querySelectorAll('.custom-select').forEach((item) => item.classList.remove('open'));
});

document.querySelectorAll('.type-option').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.type-option').forEach((item) => item.classList.toggle('active', item === button));
    document.getElementById('quick-type').value = button.dataset.type;
    updateQuickFields();
  });
});

document.getElementById('quick-generate').addEventListener('click', async () => {
  const button = document.getElementById('quick-generate');
  const result = document.getElementById('quick-result');
  const prompt = document.getElementById('quick-prompt').value.trim();
  if (!prompt) {
    result.textContent = 'Prompt required.';
    return;
  }

  button.disabled = true;
  button.textContent = 'Generating…';
  result.textContent = 'Generating with Flow… This may take a moment.';

  try {
    const type = document.getElementById('quick-type').value;
    const aspect = document.getElementById('quick-aspect').value;
    let data;
    if (type === 'image') {
      const sizes = { landscape: '1792x1024', portrait: '1024x1792', square: '1024x1024' };
      data = await apiJson('/v1/images/generations', {
        method: 'POST',
        body: JSON.stringify({
          prompt,
          model: document.getElementById('quick-model').value,
          size: sizes[aspect] || sizes.landscape,
          n: 1,
        }),
      });
    } else {
      data = await apiJson('/v1/videos/generations', {
        method: 'POST',
        body: JSON.stringify({
          prompt,
          aspect,
          duration: Number(document.getElementById('quick-duration').value),
          n: 1,
        }),
      });
    }

    const item = data.data?.[0] || {};
    chrome.runtime.sendMessage({
      type: 'ADD_HISTORY',
      entry: {
        id: `popup-${Date.now()}`,
        time: new Date().toISOString(),
        type: type === 'image' ? 'GEN_IMG' : 'GEN_VID',
        status: 'success',
        url: item.url || '',
        prompt,
      },
    });
    if (item.url) {
      const safeUrl = escHtml(item.url);
      const preview = type === 'image'
        ? `<img class="result-preview" src="${safeUrl}" alt="Generated image">`
        : `<video class="result-preview" src="${safeUrl}" controls preload="metadata"></video>`;
      result.innerHTML = `<div class="result-card">${preview}<div class="result-meta"><strong>Generation ready</strong><a class="result-open" href="${safeUrl}" target="_blank" rel="noreferrer">View</a></div></div>`;
    } else {
      result.textContent = 'Done. Check Flow history for the result.';
    }
    refreshQuickStatus();
    loadMedia();
  } catch (error) {
    result.textContent = `Failed: ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = 'Generate with Flow';
  }
});

updateQuickFields();
refreshQuickStatus();
refreshMonitor();
setInterval(refreshMonitor, 3000);
