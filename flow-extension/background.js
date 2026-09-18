/**
 * Flow Agent — Chrome Extension Background Service Worker
 *
 * Connects to local Python agent via WebSocket (agent runs WS server).
 * Captures bearer token, solves reCAPTCHA, proxies API calls through browser.
 */

importScripts('config.js');

// Keep the last '[Flow Agent]' console lines in chrome.storage.local (debugLog)
// so a stall can be diagnosed without the service-worker console, which is
// gone by the time anyone looks.
const DEBUG_LOG_MAX = 200;
let _debugLog = [];
let _debugLogFlush = null;
for (const level of ['log', 'warn', 'error']) {
  const original = console[level].bind(console);
  console[level] = (...args) => {
    original(...args);
    if (typeof args[0] !== 'string' || !args[0].startsWith('[Flow Agent]')) return;
    const line = args.map((a) => (typeof a === 'string' ? a : (a?.message ?? JSON.stringify(a)))).join(' ');
    _debugLog.push(`${new Date().toISOString()} ${level.toUpperCase()} ${line}`);
    if (_debugLog.length > DEBUG_LOG_MAX) _debugLog = _debugLog.slice(-DEBUG_LOG_MAX);
    if (!_debugLogFlush) {
      _debugLogFlush = setTimeout(() => {
        _debugLogFlush = null;
        chrome.storage.local.set({ debugLog: _debugLog }).catch(() => {});
      }, 250);
    }
  };
}

let callbackUrl = 'http://127.0.0.1:3001/api/ext/callback';
// NOTE: This is a browser-restricted public API key — safe to ship in extension bundles.
const API_KEY = 'AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY';

let ws = null;
let flowKey = null;
let callbackSecret = null;  // Auth secret for HTTP callback, received from server on WS connect
let httpConnected = false;
let httpPollTimer = null;
let httpPollIntervalMs = 1000;
let state = 'off'; // off | idle | running
let manualDisconnect = false;
let extensionClientId = '';
let connectedServerHost = CONFIG.DEFAULT_SERVER_HOST;

function normalizeCallbackUrl(value) {
  try {
    const raw = String(value || '').trim();
    const parsed = new URL(/^https?:\/\//i.test(raw) ? raw : `https://${raw}`);
    const local = /^(localhost|127\.0\.0\.1|192\.168\.|10\.)/.test(parsed.hostname);
    parsed.protocol = local ? 'http:' : 'https:';
    parsed.pathname = '/api/ext/callback';
    parsed.search = '';
    parsed.hash = '';
    return parsed.toString().replace(/\/$/, '');
  } catch {
    return 'http://127.0.0.1:8001/api/ext/callback';
  }
}
let metrics = {
  tokenCapturedAt: null,
  requestCount: 0,   // captcha-consuming requests only (gen image/video/upscale)
  successCount: 0,
  failedCount: 0,
  lastError: null,
};

// ─── URL → Log Type Classifier ─────────────────────────────

// Visible log types — only these appear in the request log
const _VISIBLE_TYPES = new Set(['GEN_IMG', 'GEN_VID', 'GEN_VID_REF', 'UPSCALE', 'TRACKING', 'URL_REFRESH']);

function _classifyApiUrl(url) {
  if (url.includes('uploadImage')) return 'UPLOAD';
  if (url.includes('batchGenerateImages')) return 'GEN_IMG';
  if (url.includes('UpsampleVideo')) return 'UPSCALE';
  if (url.includes('ReferenceImages')) return 'GEN_VID_REF';
  if (url.includes('batchAsyncGenerateVideo')) return 'GEN_VID';
  if (url.includes('batchCheckAsync')) return 'POLL';
  if (url.includes('upsampleImage')) return 'UPS_IMG';
  if (url.includes('/media/')) return 'MEDIA';
  if (url.includes('/credits')) return 'CREDITS';
  return 'API';
}

// ─── Request Log ────────────────────────────────────────────

let requestLog = [];

function addRequestLog(entry) {
  requestLog.unshift(entry);
  if (requestLog.length > 100) requestLog.pop();
  chrome.storage.local.set({ requestLog }).catch(() => {});
  broadcastRequestLog();
}

function updateRequestLog(id, updates) {
  const entry = requestLog.find((e) => e.id === id);
  if (entry) Object.assign(entry, updates);
  chrome.storage.local.set({ requestLog }).catch(() => {});
  broadcastRequestLog();
}

function broadcastRequestLog() {
  chrome.runtime.sendMessage({ type: 'REQUEST_LOG_UPDATE', log: requestLog }).catch(() => { });
}

// ─── Startup ────────────────────────────────────────────────

let initialization;
function ensureInitialized() {
  if (!initialization) initialization = init().catch((error) => {
    initialization = null;
    console.error('[Flow Agent] Initialization failed:', error);
  });
  return initialization;
}

chrome.runtime.onInstalled.addListener(ensureInitialized);
chrome.runtime.onStartup.addListener(ensureInitialized);
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'reconnect') connectToAgent();
  if (alarm.name === 'keepAlive') keepAlive();
  if (alarm.name === 'flushOutbox') flushOutbox();
  if (alarm.name === 'closeIdleFlowTab') await closeIdleFlowTab();
});

async function init() {
  if (chrome.sidePanel?.setPanelBehavior) {
    try {
      await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
    } catch (error) {
      console.warn('[Flow Agent] Side Panel click behavior unavailable:', error.message);
    }
  }
  await chrome.storage.local.remove('customServerIp');
  const data = await chrome.storage.local.get(['flowKey', 'metrics', 'callbackSecret', 'callbackUrl', 'requestLog']);
  // Only a real bearer or a verified flow session counts; anything else in
  // storage is a stale experiment and must not be reported as a token.
  if (data.flowKey && (data.flowKey.startsWith('ya29.') || data.flowKey === FLOW_SESSION_KEY)) flowKey = data.flowKey;
  if (data.metrics) Object.assign(metrics, data.metrics);
  if (data.callbackSecret) callbackSecret = data.callbackSecret;
  if (data.callbackUrl) callbackUrl = normalizeCallbackUrl(data.callbackUrl);
  if (Array.isArray(data.requestLog)) requestLog = data.requestLog.slice(0, 100);
  await loadOutbox();
  connectToAgent();
  // 0.5 min is Chrome's minimum alarm period — anything lower is silently clamped.
  chrome.alarms.create('keepAlive', { periodInMinutes: 0.5 });
  // Retry any responses left undelivered by a previous worker lifetime.
  chrome.alarms.create('flushOutbox', { periodInMinutes: 0.5 });
  flushOutbox();
}

ensureInitialized();

// ─── Token Capture ──────────────────────────────────────────

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (!details?.requestHeaders?.length) return;
    const authHeader = details.requestHeaders.find(
      (h) => h.name?.toLowerCase() === 'authorization',
    );
    const value = authHeader?.value || '';
    if (!value.startsWith('Bearer ya29.')) return;

    const token = value.replace(/^Bearer\s+/i, '').trim();
    if (!token) return;

    // Always update — even if same token string, refresh the timestamp
    flowKey = token;
    metrics.tokenCapturedAt = Date.now();
    chrome.storage.local.set({ flowKey, metrics });
    console.log('[Flow Agent] Bearer token captured');

    // Notify whichever transport is active.
    sendToAgent({ type: 'token_captured', flowKey, clientId: extensionClientId });
  },
  { urls: ['https://aisandbox-pa.googleapis.com/*', 'https://labs.google/*', 'https://flow.google.com/*'] },
  ['requestHeaders', 'extraHeaders'],
);

let _openingFlowTab = false;

// ─── On-demand tab lifecycle ────────────────────────────────
// Open the Flow tab only when real work needs it (token capture or captcha).
// Keep it available in the background so user tabs are never redirected.
const FLOW_TAB_URLS = [
  'https://flow.google.com/*',
  'https://labs.google/fx/tools/flow*',
  'https://labs.google/fx/*/tools/flow*',
];
// labs.google/fx/tools/flow now 301s to the flow.google.com home page, which never
// loads reCAPTCHA Enterprise — only /project/<id> pages do. Land there directly.
const FLOW_URL = 'https://flow.google.com/';
let workTabId = null;
let flowTabOpening = null;
let workTabCreatedByExtension = false;
let lastFlowProjectUrl = null;

chrome.storage.local.get(['lastFlowProjectUrl']).then((data) => {
  if (!lastFlowProjectUrl && isFlowProjectUrl(data.lastFlowProjectUrl)) {
    lastFlowProjectUrl = data.lastFlowProjectUrl;
  }
}).catch(() => {});

