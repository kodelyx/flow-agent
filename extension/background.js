/**
 * Flow Bridge — background service worker.
 *
 * The browser surface of the flow-go backend, and nothing more. Where the
 * generic CDP bridge this replaces exposes `cdp.call` and `cdp.evaluate` — that
 * is, arbitrary DevTools access to the whole browser — this extension exposes
 * the handful of things the backend actually needs from a page, each with its
 * own scope:
 *
 *   cookies.list      the ~15 cookies Flow depends on, and only those
 *   flow.fingerprint  the identity a generation request has to present
 *   flow.navigate     move the attached tab, inside the tab scope
 *   flow.projects     the project links on the current page
 *   flow.captcha      is the client loaded, and mint a token for an action
 *
 * Every operation above is load-bearing and must not be trimmed. The engine's
 * Bootstrap resolves its project through flow.navigate + flow.projects, reached
 * via Bridge.EnsureProjectTab, and tabs.list / tab.attach / tab.current are on
 * that same path — so removing any of them leaves every generation failing at
 * "no project id resolved", and the per-worker project ids depend on it too.
 *
 * `flow.upscale` used to be listed here. It was implemented and tested at both
 * ends and called by nothing, so it was deleted. The pinned bridge still declares
 * an Upscale method, so a call would now come back as "Unknown operation:
 * flow.upscale" rather than quietly doing nothing. That is the honest answer, and
 * the reason this note is here.
 *
 * Everything page-level runs through chrome.scripting.executeScript rather than
 * chrome.debugger. That is the substantive difference from the generic bridge:
 * no `debugger` permission, so Chrome never shows the "started debugging this
 * browser" banner, and no generic escape hatch ships to a user.
 *
 * Wire format is the backend's: {id, op, params} in, {id, result} or
 * {id, error:{message}} out. Events are pushed as {event, params}.
 */

import { loadConfig, saveConfig, resetConfig, urlAllowed, domainAllowed } from './config.js';

const PROTOCOL_VERSION = 1;

let socket = null;
let reconnectTimer = null;
let attachedTabId = null;
let config = null;

// The tab this extension opened for itself, and nothing else.
//
// The backend asks for a tab when it needs project discovery or a captcha mint,
// and `resolveTab` opens one when none is in scope. Those tabs used to stay in
// the browser for good. This is the id of the one tab we are allowed to close —
// which is what gives the idle timer something to close, and, more importantly,
// what makes it structurally unable to close a tab the user opened.
//
// It is a tab *id* rather than a flag, and that distinction earns its keep:
// `attachedTabId` can move on to one of the user's tabs while ours is still
// open, and ours should still be reclaimed.
let autoCreatedTabId = null;

// How many operations are being handled right now.
//
// The idle countdown is restarted when a dispatch begins, which covers a burst
// of quick commands. It does not cover one slow one: `tabs.open` and `resolveTab`
// each wait up to 45s for a tab to reach its target, and a captcha mint waits on
// the page. A timer firing in the middle of either would close the tab out from
// under the operation using it, so the close is deferred while anything is in
// flight — and rescheduled rather than dropped, so the tab is still reclaimed
// once the work finishes.
let inFlightOps = 0;

// The tab resolution currently in flight, if any. See `resolveTab`.
//
// A generation starts several backend calls at once — project discovery, the
// identity check, the captcha probe — and each asks for a tab. They arrive
// close enough together that more than one can find the scope empty, so without
// this they each open a tab and the user gets duplicates side by side.
let tabResolvingPromise = null;

// A 500-entry event buffer used to live here, and `events.read` drained it. It is
// gone: nothing ever pushed into it, because observing page navigations would need
// the webNavigation permission and that buys a diagnostic rather than a capability
// the backend uses.
//
// The `events.read` op itself stays. The pinned bridge calls it (cdp/client.go),
// and the server's /v1/bridge/events and /v1/debug/events-raw read through it, so
// removing the op would turn two working routes into "Unknown operation". It now
// answers with an empty list — which is what it effectively always did, since the
// buffer was never filled.

const state = {
  daemonConnected: false,
  attachedTabId: null,
  tabTitle: null,
  tabUrl: null,
  lastOp: null,
  lastError: null,
  lastActivity: null,
  captchaMints: 0,
  // What the popup reports as "Cookie Sync". `lastSyncTime` is when the backend
  // last pulled cookies, or when a rotation was announced; `cookieCount` is how
  // many essential cookies that read yielded.
  //
  // The count is the closest thing the extension has to evidence of a signed-in
  // session. It cannot read the Labs session itself, and a Flow tab being open
  // proves nothing on its own — zero essential cookies means there is nothing to
  // authenticate with, whatever the tab looks like.
  lastSyncTime: null,
  cookieCount: 0,
};

