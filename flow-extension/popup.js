/**
 * Status popup. Reads the service worker's own state rather than keeping a copy,
 * so what it shows is what the bridge is actually doing.
 *
 * It carries two actions:
 *
 *   Copy Account Bundle   one account, self-contained: its cookies plus the
 *                         project, the browser identity and the page tokens the
 *                         backend would otherwise have to rediscover. For when a
 *                         dump has to be produced by hand without going through
 *                         the backend's HTTP surface. What lands on the
 *                         clipboard is exactly what `cookiejar.LoadBundleFile`
 *                         reads, so it can be saved as
 *                         `cookies/account_<key>.json` and simply work.
 *   Open Google Flow      opens the app in a new tab, so the panel is still
 *                         useful when the backend is not running at all.
 *
 * There is deliberately no bridge-token field. Pairing is not a human step — the
 * extension connects tokenless on first run, the backend hands it a token over
 * that connection, and the extension persists it and reconnects with it. The
 * token is therefore absent from this file entirely: no input, no read of
 * `config.bridgeToken`, nothing to validate. `socketUrl` and the reconnect in
 * `background.js` still carry it, because they are what completes the handover.
 *
 * The DOM surface is deliberately narrow — getElementById and plain node
 * properties only. The test harness supplies a stub DOM with exactly those, so
 * querySelector, classList or innerHTML would break the harness rather than the
 * browser, which is the failure mode this file is most likely to hit.
 *
 * Note that the script assigns `className` wholesale (`badge.className = 'ok'`).
 * Whatever class the markup starts with is gone after the first refresh, so
 * every rule in popup.html has to key off the element itself plus `.ok` / `.off`
 * and nothing else.
 */

const el = (id) => document.getElementById(id);

/** Ask the worker something over the popup channel. */
async function send(op, params) {
  try {
    return await chrome.runtime.sendMessage(params ? { op, params } : { op });
  } catch {
    return null;
  }
}

/**
 * The same call, for the cases where a refusal has to be told from an answer.
 *
 * The worker replies `{ok: true, ...result}` on success and `{ok: false, error}`
 * on failure, and both are *resolved* values — so a failed read is truthy and
 * `send(...).at` is `undefined` rather than an error. Every live read below is
 * best-effort, and this is what keeps a refusal out of the bundle instead of
 * quietly writing an empty string into it.
 */
async function ask(op, params) {
  const reply = await send(op, params);
  if (!reply || reply.ok === false) return null;
  return reply;
}

async function refresh() {
  const status = await send('status');

  const badge = el('badge');
  const sessionStatus = el('sessionStatus');
  const syncStatus = el('syncStatus');
  const engineStatus = el('engineStatus');

  if (status?.daemonConnected) {
    if (badge) {
      badge.textContent = 'Connected';
      badge.className = 'ok';
    }

    // The port is read from the config rather than written into the label, so a
    // changed WS_PORT does not leave the popup claiming 9222.
    if (engineStatus) {
      let port = '9222';
      try {
        if (status?.config?.bridgeUrl) {
          port = new URL(status.config.bridgeUrl).port || '9222';
        }
      } catch (_) {
        // An unparseable endpoint is not worth a blank line; the default stands.
      }

      engineStatus.textContent = `Connected (:${port})`;
      engineStatus.className = 'v';
    }

    // Cookie sync is what the backend pulls over the socket, so the honest
    // report is when it last pulled rather than a flat claim that it is running.
    if (syncStatus) {
      if (status.lastSyncTime) {
        const sec = Math.max(0, Math.floor((Date.now() - status.lastSyncTime) / 1000));
        if (sec < 5) syncStatus.textContent = 'Synced just now';
        else if (sec < 60) syncStatus.textContent = `Synced ${sec}s ago`;
        else syncStatus.textContent = `Synced ${Math.floor(sec / 60)}m ago`;
      } else {
        syncStatus.textContent = 'Real-time Active';
      }
      syncStatus.className = 'v ok';
    }

    // The extension cannot read the Labs session itself, so the count of
    // essential cookies stands in for it. Zero means there is nothing to
    // authenticate with, whatever the attached tab looks like.
    if (sessionStatus) {
      const count = status.cookieCount || 0;
      if (count > 0) {
        sessionStatus.textContent = `Active (${count} Cookies)`;
        sessionStatus.className = 'v ok';
      } else if (status.tabUrl && status.tabUrl.includes('flow.google.com')) {
        sessionStatus.textContent = 'Flow Tab Active';
        sessionStatus.className = 'v ok';
      } else {
        sessionStatus.textContent = 'No Cookies (Sign In)';
        sessionStatus.className = 'v faint';
      }
    }
  } else {
    if (badge) {
      badge.textContent = 'Disconnected';
      badge.className = 'off';
    }
    if (sessionStatus) {
      sessionStatus.textContent = 'Waiting for Bridge';
      sessionStatus.className = 'v faint';
    }
    if (syncStatus) {
      syncStatus.textContent = 'Disconnected';
      syncStatus.className = 'v faint';
    }
    if (engineStatus) {
      engineStatus.textContent = 'Offline';
      engineStatus.className = 'v off';
    }
  }
}

/**
 * The manifest is the only place the version is written down, so read it from
 * there rather than repeating it in the markup where the two would drift.
 */
function paintVersion() {
  try {
    const version = chrome.runtime.getManifest()?.version;
    if (version) el('version').textContent = `v${version}`;
  } catch {
    // A popup that cannot read its own manifest still has to render, and the
    // markup already carries a sane fallback.
  }
}

