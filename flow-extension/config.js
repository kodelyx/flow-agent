/**
 * Flow Bridge — configuration.
 *
 * Deliberately the same shape as the generic bridge's, so the backend can push
 * scope with the same `config.set` call and nothing on that side has to know
 * which extension it is talking to.
 *
 * The defaults are narrower than a generic bridge's would be: this extension
 * exists for one application, so it starts scoped to that application's own
 * hosts rather than to every tab and cookie on the machine.
 */

export const DEFAULTS = {
  // WebSocket endpoint of the flow-go backend.
  bridgeUrl: 'ws://127.0.0.1:9222',

  // There is deliberately no `bridgeToken` here.
  //
  // The token is not a setting and has no default: the backend generates it,
  // hands it over on the first connection via `config.set`, and the extension
  // persists it from there. Declaring an empty default made it look like
  // something a person was meant to fill in, which is what put a paste box in
  // the popup. Absent, `socketUrl` simply dials without one — which is exactly
  // how the first pairing is supposed to start.

  // Only tabs whose URL starts with one of these can be attached or listed. The
  // first entry is also what gets opened when a command needs a tab and none
  // exists.
  targetUrlPrefixes: [
    'https://flow.google.com',
    'https://labs.google',
  ],

  // Open the first target when a command needs a tab and none is open. This is
  // what makes a cold start work: the backend may start before the browser.
  autoOpenOnCommand: true,

  // Cookie operations are scoped to these domains. The backend asks for the
  // whole scope and filters down to the ~15 names it needs; this is the outer
  // boundary, and it deliberately excludes mail, drive, play and the rest.
  //
  // `accounts.google.com` is here for `ACCOUNT_CHOOSER` alone — the only record
  // of which Google accounts are signed in. Without it only `authuser=0` can be
  // addressed and every other account reads as absent.
  cookieDomains: ['labs.google', 'google.com', 'accounts.google.com'],

  // Maximum events buffered before the oldest are dropped. Only the Flow hosts
  // are observed, so this is small on purpose.
  //
  // Nothing reads this any more. The buffer it sized was removed — nothing ever
  // pushed into it — and `events.read` now answers an empty list. The key is kept
  // because the backend pushes the generic bridge's config shape, and a key it
  // writes should not vanish on the next read.
  eventBufferSize: 500,

  // How long a cookie-rotation burst is allowed to settle before it is announced
  // over the socket. Chrome fires one change per cookie and a sign-in rotates
  // several at once, so without this the backend would re-read its jar once per
  // cookie for what is a single event.
  cookieRotationDebounceMs: 1500,

  // Reconnect backoff for the backend socket.
  reconnectDelayMs: 1500,

  // Keep-alive alarm period, in minutes. MV3 service workers are evicted when
  // idle; this wakes the worker so the socket stays up.
  keepAliveMinutes: 0.5,
};

const STORAGE_KEY = 'flowGoBridge.config';

/** Read config from chrome.storage.local, falling back to defaults per key. */
export async function loadConfig() {
  let stored = {};
  try {
    const raw = await chrome.storage.local.get(STORAGE_KEY);
    stored = raw?.[STORAGE_KEY] || {};
  } catch {
    stored = {};
  }
  return { ...DEFAULTS, ...stored };
}

/** Merge a partial config over what is stored and persist it. */
export async function saveConfig(patch = {}) {
  const current = await loadConfig();
  const next = { ...current, ...patch };
  await chrome.storage.local.set({ [STORAGE_KEY]: next });
  return next;
}

/** Reset to factory defaults. */
export async function resetConfig() {
  await chrome.storage.local.remove(STORAGE_KEY);
  return { ...DEFAULTS };
}

/** True when `url` is inside the configured tab scope. */
export function urlAllowed(url, config) {
  const value = String(url || '');
  if (!value) return false;
  const prefixes = config.targetUrlPrefixes || [];
  if (prefixes.length === 0) return false;
  return prefixes.some((prefix) => value.startsWith(prefix));
}

/** True when a cookie domain is inside the configured scope. */
export function domainAllowed(domain, config) {
  const value = String(domain || '');
  if (!value) return false;
  const allowed = config.cookieDomains || [];
  if (allowed.length === 0) return false;
  return allowed.some((entry) => {
    const normalized = entry.startsWith('.') ? entry : `.${entry}`;
    return value === entry || value === normalized || value.endsWith(normalized);
  });
}