/* ------------------------------------------------------------------ *
 * Cookies
 * ------------------------------------------------------------------ */

/**
 * The cookies Flow actually depends on.
 *
 * The backend asks for a whole domain scope, and a signed-in Chrome profile has
 * hundreds of cookies under `.google.com` — analytics for every Google property
 * and a separate session for Mail, Drive, Play, NotebookLM, Colab and the rest.
 * None of them have any bearing on Flow, they made a 19 kB Cookie header, and
 * they were being written to disk. The filter lives here rather than in the
 * backend because this is the extension that knows what Flow needs.
 */
const ESSENTIAL_COOKIES = new Set([
  // The Labs session, and the Google identity cookies it is rebuilt from.
  '__Secure-next-auth.session-token',
  '__Secure-next-auth.callback-url',
  '__Host-next-auth.csrf-token',
  '__Secure-1PSID',
  '__Secure-3PSID',
  '__Secure-1PSIDTS',
  '__Secure-3PSIDTS',
  'SID',
  'HSID',
  'SSID',
  'APISID',
  'SAPISID',
  // Set by the Flow app itself.
  'EMAIL',
  'OSID',
  '__Secure-OSID',
  // The account session, and the record of which accounts are signed in.
  //
  // `authuser=N` selects among the accounts a browser is signed into, but only
  // alongside the cookies that carry those sessions. Dropping these left the
  // parameter with nothing to select, so every index answered for the default
  // account and the account list read as one long.
  'LSID',
  'LSOLH',
  '__Host-1PLSID',
  '__Host-3PLSID',
  'ACCOUNT_CHOOSER',
]);

const isEssentialCookie = (name) => ESSENTIAL_COOKIES.has(String(name || ''));

/**
 * The cookies Google rotates on a timer, rather than on a sign-in.
 *
 * A subset of the essential names on purpose. The rest are stable for the life
 * of a session — `SID` and `SAPISID` are reissued when someone signs in, and the
 * next `cookies.list` picks that up — so watching them would put traffic on the
 * wire for no change. These four are the ones reissued while a tab just sits
 * there, and a jar holding the previous value is refused as expired rather than
 * as wrong.
 */
const ROTATING_COOKIES = new Set([
  '__Secure-1PSIDTS',
  '__Secure-3PSIDTS',
  '__Secure-next-auth.session-token',
  'SIDCC',
]);

/**
 * Is this a cookie domain the backend reads from?
 *
 * `.google.com` is a parent-domain scope, so it covers every Google subdomain —
 * including hosts whose sessions have nothing to do with Flow. That is
 * deliberate and matches the manifest's own host permission; the name whitelist
 * above is what actually narrows this, because the rotating tokens are set on the
 * parent domain rather than on whichever host happened to be open.
 */
function isRotationDomain(domain) {
  const bare = String(domain || '').replace(/^\./, '').toLowerCase();
  if (!bare) return false;
  return bare === 'google.com' || bare.endsWith('.google.com') || bare === 'labs.google';
}

/**
 * The cookies visible for one scope.
 *
 * `getAll({domain})` can come back empty for a host that plainly has cookies —
 * no error, no warning, and the host permission is granted. A per-name `get` on
 * the host's own URL is a different path through Chrome, so when the domain
 * query yields nothing at all it is worth asking by name before reporting the
 * host as having no cookies. Both are scoped and both are permission-checked;
 * this only changes which one is asked first.
 */
async function cookiesForScope(domain, url) {
  const found = await chrome.cookies.getAll(domain ? { domain } : { url });
  if (found.length > 0 || !domain) return found;

  const byName = [];
  for (const name of ESSENTIAL_COOKIES) {
    const one = await chrome.cookies.get({ url: `https://${domain}/`, name });
    if (one) byName.push(one);
  }
  return byName;
}

/**
 * One cookie in the backend's shape — `cookiejar.Cookie`'s JSON tags, not
 * Chrome's camelCase.
 *
 * Shared by `cookies.list` and the rotation announcement below. A merge on the
 * backend side is only safe if both surfaces describe a cookie identically, and
 * two hand-written copies of this mapping is exactly how they would drift.
 */
