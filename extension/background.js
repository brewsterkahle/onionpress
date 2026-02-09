/**
 * OnionPress Background Service Worker
 *
 * Routes .onion URLs through Tor's SOCKS proxy at localhost:9050.
 * This works just like Tor Browser — the browser connects through SOCKS,
 * and Tor handles .onion resolution and end-to-end encryption.
 *
 * Clearnet URLs always go DIRECT (normal browsing is never affected).
 * When OnionPress is not running, all traffic goes DIRECT.
 */

const STATUS_URL = "http://localhost:9077/status";
const NATIVE_HOST = "press.onion.onionpress";

// Detect Firefox (manifest v2 with proxy.onRequest)
const IS_FIREFOX = typeof browser !== "undefined" && browser.runtime && browser.runtime.getBrowserInfo;

let proxyRunning = false;
let onionAddress = null;
let nativePort = null;

// ---------------------------------------------------------------------------
// Proxy status polling
// ---------------------------------------------------------------------------

async function checkProxyStatus() {
  try {
    const resp = await fetch(STATUS_URL, { signal: AbortSignal.timeout(3000) });
    if (resp.ok) {
      const data = await resp.json();
      proxyRunning = data.running === true;
      onionAddress = data.onion_address || null;
      updateProxyConfig();
      return true;
    }
  } catch (_) {
    // proxy not reachable
  }
  proxyRunning = false;
  updateProxyConfig();
  return false;
}

setInterval(checkProxyStatus, 15000);
checkProxyStatus();

// ---------------------------------------------------------------------------
// Proxy configuration — route .onion through Tor SOCKS proxy
// ---------------------------------------------------------------------------

function updateProxyConfig() {
  if (IS_FIREFOX) {
    // Firefox uses browser.proxy.onRequest (registered once below)
    // The listener checks proxyRunning dynamically
    return;
  }

  // Chrome: PAC script routes only .onion through Tor SOCKS proxy
  if (proxyRunning) {
    chrome.proxy.settings.set({
      value: {
        mode: "pac_script",
        pacScript: {
          data: `function FindProxyForURL(url, host) {
            if (host.substring(host.length - 6) === ".onion") return "SOCKS5 127.0.0.1:9050";
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
// Firefox: proxy.onRequest — SOCKS for .onion, DIRECT for everything else
// ---------------------------------------------------------------------------

if (IS_FIREFOX) {
  browser.proxy.onRequest.addListener(
    (details) => {
      if (!proxyRunning) {
        return { type: "direct" };
      }
      const url = new URL(details.url);
      if (url.hostname.endsWith(".onion")) {
        return { type: "socks", host: "127.0.0.1", port: 9050, proxyDNS: true };
      }
      return { type: "direct" };
    },
    { urls: ["<all_urls>"] }
  );

  browser.proxy.onError.addListener((error) => {
    console.error("OnionPress proxy error:", error.message);
  });
}

// ---------------------------------------------------------------------------
// Native messaging (optional)
// ---------------------------------------------------------------------------

function connectNative() {
  try {
    const api = typeof browser !== "undefined" ? browser : chrome;
    nativePort = api.runtime.connectNative(NATIVE_HOST);
    nativePort.onMessage.addListener((msg) => {
      if (msg.onion_address) onionAddress = msg.onion_address;
      if (msg.running !== undefined) {
        proxyRunning = msg.running;
        updateProxyConfig();
      }
    });
    nativePort.onDisconnect.addListener(() => {
      nativePort = null;
      setTimeout(connectNative, 30000);
    });
    nativePort.postMessage({ type: "get_config" });
  } catch (_) {
    nativePort = null;
  }
}

connectNative();

// ---------------------------------------------------------------------------
// Handle messages from popup
// ---------------------------------------------------------------------------

const api = typeof browser !== "undefined" ? browser : chrome;
api.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "get_status") {
    checkProxyStatus().then(() => {
      sendResponse({ proxyRunning, onionAddress });
    });
    return true;
  }
});
