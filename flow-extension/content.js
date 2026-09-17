/**
 * Content script — bridge between background.js and injected.js
 * Injects injected.js into MAIN world to access window.grecaptcha
 */
if (!globalThis.__FLOW_AGENT_CONTENT_LOADED__) {
globalThis.__FLOW_AGENT_CONTENT_LOADED__ = true;

(function () {
  const s = document.createElement('script');
  s.src = chrome.runtime.getURL('injected.js');
  s.onload = () => s.remove();
  (document.head || document.documentElement).appendChild(s);
})();

// Bridge liveness check — answers only if injected.js is running in this page.
chrome.runtime.onMessage.addListener((msg, _, reply) => {
  if (msg.type !== 'PING_BRIDGE') return;

  const requestId = `ping-${Math.random().toString(36).slice(2)}`;
  // Declared before the handler: injected.js answers synchronously inside the
  // first dispatch(), so the handler can run before setInterval is assigned.
  let redispatch = null;
  const handler = (e) => {
    if (e.detail?.requestId === requestId) {
      window.removeEventListener('FLOW_AGENT_PONG', handler);
      clearTimeout(timer);
      if (redispatch !== null) clearInterval(redispatch);
      redispatch = -1; // never start the interval after an answer
      reply({ ok: true, grecaptcha: !!e.detail.grecaptcha });
    }
  };
  const timer = setTimeout(() => {
    window.removeEventListener('FLOW_AGENT_PONG', handler);
    if (redispatch !== null) clearInterval(redispatch);
    redispatch = -1;
    reply({ ok: false });
  }, 2500);
  window.addEventListener('FLOW_AGENT_PONG', handler);

  const dispatch = () => window.dispatchEvent(new CustomEvent('FLOW_AGENT_PING', { detail: { requestId } }));
  dispatch();
  if (redispatch === null) redispatch = setInterval(dispatch, 300);

  return true;
});

chrome.runtime.onMessage.addListener((msg, _, reply) => {
  if (msg.type !== 'GET_CAPTCHA') return;

  const { requestId, pageAction } = msg;

  let redispatch = null; // see PING_BRIDGE: may be answered before assignment
  const handler = (e) => {
    if (e.detail?.requestId === requestId) {
      window.removeEventListener('CAPTCHA_RESULT', handler);
      clearTimeout(timer);
      if (redispatch !== null) clearInterval(redispatch);
      redispatch = -1;
      reply({ token: e.detail.token, error: e.detail.error });
    }
  };

  const timer = setTimeout(() => {
    window.removeEventListener('CAPTCHA_RESULT', handler);
    if (redispatch !== null) clearInterval(redispatch);
    redispatch = -1;
    reply({ error: 'CONTENT_TIMEOUT' });
  }, 25000);

  window.addEventListener('CAPTCHA_RESULT', handler);

  // injected.js is loaded asynchronously via a <script> tag; a dispatch that
  // lands before its listener exists is silently lost. Keep re-dispatching
  // until it answers (injected.js dedups by requestId).
  const dispatch = () => window.dispatchEvent(new CustomEvent('GET_CAPTCHA', {
    detail: { requestId, pageAction },
  }));
  dispatch();
  if (redispatch === null) redispatch = setInterval(dispatch, 500);

  return true; // keep channel open for async reply
});

// ─── TRPC Media URL Monitor ─────────────────────────────────
// Forward intercepted TRPC responses with media URLs to background.js
window.addEventListener('TRPC_MEDIA_URLS', (e) => {
  const { url, body } = e.detail || {};
  if (!body) return;
  chrome.runtime.sendMessage({
    type: 'TRPC_MEDIA_URLS',
    trpcUrl: url,
    body,
  }).catch(() => {});
});

// ─── Video Upload Relay ─────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _, reply) => {
  if (msg.type !== 'UPLOAD_VIDEO') return;

  const { requestId, videoBase64, projectId } = msg;

  const handler = (e) => {
    if (e.detail?.requestId === requestId) {
      window.removeEventListener('UPLOAD_VIDEO_RESULT', handler);
      clearTimeout(timer);
      reply(e.detail);
    }
  };

  const timer = setTimeout(() => {
    window.removeEventListener('UPLOAD_VIDEO_RESULT', handler);
    reply({ error: 'UPLOAD_TIMEOUT' });
  }, 120000); // 2 min timeout for large uploads

  window.addEventListener('UPLOAD_VIDEO_RESULT', handler);

  window.dispatchEvent(new CustomEvent('UPLOAD_VIDEO', {
    detail: { requestId, videoBase64, projectId },
  }));

  return true; // keep channel open for async reply
});

// ─── API Fetch Relay ────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _, reply) => {
  if (msg.type !== 'PROXY_FETCH') return;

  const { url, method, headers, body } = msg;
  fetch(url, {
    method: method || 'POST',
    headers: headers || {},
    credentials: 'include',
    body: method === 'GET' ? undefined : (typeof body === 'string' ? body : JSON.stringify(body)),
  })
    .then(async (res) => {
      const text = await res.text();
      let data;
      try { data = JSON.parse(text); } catch { data = text; }
      reply({ ok: res.ok, status: res.status, data, text });
    })
    .catch((err) => {
      reply({ ok: false, error: err.message });
    });

  return true; // keep channel open for async reply
});
}