const shapeCookie = (c) => ({
  domain: c.domain,
  expirationDate: c.expirationDate,
  hostOnly: c.hostOnly,
  httpOnly: c.httpOnly,
  name: c.name,
  path: c.path,
  sameSite: c.sameSite,
  secure: c.secure,
  session: c.session,
  storeId: c.storeId,
  value: c.value,
});

/* ------------------------------------------------------------------ *
 * Tabs
 * ------------------------------------------------------------------ */

async function listScopedTabs() {
  const cfg = await ensureConfig();
  const tabs = await chrome.tabs.query({});
  return tabs.filter((t) => urlAllowed(t.url, cfg));
}

/**
 * The tab the bridge would act on right now, or null. A lookup, never an action.
 *
 * Deliberately not `resolveTab`, which opens a tab when none is in scope: this
 * is called from the status op, and the popup polls that once a second. A status
 * read that created tabs as a side effect would be the worst kind of surprise.
 */
async function currentScopedTab() {
  if (attachedTabId !== null) {
    const tab = await chrome.tabs.get(attachedTabId).catch(() => null);
    if (tab) return tab;
  }
  const scoped = await listScopedTabs();
  return scoped.length > 0 ? scoped[0] : null;
}

/**
 * The tab the bridge should act on, opening one if nothing is in scope.
 *
 * **Single-flight.** A generation starts several backend calls at once — project
 * discovery, the identity check, the captcha probe — and each of them asks for a
 * tab. Without a lock they interleave like this:
 *
 *   1. Call 1 finds nothing in scope and creates a tab.
 *   2. Call 2 asks `listScopedTabs()` while that tab is still navigating, so it
 *      is not in scope *yet* and the listing does not include it.
 *   3. Call 2 creates a second tab.
 *
 * Two Flow tabs side by side, from one command. The lock makes every caller
 * after the first wait on the same resolution, so there is one creation per
 * flight and the rest share its answer.
 *
 * Note what makes this sufficient rather than merely helpful: `waitForTab`
 * resolves only once the new tab's URL is inside the scope, and `listScopedTabs`
 * filters on exactly that predicate. So by the time the flight settles the tab
 * is visible to the next listing, and a later caller finds it instead of
 * creating another.
 *
 * A caller that *named* a tab is exempt. `tabId` is a specific request rather
 * than "find me a tab", and folding it into the flight would hand it whichever
 * tab the flight happened to settle on.
 */
async function resolveTab(tabId = null) {
  if (tabId) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab && urlAllowed(tab.url, await ensureConfig())) return tab;
  }

  if (tabResolvingPromise) return tabResolvingPromise;

  tabResolvingPromise = _resolveTabInternal();
  try {
    return await tabResolvingPromise;
  } finally {
    // Cleared by the owner of the flight, never by a waiter. A waiter clearing
    // this would let a third caller open a second flight while the first was
    // still resolving — which is the duplicate this lock exists to prevent,
    // arriving by a new route.
    tabResolvingPromise = null;
  }
}

/**
 * The body of `resolveTab`, run at most once at a time.
 *
 * Kept separate so the lock above stays small enough to read at a glance.
 * Everything that can create a tab lives in here, which is what makes "one
 * creation per flight" a property of the structure rather than of a check
 * somebody has to remember to keep.
 */
async function _resolveTabInternal() {
  if (attachedTabId !== null) {
    const tab = await chrome.tabs.get(attachedTabId).catch(() => null);
    if (tab && urlAllowed(tab.url, await ensureConfig())) return tab;
  }
  const scoped = await listScopedTabs();
  if (scoped.length > 0) return scoped[0];

  const cfg = await ensureConfig();
  if (!cfg.autoOpenOnCommand) throw new Error('No tab inside the configured scope is open');

  const target = (cfg.targetUrlPrefixes || [])[0];
  if (!target) throw new Error('No tab inside the configured scope is open, and no target to open');

  // Ours to close. This is the only branch in this function that *created* a
  // tab — every path above returns one that was already open, so the countdown
  // is never armed for a tab the user made. Arming it here, and not after the
  // wait, also means a tab that never finishes loading is still reclaimed.
  const created = await chrome.tabs.create({ url: target, active: false });
  autoCreatedTabId = created.id;
  scheduleAutoTabClose();

  await waitForTab(created.id, (t) => urlAllowed(t.url, cfg), 45000, target);
  return chrome.tabs.get(created.id);
}

function attach(tabId) {
  attachedTabId = tabId;
  updateState({ attachedTabId: tabId });
  return chrome.tabs.get(tabId);
}

