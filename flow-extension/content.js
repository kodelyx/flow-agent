/**
 * Content script — bridge between background.js and injected.js
 * Injects injected.js into MAIN world to access window.grecaptcha
 */
(function () {
  const s = document.createElement('script');
  s.src = chrome.runtime.getURL('injected.js');
  s.onload = () => s.remove();
  (document.head || document.documentElement).appendChild(s);
})();

chrome.runtime.onMessage.addListener((msg, _, reply) => {
  if (msg.type !== 'GET_CAPTCHA') return;

  const { requestId, pageAction } = msg;

  const handler = (e) => {
    if (e.detail?.requestId === requestId) {
      window.removeEventListener('CAPTCHA_RESULT', handler);
      clearTimeout(timer);
      reply({ token: e.detail.token, error: e.detail.error });
    }
  };

  const timer = setTimeout(() => {
    window.removeEventListener('CAPTCHA_RESULT', handler);
    reply({ error: 'CONTENT_TIMEOUT' });
  }, 25000);

  window.addEventListener('CAPTCHA_RESULT', handler);

  window.dispatchEvent(new CustomEvent('GET_CAPTCHA', {
    detail: { requestId, pageAction },
  }));

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

// ─── Aisandbox Request Sniffer (via postMessage from MAIN world) ──
window.addEventListener('message', (e) => {
  if (e.data?.type !== '__FLOWKIT_SNIFF__') return;
  const { url, body, method } = e.data;
  if (!url) return;
  chrome.runtime.sendMessage({
    type: 'SNIFFED_AISANDBOX_REQUEST',
    url,
    method,
    payload: body,
    timestamp: Date.now(),
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