// Remember the most recent project page any tab visits so an on-demand tab can
// open somewhere captcha-capable even when the request carries no projectId.
chrome.tabs.onUpdated.addListener((_, changeInfo) => {
  if (changeInfo.url && isFlowProjectUrl(changeInfo.url)) {
    lastFlowProjectUrl = changeInfo.url;
    chrome.storage.local.set({ lastFlowProjectUrl }).catch(() => {});
  }
});

function isFlowProjectUrl(url) {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' && parsed.hostname === 'flow.google.com'
      && /^\/project\/[^/]+/.test(parsed.pathname);
  } catch {
    return false;
  }
}

function flowTabTargetUrl(projectId) {
  if (projectId) return `https://flow.google.com/project/${encodeURIComponent(projectId)}`;
  return lastFlowProjectUrl || FLOW_URL;
}

// Google only sends the ya29 bearer while labs.google/fx/tools/flow hands off to
// flow.google.com; reloading a flow.google.com page never surfaces it.
const TOKEN_URL = 'https://labs.google/fx/tools/flow';

// Drive a tab through the labs.google handoff so the webRequest listener can
// capture a fresh bearer. Never navigates a tab the user opened.
async function refreshTokenViaLabs() {
  let tabId = null;
  if (workTabId !== null && workTabCreatedByExtension) {
    try {
      await chrome.tabs.get(workTabId);
      tabId = workTabId;
    } catch {
      workTabId = null;
    }
  }
  if (tabId === null) {
    const tab = await chrome.tabs.create({ url: TOKEN_URL, active: false });
    workTabId = tab.id;
    workTabCreatedByExtension = true;
    tabId = tab.id;
  } else {
    await chrome.tabs.update(tabId, { url: TOKEN_URL });
  }
  await waitForTabComplete(tabId);
  scheduleFlowTabClose();
  return tabId;
}

function scheduleFlowTabClose() {
  if (workTabCreatedByExtension) {
    chrome.alarms.create('closeIdleFlowTab', { delayInMinutes: 2 });
  }
}

async function closeIdleFlowTab() {
  if (!workTabId || !workTabCreatedByExtension) return;
  if (state === 'running') {
    scheduleFlowTabClose();
    return;
  }
  const tabId = workTabId;
  workTabId = null;
  workTabCreatedByExtension = false;
  try {
    await chrome.tabs.remove(tabId);
  } catch { /* tab was already closed */ }
}

function isFlowUrl(url) {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:') return false;
    if (parsed.hostname === 'flow.google.com') return true;
    if (parsed.hostname !== 'labs.google') return false;
    return /^\/fx\/(?:[^/]+\/)?tools\/flow(?:\/|$)/.test(parsed.pathname);
  } catch {
    return false;
  }
}

async function waitForTabComplete(tabId, maxWaitMs = 10000) {
  return new Promise((resolve) => {
    const start = Date.now();
    function listener(updatedTabId, changeInfo, tab) {
      if (updatedTabId === tabId && changeInfo.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(tab);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
    setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      chrome.tabs.get(tabId).then(resolve).catch(() => resolve(null));
    }, maxWaitMs);
  });
}