function detach() {
  attachedTabId = null;
  updateState({ attachedTabId: null });
  return { detached: true };
}

async function waitForTab(tabId, predicate, timeoutMs = 45000, what = 'The tab') {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab && predicate(tab)) return tab;
    if (Date.now() > deadline) throw new Error(`${what} did not reach the expected state`);
    await sleep(250);
  }
}

/* ------------------------------------------------------------------ *
 * The auto-created tab's lifecycle
 *
 * A tab the backend needed once should not sit in the browser for good, and a
 * tab the user opened must never be touched. `autoCreatedTabId` is the whole of
 * that distinction: it is set only where this extension called
 * `chrome.tabs.create`, and never on a path that found an already-open tab. The
 * close below therefore cannot reach a tab the user made.
 * ------------------------------------------------------------------ */

const AUTO_CLOSE_ALARM = 'closeIdleFlowTab';

/**
 * Restart the two-minute idle countdown.
 *
 * Called where an auto-created tab appears and at the top of every dispatch, so
 * the countdown restarts while work is happening and only expires once nothing
 * has been asked of this extension for two minutes.
 *
 * A no-op when there is no auto-created tab. Arming the alarm anyway would leave
 * a timer running that can only ever do nothing, and it would fire against
 * whatever id Chrome had handed out next.
 */
function scheduleAutoTabClose() {
  if (autoCreatedTabId !== null) {
    chrome.alarms.create(AUTO_CLOSE_ALARM, { delayInMinutes: 2 });
  }
}

/**
 * Close the auto-created tab, if it is still ours and still idle.
 *
 * Two guards, and both matter. `autoCreatedTabId === null` means there is
 * nothing we are allowed to close. `inFlightOps > 0` means an operation is using
 * a tab right now, so the countdown is restarted instead of closing — a dispatch
 * that outlives the delay must not have its tab pulled out from under it.
 */
async function closeIdleFlowTab() {
  if (autoCreatedTabId === null) return;

  if (inFlightOps > 0) {
    scheduleAutoTabClose();
    return;
  }

  const tabId = autoCreatedTabId;
  autoCreatedTabId = null;
  chrome.alarms.clear(AUTO_CLOSE_ALARM);

  // Detach before removing: the tab is about to stop existing, and an attached
  // id that outlives its tab makes `tab.current` answer null for a tab the
  // backend still believes it is holding.
  if (attachedTabId === tabId) detach();

  try {
    await chrome.tabs.remove(tabId);
  } catch {
    /* Already closed — by the user, or by an earlier run of this timer. */
  }
}

// A tab closed by hand must not leave the countdown armed for an id Chrome has
// already released. Without this the alarm would fire on a dead id, and in the
// worst case Chrome would have handed that number to a different tab by then.
chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === autoCreatedTabId) {
    autoCreatedTabId = null;
    chrome.alarms.clear(AUTO_CLOSE_ALARM);
  }
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------------ *
 * Page-level work
 * ------------------------------------------------------------------ */

/**
 * Run a function in the attached tab's MAIN world and return its value.
 *
 * MAIN rather than the default isolated world, because everything this extension
 * reads lives on the page: window.grecaptcha, navigator.userAgentData, the
 * rendered project list. The function must be self-contained — it is serialised
 * and re-created in the page, so it cannot close over anything here.
 */
async function inPage(fn, args = []) {
  const tab = await resolveTab();
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: 'MAIN',
    func: fn,
    args,
  });
  if (!results || results.length === 0) throw new Error('The page returned nothing');
  return results[0].result;
}

/**
 * The page's own request identity.
 *
 * The reCAPTCHA assessment is tied to the client that produced the token, so a
 * generation request that goes out under a different user-agent or sec-ch-ua
 * does not line up with its own token and is rejected as unusual activity.
 *
 * The shape is the backend's, down to `mobile` being a `?0`/`?1` client hint and
 * `platformFull` being a quoted string — those go straight into sec-ch-ua
 * headers, so the formatting is part of the value.
 */
function readFingerprint() {
  const d = navigator.userAgentData || {};
  const brands = (d.brands || []).map((b) => '"' + b.brand + '";v="' + b.version + '"').join(', ');
  return {
    userAgent: navigator.userAgent,
    language: navigator.language || 'en-US',
    brands,
    platform: d.platform || '',
    mobile: d.mobile ? '?1' : '?0',
    platformFull: '"' + (d.platform || 'macOS') + '"',
  };
}

