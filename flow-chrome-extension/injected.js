/**
 * Injected into MAIN world on labs.google — has access to window.grecaptcha
 * Also intercepts TRPC fetch responses to capture fresh signed media URLs.
 */
const SITE_KEY = '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV';

// ─── XHR Interceptor (for file uploads) ─────────────────────
const _xhrOpen = XMLHttpRequest.prototype.open;
const _xhrSend = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open = function (method, url, ...rest) {
  this.__sniffUrl = url;
  this.__sniffMethod = method;
  return _xhrOpen.call(this, method, url, ...rest);
};
XMLHttpRequest.prototype.send = function (body) {
  try {
    const url = this.__sniffUrl || '';
    if (url.includes('googleapis.com') || url.includes('labs.google') || url.includes('storage.google')) {
      window.postMessage({
        type: '__FLOWKIT_SNIFF__',
        url,
        body: typeof body === 'string' ? body : `(binary ${body?.size || body?.byteLength || '?'} bytes)`,
        method: this.__sniffMethod || 'POST',
      }, '*');
    }
  } catch {}
  return _xhrSend.call(this, body);
};

// ─── TRPC Response Monitor ─────────────────────────────────
// Monkey-patch fetch to intercept TRPC responses containing media URLs.
// Fresh signed GCS URLs are extracted and forwarded to the agent.

const _originalFetch = window.fetch;
window.fetch = async function (...args) {
  try {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';

    // ─── SNIFF ALL outgoing requests (catch upload) ─────────
    {
      let bodyText = '';
      if (args[1]?.body) {
        const b = args[1].body;
        if (typeof b === 'string') bodyText = b.length > 5000 ? b.slice(0, 200) + `...(${b.length} chars)` : b;
        else if (b instanceof FormData) bodyText = `(FormData: ${[...b.keys()].join(', ')})`;
        else if (b instanceof Blob) bodyText = `(Blob ${b.size} bytes, type=${b.type})`;
        else if (b instanceof ArrayBuffer) bodyText = `(ArrayBuffer ${b.byteLength} bytes)`;
        else if (b instanceof ReadableStream) bodyText = '(ReadableStream)';
        else bodyText = JSON.stringify(b)?.slice(0, 2000) || '(unknown)';
      }
      window.postMessage({
        type: '__FLOWKIT_SNIFF__',
        url, body: bodyText, method: args[1]?.method || 'GET',
      }, '*');
    }
  } catch {}

  const response = await _originalFetch.apply(this, args);
  try {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    // Only intercept TRPC calls on labs.google that return project/flow data
    if (url.includes('/fx/api/trpc/') && response.ok) {
      const clone = response.clone();
      clone.text().then(text => {
        if (text.includes('storage.googleapis.com/ai-sandbox-videofx/')) {
          window.dispatchEvent(new CustomEvent('TRPC_MEDIA_URLS', {
            detail: { url, body: text },
          }));
        }
      }).catch(() => {});
    }
  } catch {}
  return response;
};


window.addEventListener('GET_CAPTCHA', async ({ detail }) => {
  const { requestId, pageAction } = detail;
  try {
    await waitForGrecaptcha();
    const token = await window.grecaptcha.enterprise.execute(SITE_KEY, {
      action: pageAction,
    });
    window.dispatchEvent(new CustomEvent('CAPTCHA_RESULT', {
      detail: { requestId, token },
    }));
  } catch (e) {
    window.dispatchEvent(new CustomEvent('CAPTCHA_RESULT', {
      detail: { requestId, error: e.message },
    }));
  }
});

function waitForGrecaptcha(timeout = 10000) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const check = () => {
      if (window.grecaptcha?.enterprise?.execute) return resolve();
      if (Date.now() - start > timeout) return reject(new Error('grecaptcha not available'));
      setTimeout(check, 200);
    };
    check();
  });
}

// ─── Video Upload Handler ───────────────────────────────────
window.addEventListener('UPLOAD_VIDEO', async ({ detail }) => {
  const { requestId, videoBase64, projectId } = detail;
  try {
    // Convert base64 to Blob
    const byteChars = atob(videoBase64);
    const byteArray = new Uint8Array(byteChars.length);
    for (let i = 0; i < byteChars.length; i++) {
      byteArray[i] = byteChars.charCodeAt(i);
    }
    const blob = new Blob([byteArray], { type: 'video/mp4' });

    // Step 1: POST start — get session URL
    const startResp = await _originalFetch('/fx/api/upload-video?action=start', {
      method: 'POST',
      credentials: 'include',
      headers: {
        'X-Upload-Project-Id': projectId || '',
        'X-Upload-Content-Type': 'video/mp4',
        'X-Upload-Content-Length': blob.size.toString(),
      },
    });
    const sessionUrl = startResp.headers.get('X-Upload-Session-Url') || '';
    const startData = await startResp.json().catch(() => ({}));
    // sessionUrl may be in header OR in response body
    const finalSessionUrl = sessionUrl || startData.sessionUrl || '';
    startData._sessionUrl = finalSessionUrl;
    startData._status = startResp.status;

    if (!finalSessionUrl) {
      window.dispatchEvent(new CustomEvent('UPLOAD_VIDEO_RESULT', {
        detail: { requestId, error: 'NO_SESSION_URL', startData },
      }));
      return;
    }

    // Step 2: PUT directly to GCS session URL with resumable upload headers
    const uploadResp = await _originalFetch(finalSessionUrl, {
      method: 'PUT',
      body: blob,
      headers: {
        'Content-Type': 'video/mp4',
        'X-Goog-Upload-Command': 'upload, finalize',
        'X-Goog-Upload-Offset': '0',
      },
    });
    const uploadData = await uploadResp.json().catch(() => ({}));
    uploadData._status = uploadResp.status;

    window.dispatchEvent(new CustomEvent('UPLOAD_VIDEO_RESULT', {
      detail: { requestId, startData, uploadData, status: uploadResp.status },
    }));
  } catch (e) {
    window.dispatchEvent(new CustomEvent('UPLOAD_VIDEO_RESULT', {
      detail: { requestId, error: e.message },
    }));
  }
});