// Every await on the tab-lookup path is bounded: one Chrome API call that never
// settles would otherwise park getOrOpenFlowTab's shared promise forever and
// silently stall every later request behind it.
function withTimeout(promise, ms, label) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}_TIMEOUT`)), ms);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

// True only if content.js AND injected.js answer in this tab. A tab can match a
// Flow URL yet have a dead bridge (opened before an extension reload, discarded,
// or on a page that never loaded injected.js) — sending it GET_CAPTCHA then just
// burns 25s and reports CONTENT_TIMEOUT.
async function bridgeAlive(tabId) {
  const ping = () => withTimeout(chrome.tabs.sendMessage(tabId, { type: 'PING_BRIDGE' }), 5000, 'PING');
  try {
    const resp = await ping();
    if (resp?.ok) return true;
  } catch { /* no content script yet — inject and retry below */ }
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!isFlowUrl(tab?.url)) return false;
    await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] }),
      10000, 'INJECT',
    );
    await sleep(300);
    const resp = await ping();
    return !!resp?.ok;
  } catch (e) {
    console.warn('[Flow Agent] Bridge ping failed for tab', tabId, e.message);
    return false;
  }
}

// Finds/wakes/creates the Flow tab. Returns
// the tab, or null if it couldn't be opened.
async function _getOrOpenFlowTab(projectId) {
  const targetUrl = flowTabTargetUrl(projectId);

  if (workTabId !== null) {
    try {
      let tab = await chrome.tabs.get(workTabId);
      // Never navigate or cache a user's non-project tab, even if its bridge
      // answers. Home pages do not load reCAPTCHA.
      if (!workTabCreatedByExtension && !isFlowProjectUrl(tab?.url)) {
        console.warn('[Flow Agent] User work tab is not on a /project/ page; forgetting it');
        workTabId = null;
      } else {
        const needsProjectPage = workTabCreatedByExtension && !isFlowProjectUrl(tab?.url);
        if (tab && needsProjectPage) {
          await withTimeout(chrome.tabs.update(workTabId, { url: targetUrl }), 10000, 'TAB_UPDATE');
          await waitForTabComplete(workTabId);
          tab = await chrome.tabs.get(workTabId);
        }
        if (await bridgeAlive(workTabId)) {
          scheduleFlowTabClose();
          return tab;
        }
        console.warn('[Flow Agent] Flow tab', workTabId, 'has a dead captcha bridge; looking for another');
        workTabId = null;
      }
    } catch (e) {
      workTabId = null; // closed by the user — fall through and open fresh
    }
  }

  const tabs = await chrome.tabs.query({ url: FLOW_TAB_URLS });
  // Project pages first — they are the only ones that load reCAPTCHA.
  const candidates = [...tabs.filter((t) => isFlowProjectUrl(t.url)), ...tabs.filter((t) => !isFlowProjectUrl(t.url))];
  for (const tab of candidates) {
    if (!isFlowProjectUrl(tab.url)) {
      console.warn('[Flow Agent] Flow tab is not on a /project/ page; reCAPTCHA is only available there');
      if (isFlowProjectUrl(targetUrl)) continue;
    }
    if (!(await bridgeAlive(tab.id))) continue;
    workTabId = tab.id;
    workTabCreatedByExtension = false;
    return tab;
  }
  if (tabs.length) {
    console.warn('[Flow Agent] None of', tabs.length, 'eligible Flow tab(s) answered the bridge ping; opening a fresh one');
  }

  const createdTab = await withTimeout(chrome.tabs.create({ url: targetUrl, active: false }), 10000, 'TAB_CREATE');
  workTabId = createdTab.id;
  workTabCreatedByExtension = true;
  console.log('[Flow Agent] Opened Flow work tab', workTabId, 'at', targetUrl);
  await waitForTabComplete(workTabId);
  await sleep(1500);

  // Inject content script to make sure reCAPTCHA bridge is ready
  try {
    const readyTab = await chrome.tabs.get(workTabId);
    if (!isFlowUrl(readyTab?.url)) throw new Error('INVALID_FLOW_TAB');
    await withTimeout(chrome.scripting.executeScript({
      target: { tabId: workTabId },
      files: ['content.js'],
    }), 10000, 'INJECT');
  } catch (e) {
    console.warn('[Flow Agent] Content script pre-injection:', e.message);
  }

  scheduleFlowTabClose();
  return createdTab;
}

async function getOrOpenFlowTab(projectId) {
  if (flowTabOpening) return flowTabOpening;
  flowTabOpening = withTimeout(_getOrOpenFlowTab(projectId), 60000, 'FLOW_TAB')
    .catch((e) => {
      console.error('[Flow Agent] getOrOpenFlowTab failed:', e.message);
      return null;
    });
  try {
    return await flowTabOpening;
  } finally {
    flowTabOpening = null;
  }
}

// Token is considered fresh if it exists and was captured less than 50 minutes ago.
// Google OAuth tokens expire after ~60 min, so 50 min gives a safe buffer.
function isTokenFresh() {
  if (!flowKey || !metrics.tokenCapturedAt) return false;
  const ageMs = Date.now() - metrics.tokenCapturedAt;
  return ageMs < 50 * 60 * 1000; // 50 minutes
}

async function captureTokenFromFlowTab() {
  // Skip if token is still fresh — no need to open/refresh anything
  if (isTokenFresh()) {
    console.log('[Flow Agent] Token still fresh, skipping tab refresh');
    return;
  }

  // flow.google.com: no bearer exists; a verified cookie session is the token.
  if (await ensureFlowSession()) return;

  if (_openingFlowTab) {
    console.log('[Flow Agent] Flow tab already opening, skipping');
    return;
  }
  _openingFlowTab = true;
  try {
    const tabId = await refreshTokenViaLabs();
    console.log('[Flow Agent] Token refresh triggered via labs.google handoff in tab', tabId);
  } catch (e) {
    console.error('[Flow Agent] Token refresh failed:', e);
  } finally {
    _openingFlowTab = false;
  }
}


// ─── WebSocket to Agent ─────────────────────────────────────

async function connectToAgent() {
  if (manualDisconnect) return;
  await connectHttpAgent();
  if (ws?.readyState === WebSocket.CONNECTING) return;
  if (ws?.readyState === WebSocket.OPEN) return;

  const data = await chrome.storage.local.get(['clientId']);
  const serverIp = CONFIG.DEFAULT_SERVER_HOST;
  connectedServerHost = serverIp;
  const isLocal = /^(127\.0\.0\.1|localhost|192\.168\.|10\.)/.test(serverIp);
  const wsScheme = isLocal ? 'ws' : 'wss';
  const httpScheme = isLocal ? 'http' : 'https';
  const wsUrl = `${wsScheme}://${serverIp}/ws`;

  // Dynamically resolve callbackUrl
  callbackUrl = `${httpScheme}://${serverIp}/api/ext/callback`;

  try {
    ws = new WebSocket(wsUrl);
  } catch (e) {
    console.error('[Flow Agent] WS connect error:', e);
    scheduleReconnect();
    return;
  }

  ws.onopen = async () => {
    console.log('[Flow Agent] Connected to agent: ' + wsUrl);
    chrome.alarms.clear('reconnect');
    setState('idle');

    const storage = await chrome.storage.local.get(['clientId']);
    let clientId = storage.clientId;
    if (!clientId) {
      const prefix = CONFIG.DEFAULT_CLIENT_ID_PREFIX || 'client';
      clientId = `${prefix}-${Math.random().toString(36).substring(2, 8)}`;
      await chrome.storage.local.set({ clientId });
    }
    extensionClientId = clientId;

    // Send current state + resend token if we have one, along with clientId
    ws.send(JSON.stringify({
      type: 'extension_ready',
      clientId: clientId,
      flowKeyPresent: !!flowKey,
      tokenAge: flowKey && metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
    }));
    if (flowKey) {
      ws.send(JSON.stringify({
        type: 'token_captured',
        clientId: clientId,
        flowKey: flowKey
      }));
    }
    // Backend is reachable again — push any responses queued while it was down.
    flushOutbox();
  };

  ws.onmessage = async ({ data }) => {
    try {
      const msg = JSON.parse(data);

      if (msg.method === 'api_request') {
        await handleApiRequest(msg);
      } else if (msg.method === 'get_media_url') {
        await handleGetMediaUrl(msg);
      } else if (msg.method === 'trpc_request') {
        await handleTrpcRequest(msg);
      } else if (msg.method === 'upload_video') {
        await handleUploadVideo(msg);
      } else if (msg.method === 'solve_captcha') {
        await handleSolveCaptcha(msg);
      } else if (msg.method === 'get_status') {
        sendToAgent({
          id: msg.id,
          result: {
            state,
            flowKeyPresent: !!flowKey,
            manualDisconnect,
            tokenAge: metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
            metrics,
          },
        });
      } else if (msg.method === 'reload_extension') {
        console.log('[Flow Agent] Reloading extension on agent request');
        chrome.runtime.reload();
      } else if (msg.method === 'open_flow_tab') {
        // Python bridge asks us to open/focus a Flow tab
        // If token is still fresh, just send it back — no need to open/reload
        if (isTokenFresh()) {
          console.log('[Flow Agent] open_flow_tab: token fresh, sending cached token');
          sendToAgent({ type: 'token_captured', flowKey, clientId: extensionClientId });
        } else if (await ensureFlowSession()) {
          console.log('[Flow Agent] open_flow_tab: flow session verified');
        } else {
          console.log('[Flow Agent] open_flow_tab: token missing/expired, opening tab');
          // Reloading an existing flow.google.com tab never yields a bearer —
          // only the labs.google handoff does.
          await refreshTokenViaLabs();
          await sleep(5000);
          if (flowKey && ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
            console.log('[Flow Agent] Sent token after tab open');
          } else {
            const data = await chrome.storage.local.get(['flowKey']);
            if (data.flowKey) {
              flowKey = data.flowKey;
              if (ws?.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
                console.log('[Flow Agent] Sent token from storage after tab open');
              }
            }
          }
        }
      } else if (msg.method === 'refresh_flow_tab' || msg.method === 'force_refresh') {
        // Python bridge asks us to refresh token.
        // force_refresh (or an explicit msg.force) bypasses the freshness check:
        // Google can invalidate a token via inactivity long before its 50-min
        // age limit, so a "fresh" token may still be dead (401). In that case we
        // must actually reload the tab and re-capture, not resend the cached one.
        const force = msg.force === true || msg.method === 'force_refresh';
        if (isTokenFresh() && !force) {
          console.log('[Flow Agent] refresh_flow_tab: token fresh, sending cached token');
          if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
          }
        } else {
          console.log('[Flow Agent] refresh_flow_tab: forcing tab reload + re-capture');
          // Drop the stale token so captureTokenFromFlowTab can't short-circuit.
          if (force) {
            flowKey = null;
            metrics.tokenCapturedAt = null;
          }
          await captureTokenFromFlowTab();
          await sleep(3000);
          if (flowKey && ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
            console.log('[Flow Agent] Sent token after refresh');
          } else {
            const data = await chrome.storage.local.get(['flowKey']);
            if (data.flowKey) {
              flowKey = data.flowKey;
              if (ws?.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
                console.log('[Flow Agent] Sent token from storage after refresh');
              }
            }
          }
        }
      } else if (msg.type === 'callback_config') {
        callbackSecret = msg.secret;
        callbackUrl = normalizeCallbackUrl(msg.callback_url);
        chrome.storage.local.set({ callbackSecret: msg.secret, callbackUrl });
        console.log('[Flow Agent] Received callback config:', callbackUrl);
      } else if (msg.type === 'callback_secret') {
        callbackSecret = msg.secret;
        chrome.storage.local.set({ callbackSecret: msg.secret });
        console.log('[Flow Agent] Received callback secret');
      } else if (msg.type === 'pong') {
        // keepalive response
      }
    } catch (e) {
      console.error('[Flow Agent] Message error:', e);
    }
  };

  ws.onclose = () => {
    setState('off');
    if (!manualDisconnect) scheduleReconnect();
  };

  ws.onerror = (e) => {
    console.error('[Flow Agent] WS error:', e);
    metrics.lastError = 'WS_ERROR';
    chrome.storage.local.set({ metrics });
  };
}

function agentHttpBase() {
  const host = String(connectedServerHost || CONFIG.DEFAULT_SERVER_HOST).trim().replace(/\/$/, '');
  const hostWithoutScheme = host.replace(/^https?:\/\//i, '');
  const local = /^(127\.0\.0\.1|localhost|192\.168\.|10\.)(:|$)/.test(hostWithoutScheme);
  return /^https?:\/\//i.test(host) ? host : `${local ? 'http' : 'https'}://${host}`;
}

async function connectHttpAgent() {
  if (manualDisconnect || httpConnected) return;
  const storage = await chrome.storage.local.get(['clientId']);
  let clientId = storage.clientId;
  if (!clientId) {
    const prefix = CONFIG.DEFAULT_CLIENT_ID_PREFIX || 'client';
    clientId = `${prefix}-${Math.random().toString(36).substring(2, 8)}`;
    await chrome.storage.local.set({ clientId });
  }
  extensionClientId = clientId;
  connectedServerHost = CONFIG.DEFAULT_SERVER_HOST;
  try {
    const response = await fetch(`${agentHttpBase()}/api/ext/hello`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: clientId,
        clientId,
        flowKey,
        flowKeyPresent: !!flowKey,
        extension_version: chrome.runtime.getManifest().version,
      }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    callbackSecret = data.secret;
    callbackUrl = new URL(data.callback_url, agentHttpBase()).toString();
    httpPollIntervalMs = Math.max(250, Number(data.poll_interval_ms) || 1000);
    httpConnected = true;
    await chrome.storage.local.set({ callbackSecret, callbackUrl });
    setState('idle');
    scheduleHttpPoll(0);
    flushOutbox();
  } catch (error) {
    httpConnected = false;
    console.warn('[Flow Agent] HTTP bridge unavailable; using WebSocket fallback:', error.message);
  }
}

function scheduleHttpPoll(delay = httpPollIntervalMs) {
  if (httpPollTimer) clearTimeout(httpPollTimer);
  if (!httpConnected || manualDisconnect) return;
  httpPollTimer = setTimeout(pollHttpCommands, delay);
}

async function pollHttpCommands() {
  if (!httpConnected || manualDisconnect) return;
  try {
    const response = await fetch(`${agentHttpBase()}/api/ext/poll?session_id=${encodeURIComponent(extensionClientId)}`, {
      headers: { Authorization: `Bearer ${callbackSecret}` },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const commands = data.commands || [];
    if (commands.length > 0 && typeof ws?.onmessage === 'function') {
      commands.forEach((command) => {
        Promise.resolve(ws.onmessage({ data: JSON.stringify(command) })).catch((err) => {
          console.error('[Flow Agent] Command execution error:', err);
        });
      });
    }
    scheduleHttpPoll();
  } catch (error) {
    httpConnected = false;
    console.warn('[Flow Agent] HTTP polling stopped:', error.message);
    scheduleReconnect();
  }
}

function scheduleReconnect() {
  chrome.alarms.create('reconnect', { delayInMinutes: 0.5 });
}

function keepAlive() {
  if (httpConnected) {
    connectHttpAgent();
  } else if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'ping' }));
  } else {
    connectToAgent();
  }
}