/** The project links the Flow app has rendered on the current page. */
function readProjectLinks() {
  return [...document.querySelectorAll('a[href*="/project/"]')].map((a) => a.href).slice(0, 40);
}

/**
 * The two values the app puts on every batchexecute request.
 *
 * `at` goes in the form body and `f.sid` in the query string. The backend read the
 * first and never sent the second, so every call it made was missing a parameter
 * the app sends on all of them — visible only by reading the app's own traffic.
 */
function readPageTokens() {
  const w = window.WIZ_global_data || {};
  return { at: w.SNlM0e || '', fsid: w.FdrFJe || '' };
}

/**
 * Mint a reCAPTCHA Enterprise token with the page's own client.
 *
 * The `ready()` wait is load-bearing: skipping it still returns a token, but one
 * produced before the client finished initialising, and the assessment behind it
 * scores low enough that the upstream rejects the call. A token of the right
 * length is therefore not evidence that this worked.
 */
async function mintCaptcha(siteKey, action) {
  try {
    if (!window.grecaptcha || !window.grecaptcha.enterprise) {
      return { available: false, error: 'the page has no reCAPTCHA client loaded' };
    }
    await new Promise((resolve) => window.grecaptcha.enterprise.ready(resolve));
    const token = await window.grecaptcha.enterprise.execute(siteKey, { action });
    return { available: true, token };
  } catch (e) {
    return { available: true, error: String(e) };
  }
}

/* ------------------------------------------------------------------ *
 * Operations
 * ------------------------------------------------------------------ */

/**
 * The entry point every caller goes through — the socket, and the popup's
 * runtime message — so neither has to remember to keep the idle countdown alive.
 *
 * The counter is what makes refreshing here sufficient. Refreshing on entry
 * alone covers a burst of quick commands but not one that outlives the
 * two-minute delay, and `closeIdleFlowTab` reads this counter to defer rather
 * than close while anything is still in flight.
 *
 * The body lives in `dispatch` so this wrapper stays small enough to see: it is
 * the only place the countdown and the in-flight count are maintained, and a
 * `try/finally` around a 200-line switch is exactly the kind of thing that gets
 * broken by a later edit.
 */
async function handle(op, params = {}) {
  inFlightOps += 1;
  scheduleAutoTabClose();
  try {
    return await dispatch(op, params);
  } finally {
    inFlightOps -= 1;
  }
}

