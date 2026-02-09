/**
 * OnionPress Background Service Worker
 *
 * Intercepts ALL .onion navigations and redirects them through the local
 * OnionPress proxy at localhost:9077.  Also connects to the native messaging
 * host to discover proxy port and service status.
 */

const PROXY_BASE = "http://localhost:9077";
const NATIVE_HOST = "press.onion.onionpress";
const ONION_RE = /^https?:\/\/([a-z2-7]{56}\.onion)(\/.*)?$/i;

let proxyRunning = false;
let onionAddress = null;
let nativePort = null;  // native messaging port

// ---------------------------------------------------------------------------
// Proxy status polling
// ---------------------------------------------------------------------------

async function checkProxyStatus() {
  try {
    const resp = await fetch(`${PROXY_BASE}/status`, { signal: AbortSignal.timeout(3000) });
    if (resp.ok) {
      const data = await resp.json();
      proxyRunning = data.running === true;
      onionAddress = data.onion_address || null;
      return true;
    }
  } catch (_) {
    // proxy not reachable
  }
  proxyRunning = false;
  return false;
}

// Poll every 15 seconds
setInterval(checkProxyStatus, 15000);
checkProxyStatus();

// ---------------------------------------------------------------------------
// Native messaging (optional — used to write extension-connected marker)
// ---------------------------------------------------------------------------

function connectNative() {
  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST);
    nativePort.onMessage.addListener((msg) => {
      if (msg.proxy_port) {
        // could override PROXY_BASE in the future
      }
      if (msg.onion_address) {
        onionAddress = msg.onion_address;
      }
      if (msg.running !== undefined) {
        proxyRunning = msg.running;
      }
    });
    nativePort.onDisconnect.addListener(() => {
      nativePort = null;
      // Retry after 30 seconds
      setTimeout(connectNative, 30000);
    });
    // Request config
    nativePort.postMessage({ type: "get_config" });
  } catch (_) {
    // Native host not installed — that's OK, proxy polling is sufficient
    nativePort = null;
  }
}

connectNative();

// ---------------------------------------------------------------------------
// Intercept .onion navigations
// ---------------------------------------------------------------------------

/**
 * Convert a .onion URL into a proxy URL.
 * e.g. http://abc...xyz.onion/path → http://localhost:9077/proxy/abc...xyz.onion/path
 */
function toProxyUrl(url) {
  const m = url.match(ONION_RE);
  if (!m) return null;
  const host = m[1].toLowerCase();
  const path = m[2] || "/";
  return `${PROXY_BASE}/proxy/${host}${path}`;
}

// Listen for navigations to .onion URLs (typed in address bar or link clicks)
chrome.webNavigation.onBeforeNavigate.addListener(async (details) => {
  // Only handle main frame and sub-frame navigations
  if (details.frameId !== 0 && details.frameType !== "sub_frame") return;

  const proxyUrl = toProxyUrl(details.url);
  if (!proxyUrl) return;

  // Check if proxy is running before redirecting
  const up = await checkProxyStatus();
  if (up) {
    chrome.tabs.update(details.tabId, { url: proxyUrl });
  } else {
    // Show offline page
    chrome.tabs.update(details.tabId, { url: chrome.runtime.getURL("offline.html") });
  }
}, {
  url: [{ hostSuffix: ".onion" }]
});

// ---------------------------------------------------------------------------
// Handle messages from popup and content scripts
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "get_status") {
    checkProxyStatus().then(() => {
      sendResponse({
        proxyRunning,
        onionAddress,
      });
    });
    return true; // async response
  }

  if (msg.type === "to_proxy_url") {
    sendResponse({ url: toProxyUrl(msg.url) });
    return false;
  }
});