function sendToAgent(msg) {
  // API responses (with msg.id) go through a durable outbox so a generated
  // result is never lost — persisted and retried until the agent acks it.
  if (msg.id) {
    enqueueResponse(msg);
    return;
  }
  if (httpConnected && callbackSecret) {
    fetch(callbackUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${callbackSecret}` },
      body: JSON.stringify({ ...msg, session_id: extensionClientId }),
    }).catch(() => {});
  } else if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

// ─── Durable Response Outbox ────────────────────────────────
// A generated image/video result must survive a momentary backend hiccup or a
// service-worker restart. Every id-bearing response is persisted and retried
// with backoff until the agent confirms receipt, then dropped.

const MAX_DELIVERY_ATTEMPTS = 8;
let outbox = {};              // id -> { msg, attempts, nextAt }
let _flushingOutbox = false;

async function loadOutbox() {
  try {
    const { responseOutbox } = await chrome.storage.local.get('responseOutbox');
    if (responseOutbox && typeof responseOutbox === 'object') outbox = responseOutbox;
  } catch { }
}

function persistOutbox() {
  chrome.storage.local.set({ responseOutbox: outbox }).catch(() => { });
}

function enqueueResponse(msg) {
  outbox[msg.id] = { msg, attempts: 0, nextAt: 0 };
  persistOutbox();
  flushOutbox();
}

async function deliverOnce(entry) {
  try {
    const serverIp = connectedServerHost || CONFIG.DEFAULT_SERVER_HOST;
    const targetCallbackUrl = normalizeCallbackUrl(serverIp);

    const resp = await fetch(targetCallbackUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(callbackSecret ? { Authorization: `Bearer ${callbackSecret}` } : {}),
      },
      body: JSON.stringify({ ...entry.msg, session_id: extensionClientId }),
      // A stalled delivery must not wedge flushOutbox (and every response behind it).
      signal: AbortSignal.timeout(30000),
    });
    // Any HTTP reply means the backend is reachable and has taken the response
    // (ok:true = matched a request, ok:false = unknown id / already handled).
    // Either way there is nothing to retry — only transport failures retry.
    if (resp.ok) return true;
    // 5xx / transient server error — retry.
    return false;
  } catch {
    // Network error: backend unreachable. Try WS as an immediate fallback but
    // keep the entry queued so a later flush can still deliver it.
    if (ws?.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify(entry.msg)); } catch { }
    }
    return false;
  }
}

async function flushOutbox() {
  if (_flushingOutbox) return;
  _flushingOutbox = true;
  try {
    const ids = Object.keys(outbox);
    if (!ids.length) return;
    const now = Date.now();
    for (const id of ids) {
      const entry = outbox[id];
      if (!entry) continue;
      if (entry.nextAt && entry.nextAt > now) continue;
      const delivered = await deliverOnce(entry);
      if (delivered) {
        delete outbox[id];
        persistOutbox();
        continue;
      }
      entry.attempts++;
      if (entry.attempts >= MAX_DELIVERY_ATTEMPTS) {
        console.error('[Flow Agent] Dropping response', id, 'after', entry.attempts, 'failed deliveries');
        delete outbox[id];
      } else {
        // Exponential backoff, capped at 30s.
        entry.nextAt = Date.now() + Math.min(30000, 1000 * 2 ** entry.attempts);
      }
      persistOutbox();
    }
  } finally {
    _flushingOutbox = false;
  }
}

// ─── reCAPTCHA Solving ──────────────────────────────────────

async function requestCaptchaFromTab(tabId, requestId, pageAction) {
  try {
    return await chrome.tabs.sendMessage(tabId, {
      type: 'GET_CAPTCHA',
      requestId,
      pageAction,
    });
  } catch (error) {
    const msg = error?.message || '';
    const shouldInject =
      msg.includes('Receiving end does not exist') ||
      msg.includes('Could not establish connection');
    if (!shouldInject) throw error;

    // Inject content script and retry
    const tab = await chrome.tabs.get(tabId);
    if (!isFlowUrl(tab?.url)) throw new Error('INVALID_FLOW_TAB');
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ['content.js'],
    });
    await sleep(200);
    return await chrome.tabs.sendMessage(tabId, {
      type: 'GET_CAPTCHA',
      requestId,
      pageAction,
    });
  }
}

async function solveCaptcha(requestId, captchaAction, projectId) {
  const tab = await getOrOpenFlowTab(projectId);
  if (!tab) return { error: 'NO_FLOW_TAB' };
  console.log('[Flow Agent] Solving captcha', captchaAction, 'in tab', tab.id, tab.url);

  try {
    const resp = await Promise.race([
      requestCaptchaFromTab(tab.id, requestId, captchaAction),
      new Promise((_, rej) => setTimeout(() => rej(new Error('CAPTCHA_TIMEOUT')), 30000)),
    ]);
    return resp;
  } catch (e) {
    return { error: e.message };
  }
}

async function handleSolveCaptcha(msg) {
  const { id, params } = msg;
  const result = await solveCaptcha(id, params?.captchaAction || 'VIDEO_GENERATION', params?.projectId);

  // Standalone captcha solve counts as captcha-consuming
  metrics.requestCount++;
  if (result?.token) {
    metrics.successCount++;
  } else {
    metrics.failedCount++;
    metrics.lastError = result?.error || 'NO_TOKEN';
  }
  chrome.storage.local.set({ metrics });

  sendToAgent({ id, result });
}

// ─── API Request Proxy ──────────────────────────────────────

async function handleTrpcRequest(msg) {
  const { id, params } = msg;
  const { url, method = 'POST', headers = {}, body } = params;

  if (!url || !url.startsWith('https://labs.google/')) {
    sendToAgent({ id, error: 'INVALID_TRPC_URL' });
    return;
  }

  setState('running');
  // TRPC calls don't consume captcha and are silent — no metrics, no request log.

  const fetchHeaders = { 'Content-Type': 'application/json', ...headers };
  if (flowKey) {
    fetchHeaders['authorization'] = `Bearer ${flowKey}`;
  }

  try {
    const resp = await fetch(url, {
      method,
      headers: fetchHeaders,
      body: body ? JSON.stringify(body) : undefined,
      credentials: 'include',
    });
    const data = await resp.json();
    sendToAgent({ id, status: resp.status, data });
  } catch (e) {
    console.error('[Flow Agent] tRPC request failed:', e);
    sendToAgent({ id, error: e.message || 'TRPC_FETCH_FAILED' });
  } finally {
    setState('idle');
  }
}


async function handleUploadVideo(msg) {
  const { id, params } = msg;
  const { videoBase64, projectId, videoSize } = params;

  try {
    const tabs = await chrome.tabs.query({ url: FLOW_TAB_URLS });
    if (!tabs.length) {
      sendToAgent({ id, error: 'NO_FLOW_TAB' });
      return;
    }

    const size = videoSize || (videoBase64 ? Math.floor(videoBase64.length * 3 / 4) : 0);

    // Get session URL via page context XHR (needs session cookies)
    const startResults = await chrome.scripting.executeScript({
      target: { tabId: tabs[0].id },
      world: 'MAIN',
      func: (projId, sz) => {
        return new Promise((resolve) => {
          const xhr = new XMLHttpRequest();
          xhr.open('POST', '/fx/api/upload-video?action=start');
          xhr.setRequestHeader('X-Upload-Project-Id', projId);
          xhr.setRequestHeader('X-Upload-Content-Type', 'video/mp4');
          xhr.setRequestHeader('X-Upload-Content-Length', sz.toString());
          xhr.withCredentials = true;
          xhr.onload = () => {
            let data;
            try { data = JSON.parse(xhr.responseText); } catch { data = {}; }
            resolve({
              sessionUrl: data.sessionUrl || xhr.getResponseHeader('X-Upload-Session-Url') || '',
              status: xhr.status,
            });
          };
          xhr.onerror = () => resolve({ error: 'POST_FAILED' });
          xhr.send();
        });
      },
      args: [projectId, size],
    });

    const step1 = startResults?.[0]?.result;
    if (!step1 || step1.error || !step1.sessionUrl) {
      sendToAgent({ id, error: step1?.error || 'NO_SESSION_URL' });
      return;
    }

    // Return sessionUrl + token — caller handles PUT
    sendToAgent({
      id,
      result: {
        sessionUrl: step1.sessionUrl,
        token: flowKey || '',
      },
    });
  } catch (e) {
    sendToAgent({ id, error: `UPLOAD_ERROR: ${e.message}` });
  }
}

async function handleApiRequest(msg) {
  const { id, params } = msg;
  const { url, method, headers, body, captchaAction } = params;

  if (!url) {
    sendToAgent({ id, error: 'MISSING_URL' });
    return;
  }

  // flow.google.com no longer talks to aisandbox-pa; these calls are served
  // by the page's own batchexecute RPCs instead (see handleFlowRpcRequest).
  const rpcKind = classifyFlowRpc(url);
  if (rpcKind) {
    await handleFlowRpcRequest(msg, rpcKind);
    return;
  }

  if (!url.startsWith('https://aisandbox-pa.googleapis.com/')) {
    sendToAgent({ id, error: 'INVALID_URL' });
    return;
  }

  setState('running');
  const hasCaptcha = !!captchaAction;
  if (hasCaptcha) metrics.requestCount++;

  const logId = id;
  const logType = _classifyApiUrl(url);
  if (_VISIBLE_TYPES.has(logType)) {
    const payloadSummary = body ? JSON.stringify(body).slice(0, 200) : null;
    addRequestLog({ id: logId, type: logType, time: new Date().toISOString(), status: 'processing', error: null, outputUrl: null, url, payloadSummary });
  }

  try {
    // Step 1: Solve captcha if needed
    let captchaToken = null;
    if (captchaAction) {
      const projectId = body?.clientContext?.projectId || body?.requests?.[0]?.clientContext?.projectId || null;
      const captchaResult = await solveCaptcha(id, captchaAction, projectId);
      captchaToken = captchaResult?.token || null;
      if (!captchaToken) {
        // Cannot proceed without captcha — API will 403
        const err = captchaResult?.error || 'CAPTCHA_FAILED';
        console.error(`[Flow Agent] Captcha failed for ${captchaAction}: ${err}`);
        sendToAgent({ id, status: 403, error: `CAPTCHA_FAILED: ${err}` });
        if (hasCaptcha) { metrics.failedCount++; metrics.lastError = `CAPTCHA_FAILED: ${err}`; }
        chrome.storage.local.set({ metrics });
        updateRequestLog(logId, { status: 'failed', error: `CAPTCHA_FAILED: ${err}` });
        setState('idle');
        return;
      }
    }

    // Step 2: Inject captcha token into body
    let finalBody = body;
    if (captchaToken && finalBody) {
      finalBody = JSON.parse(JSON.stringify(finalBody)); // deep clone
      if (finalBody.clientContext?.recaptchaContext) {
        finalBody.clientContext.recaptchaContext.token = captchaToken;
      }
      if (finalBody.requests && Array.isArray(finalBody.requests)) {
        for (const req of finalBody.requests) {
          if (req.clientContext?.recaptchaContext) {
            req.clientContext.recaptchaContext.token = captchaToken;
          }
        }
      }
    }

    // Step 3: Use flowKey for auth
    const activeFlowKey = flowKey;
    if (activeFlowKey === FLOW_SESSION_KEY) {
      // Cookie session only — aisandbox-pa rejects it (XD3). Endpoints not yet
      // mapped to a batchexecute RPC cannot work on flow.google.com.
      const err = `NOT_SUPPORTED_ON_FLOW_GOOGLE_COM: ${_classifyApiUrl(url)}`;
      sendToAgent({ id, status: 501, error: err });
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = err; }
      chrome.storage.local.set({ metrics });
      updateRequestLog(logId, { status: 'failed', error: err });
      setState('idle');
      return;
    }
    if (!activeFlowKey) {
      sendToAgent({ id, status: 503, error: 'NO_FLOW_KEY' });
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = 'NO_FLOW_KEY'; }
      chrome.storage.local.set({ metrics });
      updateRequestLog(logId, { status: 'failed', error: 'NO_FLOW_KEY' });
      setState('idle');
      return;
    }

    const fetchHeaders = { ...(headers || {}) };
    fetchHeaders['authorization'] = `Bearer ${activeFlowKey}`;

    // Step 4: Make the API call from browser context. Bound it: a stalled
    // connection here otherwise leaves the agent waiting for its own timeout
    // with no error ever reported.
    const abort = new AbortController();
    const abortTimer = setTimeout(() => abort.abort(), 120000);
    let response;
    try {
      response = await fetch(url, {
        method: method || 'POST',
        headers: fetchHeaders,
        credentials: 'include',
        body: method === 'GET' ? undefined : JSON.stringify(finalBody),
        signal: abort.signal,
      });
    } finally {
      clearTimeout(abortTimer);
    }

    let responseData;
    const responseText = await response.text();
    try {
      responseData = JSON.parse(responseText);
    } catch {
      responseData = responseText;
    }

    // Self-heal: a 401 means Google invalidated our cached token (usually via
    // inactivity, before our 50-min freshness window). Drop it so the very next
    // request / refresh forces a genuine tab reload + re-capture instead of
    // resending the same dead token.
    if (response.status === 401) {
      console.warn('[Flow Agent] 401 UNAUTHENTICATED — invalidating cached token to force refresh');
      flowKey = null;
      metrics.tokenCapturedAt = null;
      chrome.storage.local.set({ flowKey: null });
    }

    sendToAgent({
      id,
      status: response.status,
      data: responseData,
    });

    const responseSummary = responseText ? responseText.slice(0, 300) : null;
    if (response.ok) {
      if (hasCaptcha) { metrics.successCount++; metrics.lastError = null; }
      updateRequestLog(logId, { status: 'success', httpStatus: response.status, responseSummary });
    } else {
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = `API_${response.status}`; }
      updateRequestLog(logId, { status: 'failed', error: `API_${response.status}`, httpStatus: response.status, responseSummary });
    }
  } catch (e) {
    sendToAgent({
      id,
      status: 500,
      error: e.message || 'API_REQUEST_FAILED',
    });
    if (hasCaptcha) { metrics.failedCount++; metrics.lastError = e.message; }
    updateRequestLog(logId, { status: 'failed', error: e.message || 'API_REQUEST_FAILED' });
  }

  chrome.storage.local.set({ metrics });
  setState('idle');
}

async function handleGetMediaUrl(msg) {
  const { id, params } = msg;
  const mediaId = params?.media_id;
  if (!mediaId) { sendToAgent({ id, error: 'MISSING_MEDIA_ID' }); return; }
  try {
    // Media generated through batchexecute: signed URL comes from as29s.
    const signed = await resolveFlowMediaUrl(mediaId);
    if (signed) { sendToAgent({ id, status: 200, result: { url: signed } }); return; }
    const url = new URL('https://labs.google/fx/api/trpc/media.getMediaUrlRedirect');
    url.searchParams.set('name', mediaId);
    const response = await fetch(url.toString(), { credentials: 'include', redirect: 'follow' });
    if (!response.ok) { sendToAgent({ id, status: response.status, error: `MEDIA_URL_HTTP_${response.status}` }); return; }
    sendToAgent({ id, status: 200, result: { url: response.url } });
  } catch (error) {
    sendToAgent({ id, error: `MEDIA_URL_FAILED: ${error.message}` });
  }
}

// ─── flow.google.com batchexecute RPCs ──────────────────────
//
// The Angular frontend on flow.google.com does not call aisandbox-pa; it
// talks to /_/AiSandboxAngularFrontend/data/batchexecute with cookie auth
// plus a per-page CSRF token (WIZ_global_data.SNlM0e). The RPCs below were
// captured from a real session on 2026-09-18 and are replayed from inside a
// Flow tab (MAIN world) so cookies, CSRF and origin all line up. Results are
// mapped back into the aisandbox-pa REST shapes the Python side still parses.
//
//   YhhmEf  submit text-to-video   -> [null, credits, [[mediaId, ...]], [[opId, projectId, mediaId, "CAE", ...]]]
//   jwpduf  poll   [null,null,[[opId]]] -> [null, credits, [[record]]]; record[5][8][0] = status (6 queued, 2 running, 3 done)
//   as29s   result ["opId"]        -> record incl. signed flow-content.google/video/<opId> URL
//   nzlxg   credits []             -> [credits, ...]
//   ogiZ0b  generate image (sync, ~25 s) -> [[[mediaId, null, sceneId, ..., [[...,"<signed image url>",aspect,...], null, [w,h]]]], ...]

const FLOW_VIDEO_ASPECT = { VIDEO_ASPECT_RATIO_PORTRAIT: 1, VIDEO_ASPECT_RATIO_LANDSCAPE: 2 };
// Verified by generating one image per enum and reading the returned [w,h].
const FLOW_IMAGE_ASPECT = {
  IMAGE_ASPECT_RATIO_SQUARE: 1,     // 1024x1024
  IMAGE_ASPECT_RATIO_PORTRAIT: 2,   // 768x1376
  IMAGE_ASPECT_RATIO_LANDSCAPE: 3,  // 1376x768
  IMAGE_ASPECT_RATIO_3_4: 4,        // 896x1200
  IMAGE_ASPECT_RATIO_4_3: 5,        // 1200x896
};
// flowKey value reported to the agent once the cookie session has been proven
// by a real RPC. The agent only routes work to clients that reported a key;
// there is no bearer to capture on flow.google.com any more.
const FLOW_SESSION_KEY = 'flow-session';
const FLOW_STATUS_DONE = 3;
const FLOW_STATUS_PENDING = new Set([0, 1, 2, 6]);
// opId -> signed video URL, filled in as polls complete.
const flowMediaUrls = new Map();

function classifyFlowRpc(url) {
  if (url.includes('/v1/credits') || url.endsWith('/credits')) return 'credits';
  if (url.includes('batchAsyncGenerateVideoText')) return 't2v';
  if (url.includes('batchCheckAsyncVideoGenerationStatus')) return 'poll';
  if (url.includes('batchGenerateImages')) return 'image';
  return null;
}

// Prove the cookie session with a credits call and tell the agent about it.
// Replaces bearer capture on flow.google.com; the marker expires like a token
// (isTokenFresh) so it is re-proven periodically.
let _ensuringSession = null;
async function ensureFlowSession(force = false) {
  if (!force && flowKey === FLOW_SESSION_KEY && isTokenFresh()) return true;
  if (_ensuringSession) return _ensuringSession;
  _ensuringSession = (async () => {
    try {
      const tab = await flowTabFor(null);
      if (!tab) return false;
      const res = await flowRpc(tab.id, 'nzlxg', [], null);
      if (!res.ok) {
        console.warn('[Flow Agent] Flow session check failed:', res.error);
        return false;
      }
      flowKey = FLOW_SESSION_KEY;
      metrics.tokenCapturedAt = Date.now();
      await chrome.storage.local.set({ flowKey, metrics });
      console.log('[Flow Agent] Flow session verified (credits:', res.data?.[0], ')');
      sendToAgent({ type: 'token_captured', flowKey, clientId: extensionClientId });
      return true;
    } catch (e) {
      console.warn('[Flow Agent] Flow session check error:', e.message);
      return false;
    } finally {
      _ensuringSession = null;
    }
  })();
  return _ensuringSession;
}

// Old keys (abra_t2v_8s) and new ones (veo_3_1_t2v_fast) both map onto the
// new frontend's model keys; portrait is a distinct key with a suffix.
function flowVideoModelKey(requested, aspect) {
  let base = 'veo_3_1_t2v_fast';
  const r = (requested || '').toLowerCase();
  if (r.includes('veo_3_1_t2v')) base = r.replace(/_portrait$/, '');
  else if (r.includes('quality')) base = 'veo_3_1_t2v';
  else if (r.includes('lite')) base = 'veo_3_1_t2v_lite';
  return aspect === 'VIDEO_ASPECT_RATIO_PORTRAIT' ? `${base}_portrait` : base;
}

// Runs inside the Flow tab. Self-contained: executeScript serialises it.
function pageFlowRpc(rpcId, arg, sourcePath) {
  return (async () => {
    try {
      const w = window.WIZ_global_data || {};
      if (!w.SNlM0e) return { ok: false, error: 'NO_CSRF_TOKEN' };
      const q = new URLSearchParams({
        rpcids: rpcId,
        'source-path': sourcePath,
        bl: w.cfb2h || '',
        'f.sid': w.FdrFJe || '',
        hl: w.GWsdKe || 'en',
        _reqid: String(Math.floor(Math.random() * 900000) + 100000),
        rt: 'c',
      });
      const body = new URLSearchParams({
        'f.req': JSON.stringify([[[rpcId, JSON.stringify(arg), null, 'generic']]]),
        at: w.SNlM0e,
      });
      const res = await fetch(`https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute?${q}`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
          'x-same-domain': '1',
        },
        body: body.toString(),
      });
      const text = await res.text();
      for (const line of text.split('\n')) {
        if (!line.startsWith('[[')) continue;
        let frames;
        try { frames = JSON.parse(line); } catch { continue; }
        for (const f of frames) {
          if (!Array.isArray(f) || f[0] !== 'wrb.fr' || f[1] !== rpcId) continue;
          if (typeof f[2] === 'string') return { ok: true, status: res.status, data: JSON.parse(f[2]) };
          return { ok: false, status: res.status, error: `RPC_ERROR ${JSON.stringify(f.slice(3)).slice(0, 300)}` };
        }
      }
      return { ok: false, status: res.status, error: `NO_FRAME ${text.slice(0, 200)}` };
    } catch (e) {
      return { ok: false, error: e.message };
    }
  })();
}

async function flowRpc(tabId, rpcId, arg, projectId) {
  const results = await withTimeout(
    chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      func: pageFlowRpc,
      args: [rpcId, arg, projectId ? `/project/${projectId}` : '/'],
    }),
    60000,
    `FLOW_RPC_${rpcId}`,
  );
  return results?.[0]?.result || { ok: false, error: 'NO_RESULT' };
}

function findFlowUrl(node, kind) {
  if (typeof node === 'string') return node.includes(`flow-content.google/${kind}/`) ? node : null;
  if (Array.isArray(node)) {
    for (const v of node) { const hit = findFlowUrl(v, kind); if (hit) return hit; }
  }
  return null;
}

async function flowTabFor(projectId) {
  return (await getAnyFlowTab()) || (await getOrOpenFlowTab(projectId));
}

async function resolveFlowMediaUrl(mediaId) {
  if (flowMediaUrls.has(mediaId)) return flowMediaUrls.get(mediaId);
  const tab = await flowTabFor(null);
  if (!tab) return null;
  const res = await flowRpc(tab.id, 'as29s', [mediaId], null);
  const url = res.ok ? (findFlowUrl(res.data, 'video') || findFlowUrl(res.data, 'image')) : null;
  if (url) flowMediaUrls.set(mediaId, url);
  return url;
}

function flowStatusString(code) {
  if (code === FLOW_STATUS_DONE) return 'MEDIA_GENERATION_STATUS_SUCCESSFUL';
  if (FLOW_STATUS_PENDING.has(code)) return 'MEDIA_GENERATION_STATUS_ACTIVE';
  return `MEDIA_GENERATION_STATUS_FAILED_${code}`;
}

async function handleFlowRpcRequest(msg, kind) {
  const { id, params } = msg;
  const { url, body, captchaAction } = params;
  const projectId = body?.clientContext?.projectId || body?.requests?.[0]?.clientContext?.projectId || body?.media?.[0]?.projectId || null;
  const logType = _classifyApiUrl(url);
  const visible = _VISIBLE_TYPES.has(logType);
  if (visible) {
    addRequestLog({ id, type: logType, time: new Date().toISOString(), status: 'processing', error: null, outputUrl: null, url, payloadSummary: body ? JSON.stringify(body).slice(0, 200) : null });
  }
  const fail = (status, error) => {
    console.warn('[Flow Agent] flow rpc', kind, 'failed:', error);
    sendToAgent({ id, status, error });
    if (kind === 't2v' || kind === 'image') { metrics.failedCount++; metrics.lastError = error; chrome.storage.local.set({ metrics }); }
    if (visible) updateRequestLog(id, { status: 'failed', error });
    setState('idle');
  };

  setState('running');
  try {
    const tab = await flowTabFor(projectId);
    if (!tab) return fail(503, 'NO_FLOW_TAB');

    if (kind === 'credits') {
      const res = await flowRpc(tab.id, 'nzlxg', [], projectId);
      if (!res.ok) return fail(res.status || 500, res.error);
      const credits = Array.isArray(res.data) ? res.data[0] : null;
      sendToAgent({ id, status: 200, data: { credits, userPaygateTier: 'PAYGATE_TIER_ONE', sku: 'G1_PRO' } });
      setState('idle');
      return;
    }

    if (kind === 'poll') {
      const media = [];
      let credits;
      for (const m of body?.media || []) {
        const res = await flowRpc(tab.id, 'jwpduf', [null, null, [[m.name]]], projectId || m.projectId);
        if (!res.ok) return fail(res.status || 500, res.error);
        credits = res.data?.[1];
        const record = res.data?.[2]?.[0];
        const code = record?.[5]?.[8]?.[0];
        const videoUrl = findFlowUrl(record, 'video');
        if (videoUrl) flowMediaUrls.set(m.name, videoUrl);
        const status = videoUrl ? 'MEDIA_GENERATION_STATUS_SUCCESSFUL' : flowStatusString(code);
        media.push({ name: m.name, mediaMetadata: { mediaStatus: { mediaGenerationStatus: status } } });
      }
      sendToAgent({ id, status: 200, data: { media, remainingCredits: credits } });
      setState('idle');
      return;
    }

    if (kind === 'image') {
      metrics.requestCount++;
      const items = body?.requests || [];
      if (items.some((r) => r?.imageInputs?.length)) {
        return fail(501, 'NOT_SUPPORTED: reference images are not wired to the flow.google.com RPC yet');
      }
      const uuid = () => crypto.randomUUID().toUpperCase();
      // One ogiZ0b call per requested image, in parallel; each needs its own
      // reCAPTCHA token.
      const results = await Promise.all(items.map(async (req, i) => {
        const prompt = req?.structuredPrompt?.parts?.map((p) => p.text).join('\n') || '';
        const aspectEnum = FLOW_IMAGE_ASPECT[req?.imageAspectRatio] || FLOW_IMAGE_ASPECT.IMAGE_ASPECT_RATIO_LANDSCAPE;
        const model = req?.imageModelName || 'NARWHAL';
        const seed = Number.isInteger(req?.seed) ? req.seed : Math.floor(Math.random() * 1000000);
        const captchaResult = await solveCaptcha(`${id}-${i}`, captchaAction || 'IMAGE_GENERATION', projectId);
        const token = captchaResult?.token;
        if (!token) return { ok: false, error: `CAPTCHA_FAILED: ${captchaResult?.error || 'no token'}` };
        const ctx = [null, 22, null, null, null, projectId, null, null, null, null, [token, 1]];
        const arg = [null, [[null, null, null, seed, aspectEnum, model, null, ctx, [[[prompt]]], null, null, null, uuid(), uuid()]], 1, ctx, [uuid()]];
        console.log('[Flow Agent] ogiZ0b submit', model, req?.imageAspectRatio, 'project', projectId);
        const res = await flowRpc(tab.id, 'ogiZ0b', arg, projectId);
        if (!res.ok) return res;
        const imageUrl = findFlowUrl(res.data, 'image');
        if (!imageUrl) return { ok: false, error: `NO_IMAGE_URL ${JSON.stringify(res.data).slice(0, 200)}` };
        const mediaId = res.data?.[0]?.[0]?.[0] || (imageUrl.match(/image\/([0-9a-f-]{36})/) || [])[1];
        flowMediaUrls.set(mediaId, imageUrl);
        return { ok: true, mediaId, imageUrl };
      }));
      const bad = results.find((r) => !r.ok);
      if (bad) return fail(bad.status || 500, bad.error);
      metrics.successCount++;
      metrics.lastError = null;
      chrome.storage.local.set({ metrics });
      const media = results.map((r) => ({ name: r.mediaId, image: { generatedImage: { fifeUrl: r.imageUrl } } }));
      if (visible) updateRequestLog(id, { status: 'success', httpStatus: 200, outputUrl: results[0].imageUrl, responseSummary: JSON.stringify(media.map((m) => m.name)) });
      sendToAgent({ id, status: 200, data: { media } });
      setState('idle');
      return;
    }

    // kind === 't2v'
    metrics.requestCount++;
    const media = [];
    let credits;
    for (const req of body?.requests || []) {
      const prompt = req?.textInput?.structuredPrompt?.parts?.map((p) => p.text).join('\n') || '';
      const aspect = req?.aspectRatio || 'VIDEO_ASPECT_RATIO_LANDSCAPE';
      const modelKey = flowVideoModelKey(req?.videoModelKey, aspect);
      const aspectEnum = FLOW_VIDEO_ASPECT[aspect] || FLOW_VIDEO_ASPECT.VIDEO_ASPECT_RATIO_LANDSCAPE;

      const captchaResult = await solveCaptcha(id, captchaAction || 'VIDEO_GENERATION', projectId);
      const token = captchaResult?.token;
      if (!token) return fail(403, `CAPTCHA_FAILED: ${captchaResult?.error || 'no token'}`);

      const uuid = () => crypto.randomUUID().toUpperCase();
      const arg = [
        [[[null, null, [[[prompt]]]], modelKey, aspectEnum, null, [null, null, null, null, uuid(), uuid()]]],
        [null, 22, null, null, null, projectId, null, null, null, null, [token, 1]],
        [uuid(), 1],
      ];
      console.log('[Flow Agent] YhhmEf submit', modelKey, aspect, 'project', projectId);
      const res = await flowRpc(tab.id, 'YhhmEf', arg, projectId);
      if (!res.ok) return fail(res.status || 500, res.error);
      credits = res.data?.[1];
      const opId = res.data?.[3]?.[0]?.[0] || res.data?.[2]?.[0]?.[3]?.[4];
      if (!opId) return fail(500, `NO_OP_ID ${JSON.stringify(res.data).slice(0, 200)}`);
      media.push({ name: opId });
    }
    metrics.successCount++;
    metrics.lastError = null;
    chrome.storage.local.set({ metrics });
    if (visible) updateRequestLog(id, { status: 'success', httpStatus: 200, responseSummary: JSON.stringify(media) });
    sendToAgent({ id, status: 200, data: { media, remainingCredits: credits } });
    setState('idle');
  } catch (e) {
    fail(500, e.message || 'FLOW_RPC_FAILED');
  }
}

async function getAnyFlowTab() {
  try {
    const tabs = await chrome.tabs.query({ url: FLOW_TAB_URLS });
    if (!tabs || !tabs.length) return null;
    const projectTab = tabs.find((t) => isFlowProjectUrl(t.url));
    return projectTab || tabs[0];
  } catch {
    return null;
  }
}

// ─── State & Popup ──────────────────────────────────────────

function setState(newState) {
  state = newState;
  const badges = { idle: '●', running: '▶', off: '○' };
  const colors = { idle: '#22c55e', running: '#f59e0b', off: '#6b7280' };
  chrome.action.setBadgeText({ text: badges[state] || '' });
  chrome.action.setBadgeBackgroundColor({ color: colors[state] || '#000' });
  broadcastStatus();
}

function broadcastStatus() {
  chrome.runtime.sendMessage({ type: 'STATUS_PUSH' }).catch(() => { });
}

chrome.runtime.onMessage.addListener((msg, _, reply) => {
  if (msg.type === 'SETTINGS_UPDATED') {
    if (ws) {
      try { ws.close(); } catch { }
    }
    connectToAgent();
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'STATUS') {
    reply({
      connected: httpConnected || ws?.readyState === WebSocket.OPEN,
      agentConnected: httpConnected || ws?.readyState === WebSocket.OPEN,
      httpConnected,
      transport: httpConnected ? 'http' : (ws?.readyState === WebSocket.OPEN ? 'ws' : 'none'),
      flowKeyPresent: !!flowKey,
      manualDisconnect,
      tokenAge: metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
      metrics: {
        requestCount: metrics.requestCount,
        successCount: metrics.successCount,
        failedCount: metrics.failedCount,
        lastError: metrics.lastError,
      },
      state,
      clientId: extensionClientId,
    });
  }

  if (msg.type === 'DISCONNECT') {
    manualDisconnect = true;
    httpConnected = false;
    if (httpPollTimer) clearTimeout(httpPollTimer);
    if (ws) ws.close();
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'RECONNECT') {
    manualDisconnect = false;
    connectToAgent();
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'REQUEST_LOG') {
    reply({ log: requestLog });
    return true;
  }

  if (msg.type === 'GET_CLIENT_CREDITS') {
    const host = String(connectedServerHost || CONFIG.DEFAULT_SERVER_HOST).trim().replace(/\/$/, '');
    const hostWithoutScheme = host.replace(/^https?:\/\//i, '');
    const local = /^(127\.0\.0\.1|localhost|192\.168\.|10\.)(:|$)/.test(hostWithoutScheme);
    const base = /^https?:\/\//i.test(host) ? host : `${local ? 'http' : 'https'}://${host}`;
    chrome.storage.local.get(['clientId']).then(({ clientId }) => fetch(`${base}/v1/credits`, {
      headers: (extensionClientId || clientId) ? { 'X-Client-Id': extensionClientId || clientId } : {},
    }))
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        reply(data);
      })
      .catch((error) => {
        console.error('[Flow Agent] Credit request failed:', error);
        reply({ error: error.message });
      });
    return true;
  }

  if (msg.type === 'CLEAR_REQUEST_LOG') {
    requestLog = [];
    chrome.storage.local.remove('requestLog').then(() => {
      broadcastRequestLog();
      reply({ ok: true });
    });
    return true;
  }

  if (msg.type === 'ADD_HISTORY') {
    addRequestLog({
      id: msg.entry?.id || `popup-${Date.now()}`,
      time: msg.entry?.time || new Date().toISOString(),
      type: msg.entry?.type || 'GEN_IMG',
      status: msg.entry?.status || 'success',
      url: msg.entry?.url || '',
      payloadSummary: msg.entry?.prompt || '',
      responseSummary: msg.entry?.url ? 'Generated result ready' : 'Generation completed',
    });
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'OPEN_FLOW_TAB') {
    chrome.tabs.query({ url: FLOW_TAB_URLS }).then((tabs) => {
      if (tabs.length) {
        chrome.tabs.update(tabs[0].id, { active: true });
        reply({ ok: true, tabId: tabs[0].id });
      } else {
        chrome.tabs.create({ url: 'https://labs.google/fx/tools/flow' })
          .then((tab) => reply({ ok: true, tabId: tab.id }))
          .catch((e) => reply({ error: e.message }));
      }
    }).catch((e) => reply({ error: e.message }));
    return true;
  }

  if (msg.type === 'REFRESH_TOKEN') {
    captureTokenFromFlowTab()
      .then(() => reply({ ok: true }))
      .catch((e) => reply({ error: e.message }));
    return true;
  }

  if (msg.type === 'TEST_CAPTCHA') {
    solveCaptcha(`test-${Date.now()}`, msg.pageAction || 'IMAGE_GENERATION')
      .then((r) => reply(r))
      .catch((e) => reply({ error: e.message }));
    return true;
  }

  if (msg.type === 'TRPC_MEDIA_URLS') {
    handleTrpcMediaUrls(msg.trpcUrl, msg.body);
    reply({ ok: true });
    return true;
  }

  return true;
});