async function dispatch(op, params = {}) {
  const cfg = await ensureConfig();

  switch (op) {
    case 'ping':
      // `ops` is how the backend decides which surface it is talking to. The
      // generic bridge answers ping too, but without this list — so a backend
      // that prefers these operations can tell the two apart instead of guessing
      // from a failed call.
      return {
        ok: true,
        protocol: PROTOCOL_VERSION,
        version: chrome.runtime.getManifest().version,
        ops: [
          'ping', 'config.get', 'config.set', 'config.reset',
          'tabs.list', 'tabs.open', 'tab.attach', 'tab.detach', 'tab.current',
          'events.read', 'cookies.list', 'cookies.names',
          'flow.fingerprint', 'flow.navigate', 'flow.projects',
          'flow.captcha', 'flow.at', 'status',
        ],
      };

    case 'config.get':
      return cfg;

    case 'config.set': {
      const previous = await ensureConfig();
      const next = await saveConfig(params.patch || {});
      config = next;

      // Reconnect when the endpoint or the credential changes. On first pairing
      // this is what moves the socket from tokenless to authenticated: the
      // backend hands the token over on the connection it has just accepted, and
      // without a reconnect the socket stays tokenless and the pairing marker is
      // never written — so the bridge would sit in the open state for good.
      //
      // The close is deferred by a tick so the reply goes out first. Closing
      // synchronously drops it: `send` checks `readyState`, and `close` moves it
      // to CLOSING before returning, so the backend would report a disconnected
      // socket for a `config.set` that actually succeeded.
      const endpointChanged =
        (params.patch?.bridgeUrl && params.patch.bridgeUrl !== previous.bridgeUrl) ||
        (params.patch?.bridgeToken && params.patch.bridgeToken !== previous.bridgeToken);
      if (endpointChanged) setTimeout(() => socket?.close(), 0);

      return next;
    }

    case 'config.reset':
      config = await resetConfig();
      return config;

    case 'tabs.list': {
      const tabs = await listScopedTabs();
      return tabs.map((t) => ({ tabId: t.id, url: t.url, title: t.title || null }));
    }

    case 'tabs.open': {
      const url = String(params.url || '');
      if (!urlAllowed(url, cfg)) throw new Error(`Refusing to open a URL outside the tab scope: ${url}`);
      const created = await chrome.tabs.create({ url, active: params.active !== false });
      // Ours as well, and this is the one case where a tab we created is
      // deliberately in front of the user — `active` defaults to true here,
      // unlike in `resolveTab`. It is still reclaimed on the same idle
      // countdown, and the refresh at the top of every dispatch is what keeps
      // that countdown away from work in progress.
      autoCreatedTabId = created.id;
      scheduleAutoTabClose();
      const tab = await waitForTab(created.id, (t) => urlAllowed(t.url, cfg), 45000, url);
      return { tabId: tab.id, url: tab.url, title: tab.title || null };
    }

    case 'tab.attach': {
      const tab = await resolveTab(params.tabId || null);
      const attached = await attach(tab.id);
      return { tabId: attached.id, url: attached.url, title: attached.title || null };
    }

    case 'tab.detach':
      return detach();

    case 'tab.current': {
      if (attachedTabId === null) return null;
      const tab = await chrome.tabs.get(attachedTabId).catch(() => null);
      return tab ? { tabId: tab.id, url: tab.url, title: tab.title || null } : null;
    }

    case 'events.read': {
      // Always empty, and deliberately kept rather than removed.
      //
      // The buffer this used to drain is gone — nothing ever pushed into it. But
      // the op itself is on the wire: the pinned bridge calls it, and the
      // server's /v1/bridge/events and /v1/debug/events-raw read through it. An
      // unknown operation would turn those two working routes into a 502, so the
      // answer stays a well-formed empty list. `limit` is accepted and ignored
      // for the same reason: a caller sending it should not get an error back.
      return [];
    }

    case 'cookies.names': {
      // A diagnostic: which cookie names are visible for a scope, without any
      // values. Retrieval fails silently in three different ways — the scope
      // check refuses, the host permission was never granted, and the name
      // filter drops everything — and all three return the same empty list.
      // Chrome gives no error for a missing host permission, so the only way to
      // tell them apart is to look before the filter runs.
      const details = params.details || {};
      const domain = String(details.domain || '');
      const url = String(details.url || '');
      if (!domain && !url) {
        throw new Error('Refusing an unscoped cookie read: give a domain or a url');
      }

      const direct = await chrome.cookies.getAll(domain ? { domain } : { url });
      const found = await cookiesForScope(domain, url);
      const names = [...new Set(found.map((c) => String(c.name)))].sort();
      const hosts = [...new Set(found.map((c) => String(c.domain)))].sort();

      return {
        scope: domain || url,
        // `direct` is what the domain query alone returned, `seen` is what the
        // per-name fallback added. When direct is 0 and seen is not, the domain
        // query is the thing that cannot see this host.
        direct: direct.length,
        seen: found.length,
        names,
        hosts,
        kept: names.filter(isEssentialCookie),
      };
    }

    case 'cookies.list': {
      const details = params.details || {};
      const domain = String(details.domain || '');
      const url = String(details.url || '');

      // Exactly one scope is required, and it has to be given rather than
      // inferred. An unscoped call used to fall through to `getAll({})` — every
      // cookie in the profile — and because the name filter downstream kept
      // only the fifteen Flow cookies, the widening was invisible: it looked
      // like a scoped read that happened to return the right names.
      if (!domain && !url) {
        throw new Error('Refusing an unscoped cookie read: give a domain or a url');
      }
      if (domain && !domainAllowed(domain, cfg)) {
        throw new Error(`Refusing to read cookies outside the configured scope: ${domain}`);
      }
      if (url) {
        let host = '';
        try {
          host = new URL(url).hostname;
        } catch {
          throw new Error(`Not a URL: ${url}`);
        }
        if (!domainAllowed(host, cfg)) {
          throw new Error(`Refusing to read cookies outside the configured scope: ${url}`);
        }
      }

      const found = await cookiesForScope(domain, url);
      const kept = found.filter((c) => isEssentialCookie(c.name));

      // Stamp the pull before answering. This is the read the backend actually
      // syncs from, so it is what "Cookie Sync" in the popup is reporting.
      updateState({ lastSyncTime: Date.now(), cookieCount: kept.length });

      return kept.map((c) => ({
        domain: c.domain,
        expirationDate: c.expirationDate,
        hostOnly: c.hostOnly,
        httpOnly: c.httpOnly,
        name: c.name,
        path: c.path,
        sameSite: c.sameSite,
        secure: c.secure,
        session: c.session,
        storeId: c.storeId,
        value: c.value,
      }));
    }

    case 'flow.fingerprint': {
      const fp = await inPage(readFingerprint);
      if (!fp || !fp.userAgent) throw new Error('The page did not report an identity');
      return fp;
    }

    case 'flow.navigate': {
      const url = String(params.url || '');
      if (!urlAllowed(url, cfg)) throw new Error(`Refusing to navigate outside the tab scope: ${url}`);
      const tab = await resolveTab();
      await chrome.tabs.update(tab.id, { url });
      return { tabId: tab.id, url };
    }

    case 'flow.projects':
      return await inPage(readProjectLinks);

    case 'flow.at': {
      const t = await inPage(readPageTokens);
      return { at: String((t && t.at) || ''), fsid: String((t && t.fsid) || '') };
    }

    case 'flow.captcha': {
      const action = String(params.action || '');
      const siteKey = cfg.recaptchaSiteKey || params.siteKey || '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV';
      if (!action) {
        // A probe: the caller wants to know whether the page can mint at all,
        // usually to decide whether to navigate somewhere that can.
        const fp = await inPage(() => !!(window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.execute));
        return { available: !!fp };
      }
      const out = await inPage(mintCaptcha, [siteKey, action]);
      if (out && out.token) state.captchaMints++;
      return out || { available: false, error: 'the page returned nothing' };
    }

    case 'status': {
      // The tab is reported here rather than only when some other op happens to
      // resolve one. The popup reads `tabUrl` on its first paint, before any op
      // has run, and the bundle export reads it to name the project — so a value
      // that is only ever set as a side effect of something else would leave both
      // of them describing a browser that is not there.
      const tab = await currentScopedTab();
      return {
        ...state,
        tabUrl: tab?.url || null,
        tabTitle: tab?.title || null,
        config: cfg,
      };
    }

    default:
      throw new Error(`Unknown operation: ${op}`);
  }
}