/* ------------------------------------------------------------------ *
 * Account bundle export
 * ------------------------------------------------------------------ */

/**
 * The Flow project the attached tab is sitting on, or '' when there is none.
 *
 * Read from the tab URL rather than asked of the backend, and the two are not
 * the same question: the backend answers with the project it resolved at boot,
 * which may be a configured one, or one it read from this tab minutes ago and
 * which the operator has since navigated away from. What belongs in a dump is
 * where the browser is now.
 */
function projectFromTabUrl(tabUrl) {
  const match = /\/project\/([A-Za-z0-9_-]+)/.exec(String(tabUrl || ''));
  return match ? match[1] : '';
}

/**
 * The essential cookies for every configured domain, de-duplicated.
 *
 * Deliberately the same operation the backend uses, so what lands on the
 * clipboard is the set the engine would sync rather than "whatever Chrome has".
 * One call per domain is required and not a shortcut avoided: an unscoped read is
 * refused on purpose, and widening it here would export every cookie in the
 * profile — analytics for every Google property, plus a separate session for
 * Mail, Drive and the rest.
 *
 * `status` is passed in by the bundle export, which has already read it. Left
 * optional so this stays callable on its own — the export is not the only thing
 * that has ever wanted the cookies.
 */
async function collectCookies(status) {
  const cfg = (status || (await send('status')))?.config || {};
  const domains = cfg.cookieDomains || [];
  if (domains.length === 0) throw new Error('no cookie domains are configured');

  const seen = new Set();
  const merged = [];

  for (const domain of domains) {
    const reply = await send('cookies.list', { details: { domain } });

    // The worker wraps a list reply in `result` instead of spreading it, because
    // `{...["a"]}` is `{0:"a"}` — spreading an array into an object silently
    // drops its array-ness, and the popup would then iterate over an object.
    const list = Array.isArray(reply?.result) ? reply.result : [];
    for (const cookie of list) {
      const key = `${cookie.domain}\t${cookie.name}\t${cookie.path}`;
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(cookie);
    }
  }
  return merged;
}

/**
 * One account, self-contained, in the shape the backend reads back:
 * `cookiejar.Bundle` — project_id, at, fsid, fingerprint, cookies.
 *
 * The three live values are best-effort, because each of them needs a Flow tab
 * and the tab is closed more often than not. A bundle without them is still a
 * credential — the engine resolves the project from the listing and primes for
 * the token — so a missing one is reported rather than allowed to fail the
 * export. What must not happen is a bundle that silently claims to be complete,
 * which is why the caller says what it got.
 *
 * The fingerprint's field names are the bundle's, not the page's: the page
 * reports `brands` and `platformFull`, and the file spells those `sec_ch_ua` and
 * `platform`. Writing the page's names through unchanged would leave every field
 * unset after a decode, with no error anywhere.
 */
async function collectBundle() {
  const status = await send('status');

  const bundle = {
    project_id: projectFromTabUrl(status?.tabUrl),
    at: '',
    fsid: '',
    cookies: [],
  };

  const tokens = await ask('flow.at');
  if (tokens) {
    bundle.at = String(tokens.at || '');
    bundle.fsid = String(tokens.fsid || '');
  }

  const fp = await ask('flow.fingerprint');
  if (fp?.userAgent) {
    bundle.fingerprint = {
      user_agent: String(fp.userAgent),
      sec_ch_ua: String(fp.brands || ''),
      platform: String(fp.platformFull || fp.platform || ''),
      language: String(fp.language || ''),
      mobile: String(fp.mobile || ''),
    };
  }

  bundle.cookies = await collectCookies(status);
  return bundle;
}

/** "a", "a and b", "a, b and c" — the popup has one line and no list markup. */
function listOf(items) {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

el('copy').addEventListener('click', async () => {
  const result = el('result');
  const button = el('copy');
  button.disabled = true;
  result.className = '';
  result.textContent = 'Collecting cookies…';

  try {
    const bundle = await collectBundle();
    if (bundle.cookies.length === 0) {
      throw new Error('no cookies were visible for the configured domains');
    }
    await navigator.clipboard.writeText(JSON.stringify(bundle, null, 2));

    // Say what is actually in it. A bundle missing the identity and the page
    // tokens is a different thing from a complete one, and the difference only
    // shows up as a generation that comes back empty — so it is reported here
    // rather than left for whoever pastes it to discover.
    const extras = [];
    if (bundle.project_id) extras.push('the project');
    if (bundle.fingerprint) extras.push('the browser identity');
    if (bundle.at || bundle.fsid) extras.push('the page tokens');

    result.textContent = extras.length
      ? `Copied ${bundle.cookies.length} cookies with ${listOf(extras)}.`
      : `Copied ${bundle.cookies.length} cookies — no Flow tab, so no project, identity or page tokens.`;
    result.className = 'ok';
  } catch (error) {
    result.textContent = `Could not copy: ${error?.message || String(error)}`;
    result.className = 'off';
  } finally {
    button.disabled = false;
  }
});

el('openFlow')?.addEventListener('click', () => {
  if (typeof chrome !== 'undefined' && chrome.tabs?.create) {
    chrome.tabs.create({ url: 'https://flow.google.com' });
  } else if (typeof window !== 'undefined') {
    window.open('https://flow.google.com', '_blank');
  }
});

paintVersion();
refresh();
setInterval(refresh, 1000);