// ─── TRPC Media URL Extractor ──────────────────────────────

function handleTrpcMediaUrls(trpcUrl, bodyText) {
  try {
    // Extract all fresh GCS signed URLs
    const urlRegex = /https:\/\/(?:storage\.googleapis\.com\/ai-sandbox-videofx|flow-content\.google\/(?:image|video))\/[0-9a-f-]{36}\?[^"'\s]+/g;
    const matches = bodyText.match(urlRegex) || [];
    if (!matches.length) return;

    // Deduplicate and parse
    const urlMap = {};
    for (const rawUrl of matches) {
      // Unescape JSON-escaped URLs
      const url = rawUrl.replace(/\\u0026/g, '&').replace(/\\/g, '');
      const mediaMatch = url.match(/\/(image|video)\/([0-9a-f-]{36})\?/);
      if (mediaMatch) {
        const [, mediaType, mediaId] = mediaMatch;
        // Keep last occurrence (freshest)
        urlMap[mediaId] = { mediaType, url, mediaId };
      }
    }

    const entries = Object.values(urlMap);
    if (!entries.length) return;

    console.log(`[Flow Agent] Captured ${entries.length} fresh media URLs from TRPC`);
    // URL refresh is silent — don't show in request log

    // Forward to agent for DB update
    sendToAgent({ type: 'media_urls_refresh', urls: entries, session_id: extensionClientId });
  } catch (e) {
    console.error('[Flow Agent] Failed to extract TRPC media URLs:', e);
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ─── Human-like Telemetry ──────────────────────────────────
// Periodically send tracking events to Google's analytics endpoints
// to mimic normal browser behavior.

const _UA = navigator.userAgent;
let _telemetrySessionId = `;${Date.now()}`;

function _rand(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }

function _buildBatchLogPayload() {
  const events = [];
  const types = ['FLOW_IMAGE_LATENCY', 'FLOW_VIDEO_LATENCY'];
  const count = _rand(1, 3);
  for (let i = 0; i < count; i++) {
    events.push({
      event: types[_rand(0, types.length - 1)],
      eventProperties: [
        { key: 'CURRENT_TIME_MS', doubleValue: Date.now() },
        { key: 'DURATION_MS', doubleValue: _rand(150, 800) },
        { key: 'USER_AGENT', stringValue: _UA },
        { key: 'IS_DESKTOP', booleanValue: true },
      ],
      eventMetadata: { sessionId: _telemetrySessionId },
      eventTime: new Date().toISOString(),
    });
  }
  return { appEvents: events };
}

function _buildFrontendEventsPayload() {
  const eventTypes = [
    'FLOW_IMAGE_LATENCY', 'FLOW_VIDEO_LATENCY', 'GRID_SCROLL_DEPTH',
    'FLOW_PROJECT_OPEN', 'FLOW_SCENE_VIEW',
  ];
  const count = _rand(1, 4);
  const events = [];
  for (let i = 0; i < count; i++) {
    const et = eventTypes[_rand(0, eventTypes.length - 1)];
    const params = {
      USER_AGENT: { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: _UA },
      IS_DESKTOP: { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: 'true' },
    };
    if (et.includes('LATENCY')) {
      params.CURRENT_TIME_MS = { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: String(Date.now()) };
      params.DURATION_MS = { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: String(_rand(100, 600)) };
    }
    if (et === 'GRID_SCROLL_DEPTH') {
      params.MEDIA_GENERATION_PAYGATE_TIER = { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: 'PAYGATE_TIER_TWO' };
    }
    events.push({
      eventType: et,
      metadata: {
        sessionId: _telemetrySessionId,
        createTime: new Date().toISOString(),
        additionalParams: params,
      },
    });
  }
  return { events };
}

async function sendTelemetry() {
  if (!flowKey || state === 'off') return;

  const headers = {
    'Content-Type': 'text/plain;charset=UTF-8',
    'authorization': `Bearer ${flowKey}`,
  };

  // Telemetry is silent — don't show in request log
  try {
    if (Math.random() < 0.5) {
      await fetch(`https://aisandbox-pa.googleapis.com/v1:batchLog`, {
        method: 'POST', headers, credentials: 'include',
        body: JSON.stringify(_buildBatchLogPayload()),
      });
    } else {
      await fetch(`https://aisandbox-pa.googleapis.com/v1/flow:batchLogFrontendEvents`, {
        method: 'POST', headers, credentials: 'include',
        body: JSON.stringify(_buildFrontendEventsPayload()),
      });
    }
  } catch { }
}

// Send telemetry at random intervals (45-120s) to look organic
function scheduleTelemetry() {
  const delay = _rand(45, 120) * 1000;
  setTimeout(async () => {
    await sendTelemetry();
    scheduleTelemetry(); // reschedule with new random interval
  }, delay);
}

// Refresh session ID every ~30min like a real user
setInterval(() => { _telemetrySessionId = `;${Date.now()}`; }, _rand(25, 35) * 60 * 1000);

scheduleTelemetry();

console.log('[Flow Agent] Extension loaded');
