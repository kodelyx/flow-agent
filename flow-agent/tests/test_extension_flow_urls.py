import json
from pathlib import Path


EXTENSION_DIR = Path(__file__).resolve().parents[2] / "flow-extension"


def test_manifest_allows_new_and_legacy_flow_pages():
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert "https://flow.google.com/*" in manifest["host_permissions"]
    assert "https://flow.google.com/*" in manifest["content_scripts"][0]["matches"]
    assert "https://flow.google.com/*" in manifest["web_accessible_resources"][0]["matches"]
    assert "https://labs.google/fx/tools/flow*" in manifest["content_scripts"][0]["matches"]


def test_background_uses_precise_flow_page_eligibility_and_shared_tab_patterns():
    source = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")

    assert "'https://flow.google.com/*'" in source
    assert "parsed.hostname === 'flow.google.com'" in source
    assert "parsed.hostname !== 'labs.google'" in source
    assert "return createdTab;" in source
    assert "url: '*://labs.google/*'" not in source
    assert source.count("url: FLOW_TAB_URLS") >= 3


def test_background_opens_captcha_capable_project_pages():
    source = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")

    # labs.google/fx/tools/flow redirects to the flow.google.com home page, which
    # never loads reCAPTCHA; only /project/<id> pages do.
    assert "const FLOW_URL = 'https://flow.google.com/';" in source
    assert "function isFlowProjectUrl(url)" in source
    assert "/^\\/project\\/[^/]+/" in source
    assert "https://flow.google.com/project/${encodeURIComponent(projectId)}" in source
    assert "tabs.filter((t) => isFlowProjectUrl(t.url))" in source
    assert "solveCaptcha(id, captchaAction, projectId)" in source


def test_background_refreshes_token_through_labs_handoff():
    source = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")

    # The ya29 bearer is only observable during the labs.google -> flow.google.com
    # redirect; reloading a flow.google.com tab does not re-capture it.
    assert "const TOKEN_URL = 'https://labs.google/fx/tools/flow';" in source
    assert "async function refreshTokenViaLabs()" in source
    assert source.count("await refreshTokenViaLabs()") >= 2
    assert "chrome.tabs.reload(tabs[0].id)" not in source


def test_extension_verifies_captcha_bridge_before_using_a_tab():
    background = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")
    content = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")
    injected = (EXTENSION_DIR / "injected.js").read_text(encoding="utf-8")

    # A tab matching a Flow URL is not enough: its bridge must answer a ping,
    # otherwise every request burns the content-script timeout.
    assert "async function bridgeAlive(tabId)" in background
    assert "if (!(await bridgeAlive(tab.id))) continue;" in background
    assert "type: 'PING_BRIDGE'" in background
    assert "msg.type !== 'PING_BRIDGE'" in content
    assert "'FLOW_AGENT_PING'" in injected and "'FLOW_AGENT_PONG'" in injected
    # GET_CAPTCHA is re-dispatched until injected.js answers; injected.js dedups.
    assert "setInterval(dispatch, 500)" in content
    assert "_captchaInFlight" in injected
    assert "grecaptcha execute timeout" in injected



def test_background_forgets_user_home_and_skips_non_project_candidates():
    source = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")
    reuse = source.split("async function _getOrOpenFlowTab(projectId) {", 1)[1].split(
        "const tabs = await chrome.tabs.query", 1
    )[0]
    guard = "if (!workTabCreatedByExtension && !isFlowProjectUrl(tab?.url)) {"
    assert guard in reuse
    forgotten, owned = reuse.split(guard, 1)[1].split("} else {", 1)
    assert "workTabId = null;" in forgotten
    assert "console.warn(" in forgotten
    assert "return tab;" not in forgotten
    assert "chrome.tabs.update" not in forgotten
    assert "const needsProjectPage = workTabCreatedByExtension && !isFlowProjectUrl(tab?.url);" in owned
    assert "if (tab && needsProjectPage) {" in owned
    assert owned.index("if (tab && needsProjectPage)") < owned.index("chrome.tabs.update")
    candidates = source.split("for (const tab of candidates) {", 1)[1].split("if (tabs.length)", 1)[0]
    assert "if (!isFlowProjectUrl(tab.url)) {" in candidates
    skip = "if (isFlowProjectUrl(targetUrl)) continue;"
    assert "console.warn(" in candidates
    assert candidates.index(skip) < candidates.index("bridgeAlive(tab.id)") < candidates.index("workTabId = tab.id;")
    assert "chrome.tabs.create({ url: targetUrl, active: false })" in source


def test_background_serves_generation_through_flow_batchexecute_rpcs():
    source = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")

    # flow.google.com has no aisandbox-pa calls and no bearer; generation,
    # polling, signed URLs and credits are replayed as the page's own RPCs.
    assert "function classifyFlowRpc(url)" in source
    assert "async function handleFlowRpcRequest(msg, kind)" in source
    for rpc in ("'YhhmEf'", "'jwpduf'", "'as29s'", "'nzlxg'", "'ogiZ0b'"):
        assert rpc in source
    assert "/_/AiSandboxAngularFrontend/data/batchexecute" in source
    assert "const rpcKind = classifyFlowRpc(url);" in source
    assert source.index("const rpcKind = classifyFlowRpc(url);") < source.index("sendToAgent({ id, error: 'INVALID_URL' });")
    # Verified enums (see FLOW_IMAGE_ASPECT / FLOW_VIDEO_ASPECT comments).
    assert "IMAGE_ASPECT_RATIO_SQUARE: 1" in source
    assert "VIDEO_ASPECT_RATIO_PORTRAIT: 1, VIDEO_ASPECT_RATIO_LANDSCAPE: 2" in source
    assert "_portrait" in source
    # A verified cookie session stands in for the bearer, and is never sent to
    # aisandbox-pa as one.
    assert "const FLOW_SESSION_KEY = 'flow-session';" in source
    assert "async function ensureFlowSession(" in source
    assert "if (activeFlowKey === FLOW_SESSION_KEY) {" in source
    assert "const signed = await resolveFlowMediaUrl(mediaId);" in source
