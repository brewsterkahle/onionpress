/**
 * OnionPress Background Service Worker
 *
 * Uses the browser's proxy API so .onion URLs stay in the address bar.
 * The browser sends .onion requests through our local proxy at localhost:9077
 * as standard HTTP proxy requests — no URL rewriting needed.
 *
 * Firefox (manifest v2): browser.proxy.onRequest
 * Chrome  (manifest v3): chrome.proxy.settings with PAC script
 *
 * HTTPS .onion URLs are downgraded to HTTP before proxying, since:
 * - Tor already provides end-to-end encryption
 * - HTTPS would require CONNECT tunneling which is more complex
 * - The extension intercepts HTTPS .onion requests and redirects to HTTP
 */

const PROXY_BASE = "http://localhost:9077";
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
    const resp = await fetch(`${PROXY_BASE}/status`, { signal: AbortSignal.timeout(3000) });
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
// Proxy configuration — route .onion through localhost:9077
// ---------------------------------------------------------------------------

function updateProxyConfig() {
  if (IS_FIREFOX) {
    // Firefox uses browser.proxy.onRequest (registered once below)
    // The listener checks proxyRunning dynamically
    return;
  }

  // Chrome: set/clear PAC script
  if (proxyRunning) {
    chrome.proxy.settings.set({
      value: {
        mode: "pac_script",
        pacScript: {
          data: `function FindProxyForURL(url, host) {
            if (host.substring(host.length - 6) === ".onion") return "PROXY 127.0.0.1:9077";
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
// Firefox: proxy.onRequest + HTTPS downgrade
// ---------------------------------------------------------------------------

if (IS_FIREFOX) {
  // Route .onion HTTP requests through our proxy
  browser.proxy.onRequest.addListener(
    (details) => {
      if (proxyRunning) {
        return { type: "http", host: "127.0.0.1", port: 9077 };
      }
      // Proxy not running — let it fail (shows browser error)
      return { type: "direct" };
    },
    { urls: ["http://*.onion/*"] }
  );

  browser.proxy.onError.addListener((error) => {
    console.error("OnionPress proxy error:", error.message);
  });

  // Downgrade HTTPS .onion → HTTP (safe: Tor encrypts the connection)
  browser.webRequest.onBeforeRequest.addListener(
    (details) => {
      if (details.url.startsWith("https://")) {
        return { redirectUrl: details.url.replace("https://", "http://") };
      }
      return {};
    },
    { urls: ["https://*.onion/*"] },
    ["blocking"]
  );
}

// ---------------------------------------------------------------------------
// Chrome: declarativeNetRequest for HTTPS downgrade + PAC proxy
// ---------------------------------------------------------------------------

if (!IS_FIREFOX) {
  // Add dynamic rule to downgrade https .onion to http
  chrome.declarativeNetRequest.updateDynamicRules({
    addRules: [{
      id: 1,
      priority: 1,
      action: {
        type: "redirect",
        redirect: {
          transform: { scheme: "http" }
        }
      },
      condition: {
        regexFilter: "^https://[^/]*\\.onion(/|$)",
        resourceTypes: [
          "main_frame", "sub_frame", "stylesheet", "script",
          "image", "font", "xmlhttprequest", "ping", "media", "other"
        ]
      }
    }],
    removeRuleIds: [1]
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
