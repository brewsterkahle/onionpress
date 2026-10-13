/**
 * OnionPress Background Script (Chrome service worker, Firefox background page)
 *
 * Routes .onion URLs through OnionPress's local HTTP proxy
 * (src/onionpress/onion_proxy.py, port 9077 for the first macOS user),
 * which fetches them through Tor. Not Tor's SOCKS port: on a Mac that is
 * forwarded out of the Colima VM, and SOCKS through Colima's port forwarding
 * accepts the connection and then closes it (issue #318).
 *
 * The proxy port comes from the native messaging host: a second macOS user's
 * ports are offset by +10000 (19077, ...). Without the native host the
 * default port is used.
 *
 * Clearnet URLs always go DIRECT (normal browsing is never affected).
 * When OnionPress is not running, .onion pages open offline.html, which
 * offers a retry and the Wayback Machine's copy.
 *
 * Shared by the Chrome and Firefox packages (build/build-extension.sh).
 */

const DEFAULT_PROXY_PORT = 9077;
const NATIVE_HOST = "press.onion.onionpress";
// The menubar counts the extension as connected for 10 s after the last
// /status poll or native message (onionpress.browser), so poll well inside it.
const POLL_MS = 5000;

const api = typeof browser !== "undefined" ? browser : chrome;

// Detect Firefox (manifest v2 with proxy.onRequest)
const IS_FIREFOX = typeof browser !== "undefined" && browser.runtime && browser.runtime.getBrowserInfo;

let proxyPort = DEFAULT_PROXY_PORT;
let proxyRunning = false;
let onionAddress = null;
let nativePort = null;

// Detect which browser we're running in
function detectBrowser() {
  if (IS_FIREFOX) return "Firefox";
  const ua = (typeof navigator !== "undefined" && navigator.userAgent) || "";
  if (ua.includes("Edg/")) return "Microsoft Edge";
  if (typeof navigator !== "undefined" && navigator.brave) return "Brave Browser";
  return "Google Chrome";
}

function statusUrl() {
  return `http://127.0.0.1:${proxyPort}/status`;
}

function isOnionHost(hostname) {
  return /\.onion\.?$/i.test(hostname || "");
}

function isOnionUrl(url) {
  try {
    const u = new URL(url);
    return (u.protocol === "http:" || u.protocol === "https:") && isOnionHost(u.hostname);
  } catch (_) {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Proxy status check — at startup and every POLL_MS. Each poll also tells the
// menubar which browser the extension runs in (X-OnionPress-Browser).
// ---------------------------------------------------------------------------

async function checkProxyStatus() {
  try {
    const resp = await fetch(statusUrl(), {
      signal: AbortSignal.timeout(3000),
      headers: { "X-OnionPress-Browser": detectBrowser() }
    });
    if (resp.ok) {
      const data = await resp.json();
      setProxyRunning(data.running === true);
      onionAddress = data.onion_address || null;
      return proxyRunning;
    }
  } catch (_) {
    // proxy not reachable
  }
  setProxyRunning(false);
  return false;
}

function setProxyRunning(running) {
  const changed = running !== proxyRunning;
  proxyRunning = running;
  if (changed) updateProxyConfig();
}

// ---------------------------------------------------------------------------
// Proxy configuration — route .onion through the OnionPress HTTP proxy
// ---------------------------------------------------------------------------

function updateProxyConfig() {
  if (IS_FIREFOX) {
    // Firefox uses browser.proxy.onRequest (registered once below)
    // The listener checks proxyRunning and proxyPort dynamically
    return;
  }

  // Chrome: PAC script routes only .onion through the OnionPress proxy
  if (proxyRunning) {
    const port = Number(proxyPort);
    chrome.proxy.settings.set({
      value: {
        mode: "pac_script",
        pacScript: {
          data: `function FindProxyForURL(url, host) {
            if (/\\.onion\\.?$/i.test(host)) return "PROXY 127.0.0.1:${port}";
            return "DIRECT";
          }`
        }
      },
      scope: "regular"
    });
  } else {
    chrome.proxy.settings.clear({ scope: "regular" });
  }
}

// ---------------------------------------------------------------------------
// Firefox: proxy.onRequest — HTTP proxy for .onion, DIRECT for everything else
// ---------------------------------------------------------------------------

if (IS_FIREFOX) {
  browser.proxy.onRequest.addListener(
    (details) => {
      if (!proxyRunning) {
        return { type: "direct" };
      }
      const url = new URL(details.url);
      if (isOnionHost(url.hostname)) {
        return { type: "http", host: "127.0.0.1", port: proxyPort };
      }
      return { type: "direct" };
    },
    { urls: ["<all_urls>"] }
  );

  browser.proxy.onError.addListener((error) => {
    console.error("OnionPress proxy error:", error.message);
    // The proxy may have gone away; the next .onion page load re-checks.
    checkProxyStatus();
  });
}

// ---------------------------------------------------------------------------
// Offline fallback: a .onion page load while OnionPress is not running opens
// offline.html (retry, or the Wayback Machine's copy). When OnionPress runs
// but the onion service does not answer, the proxy itself returns a page
// with the same choices (onion_proxy.ONION_UNREACHABLE_PAGE).
// ---------------------------------------------------------------------------

api.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  const url = changeInfo.url || (changeInfo.status === "loading" ? tab.url : null);
  if (!url || !isOnionUrl(url)) return;
  checkProxyStatus().then((running) => {
    if (!running) {
      api.tabs.update(tabId, {
        url: api.runtime.getURL("offline.html") + "?url=" + encodeURIComponent(url)
      });
    }
  });
});

// ---------------------------------------------------------------------------
// Native messaging (optional): the proxy port and the onion address
// ---------------------------------------------------------------------------

function connectNative() {
  try {
    nativePort = api.runtime.connectNative(NATIVE_HOST);
    nativePort.onMessage.addListener((msg) => {
      if (msg.onion_address) onionAddress = msg.onion_address;
      const port = Number(msg.proxy_port);
      if (Number.isInteger(port) && port >= 1024 && port <= 65535 && port !== proxyPort) {
        proxyPort = port;
        updateProxyConfig();
        checkProxyStatus();
      }
    });
    nativePort.onDisconnect.addListener(() => {
      nativePort = null;
      setTimeout(connectNative, 30000);
    });
    nativePort.postMessage({ type: "get_config", browser: detectBrowser() });
  } catch (_) {
    nativePort = null;
  }
}

connectNative();
updateProxyConfig();  // Chrome keeps proxy settings across restarts: start clean
checkProxyStatus();
setInterval(checkProxyStatus, POLL_MS);

// ---------------------------------------------------------------------------
// Handle messages from popup and offline page
// ---------------------------------------------------------------------------

api.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "get_status") {
    checkProxyStatus().then(() => {
      sendResponse({ proxyRunning, onionAddress });
    });
    return true;
  }
});