/* ------------------------------------------------------------------ *
 * Connection
 * ------------------------------------------------------------------ */

async function ensureConfig() {
  if (!config) config = await loadConfig();
  return config;
}

function socketUrl(cfg) {
  const base = String(cfg.bridgeUrl || '').replace(/\/$/, '');
  const token = String(cfg.bridgeToken || '');
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

function send(payload) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
}

function updateState(values) {
  Object.assign(state, values, { lastActivity: Date.now() });
  chrome.action.setBadgeText({ text: socket ? 'ON' : '' }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ color: '#16803c' }).catch(() => {});
}

async function connect() {
  if (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState)) return;
  const cfg = await ensureConfig();

  try {
    socket = new WebSocket(socketUrl(cfg));
  } catch (error) {
    updateState({ daemonConnected: false, lastError: error?.message || String(error) });
    scheduleReconnect();
    return;
  }

  socket.onopen = () => {
    updateState({ daemonConnected: true, lastError: null });
    send({
      event: 'bridge.ready',
      params: {
        version: chrome.runtime.getManifest().version,
        protocol: PROTOCOL_VERSION,
        config: cfg,
      },
    });

    // Prime the count the popup shows, so it is not blank until the backend
    // happens to pull cookies. Scoped to one domain and filtered to the
    // essential names, exactly like every other read here — an unscoped
    // `getAll({})` would touch every cookie in the profile, which is the thing
    // `cookies.list` refuses to do on purpose.
    chrome.cookies
      .getAll({ domain: 'google.com' })
      .then((all) => {
        const count = all.filter((c) => isEssentialCookie(c.name)).length;
        updateState({ cookieCount: count });
      })
      .catch(() => {});
  };

  socket.onmessage = async (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!msg || !msg.id) return;

    updateState({ lastOp: msg.op });
    try {
      const result = await handle(msg.op, msg.params || {});
      send({ id: msg.id, result });
    } catch (error) {
      updateState({ lastError: error?.message || String(error) });
      send({ id: msg.id, error: { message: error?.message || String(error) } });
    }
  };

  socket.onclose = () => {
    updateState({ daemonConnected: false });
    socket = null;
    scheduleReconnect();
  };

  socket.onerror = () => {
    updateState({ daemonConnected: false });
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = config?.reconnectDelayMs || 1500;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

chrome.alarms.create('keepAlive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'keepAlive') connect();
  // The idle countdown for the tab this extension opened for itself. Nothing
  // else in this worker closes a tab, and this cannot reach one the user made —
  // see `autoCreatedTabId`.
  if (alarm.name === AUTO_CLOSE_ALARM) closeIdleFlowTab();
});

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);

