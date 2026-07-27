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

document.getElementById('btn-panel').addEventListener('click', () => {
  chrome.windows.getCurrent((win) => {
    chrome.sidePanel.open({ windowId: win.id });
  });
});

chrome.runtime.sendMessage({ type: 'REQUEST_LOG' }, (data) => {
  if (chrome.runtime.lastError) return;
  if (data && data.log) renderLog(data.log);
});

// ── Quick generation ────────────────────────────────────────

function getBackendBase() {
  return new Promise((resolve) => {
    chrome.storage.local.get(['customServerIp'], (data) => {
      const raw = String(data.customServerIp || '127.0.0.1:8001').trim().replace(/\/$/, '');
      if (/^https?:\/\//i.test(raw)) return resolve(raw);
      const local = /^(localhost|127\.0\.0\.1)(:|$)/i.test(raw);
      resolve(`${local ? 'http' : 'https'}://${raw}`);
    });
  });
}

async function apiJson(path, options = {}) {
  const base = await getBackendBase();
  const response = await fetch(`${base}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
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
  const health = document.getElementById('quick-health');
  const credits = document.getElementById('quick-credits');
  try {
    const status = await apiJson('/health');
    const ready = status.status === 'healthy' && status.extension_connected && status.has_flow_key;
    health.textContent = ready ? 'Flow ready' : 'Needs connection';
    health.className = `health-pill ${ready ? 'ok' : 'bad'}`;
    const creditData = await apiJson('/v1/credits');
    const total = creditData.total_credits ?? creditData.credits ?? creditData.data?.credits ?? '—';
    credits.textContent = `${total} credits`;
  } catch (error) {
    health.textContent = 'Backend offline';
    health.className = 'health-pill bad';
    credits.textContent = '— credits';
  }
}

function updateQuickFields() {
  const isVideo = document.getElementById('quick-type').value === 'video';
  const model = document.getElementById('quick-model');
  const duration = document.getElementById('quick-duration');
  const aspect = document.getElementById('quick-aspect');
  model.hidden = isVideo;
  duration.hidden = !isVideo;
  const square = aspect.querySelector('option[value="square"]');
  square.disabled = isVideo;
  if (isVideo && aspect.value === 'square') aspect.value = 'landscape';
}

document.getElementById('quick-type').addEventListener('change', updateQuickFields);

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
  result.textContent = 'Request sent. Keep this popup open.';

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
    if (item.url) {
      result.innerHTML = `Done · <a href="${escHtml(item.url)}" target="_blank" rel="noreferrer">Open result</a>`;
    } else {
      result.textContent = 'Done. Check Flow history for the result.';
    }
    refreshQuickStatus();
  } catch (error) {
    result.textContent = `Failed: ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = 'Generate with Flow';
  }
});

updateQuickFields();
refreshQuickStatus();