// The popup asks the worker for its own state rather than keeping a copy, so what
// it shows is what the bridge is doing. It goes through the same dispatcher as
// the backend's calls, which keeps one definition of every operation.
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (!msg || !msg.op) return false;
  handle(msg.op, msg.params || {})
    .then((result) => {
      // A list answer is wrapped rather than spread. `{...["a"]}` is `{0:"a"}` —
      // spreading an array into an object silently drops its array-ness, so the
      // popup's cookie export would iterate an object and find nothing. The
      // popup reads `reply.result` for those ops.
      reply(Array.isArray(result) ? { ok: true, result } : { ok: true, ...result });
    })
    .catch((error) => reply({ ok: false, error: error?.message || String(error) }));
  return true;
});

/* ------------------------------------------------------------------ *
 * Cookie rotation
 *
 * The one thing this extension says without being asked. Everything else is a
 * reply; this is a rotation Google performed on its own schedule, which the
 * backend cannot see and would otherwise only notice as an expired session.
 * ------------------------------------------------------------------ */

// A rotation arrives as a burst: Chrome fires one change per cookie, and signing
// in rotates several at once. Announcing each one would put five near-identical
// frames on the wire and make the backend re-read its jar five times for what is
// one event. So the changes are collected and sent once the burst goes quiet.
//
// Tunable through config for the same reason `reconnectDelayMs` is: the right
// window is a property of the deployment, not of the code.
const ROTATION_DEBOUNCE_MS = 1500;

let rotationTimer = null;
const rotationPending = new Map();

function rotationDebounceMs() {
  const value = Number(config?.cookieRotationDebounceMs);
  return Number.isFinite(value) && value >= 0 ? value : ROTATION_DEBOUNCE_MS;
}

/**
 * Announce the rotating cookies that changed, once the burst has settled.
 *
 * The values are re-read rather than taken from the change event. The event
 * carries the value Chrome saw at the instant it fired, and the debounce window
 * exists precisely because the cookie can rotate again inside it — so the value
 * on disk at the end is the one the backend needs, and the one in the event is
 * the one that has already gone stale.
 *
 * A cookie that has disappeared is reported by name, because a merge cannot tell
 * "unchanged" from "removed" from values alone, and treating a removed session
 * cookie as unchanged is the failure this whole path exists to prevent.
 */
async function flushRotation() {
  rotationTimer = null;

  const changed = [...rotationPending.values()];
  rotationPending.clear();
  if (changed.length === 0) return;

  const cookies = [];
  const removed = [];
  for (const c of changed) {
    if (c.removed) {
      removed.push(c.name);
      continue;
    }
    const host = c.domain.replace(/^\./, '');
    const one = await chrome.cookies.get({ url: `https://${host}/`, name: c.name }).catch(() => null);
    // Gone between the event and the re-read: Chrome fires a removal for that
    // case too, but the two can arrive out of order.
    if (one) cookies.push(shapeCookie(one));
    else removed.push(c.name);
  }

  // A rotation is the other half of "the cookies moved", so it restarts the sync
  // clock the popup shows rather than waiting for the backend's next pull.
  updateState({ lastSyncTime: Date.now() });

  // A rotation that lands on a closed socket is dropped rather than queued:
  // `send` is a no-op there, and the next `cookies.list` reads the same values.
  // This event is an optimisation, not the source of truth.
  send({ event: 'bridge.cookies_rotated', params: { cookies, removed, changedAt: Date.now() } });
}

function onCookieChanged(change) {
  const c = change?.cookie;
  if (!c || !isRotationDomain(c.domain) || !ROTATING_COOKIES.has(String(c.name))) return;

  // Keyed by domain and name, so a cookie that rotates three times inside one
  // window is announced once — at its final value, which is the point.
  rotationPending.set(`${c.domain}\t${c.name}`, {
    domain: String(c.domain),
    name: String(c.name),
    removed: !!change.removed,
  });

  if (rotationTimer) clearTimeout(rotationTimer);
  rotationTimer = setTimeout(flushRotation, rotationDebounceMs());
}

// Passive. This observes cookies the user's own browsing already rotates: it
// asks Chrome for nothing new — the `cookies` permission is held for
// `cookies.list` regardless — and it never writes a cookie.
chrome.cookies.onChanged.addListener(onCookieChanged);

connect();
