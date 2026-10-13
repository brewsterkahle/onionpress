// offline.html: shown by background.js for a .onion page load while
// OnionPress is not running. ?url= is the page that was asked for.
// A separate file, not an inline <script>: the extension CSP forbids those.

const params = new URLSearchParams(window.location.search);
// chrome.*, not browser.*: Firefox takes callbacks only on chrome.*.
const api = chrome;

// Only an http(s) .onion URL goes back into a link: anything else
// (javascript:, a clearnet URL) is dropped.
function onionUrl(value) {
  try {
    const u = new URL(value);
    if ((u.protocol === "http:" || u.protocol === "https:") && /\.onion\.?$/i.test(u.hostname)) {
      return u.href;
    }
  } catch (_) {
    // not a URL
  }
  return null;
}

const originalUrl = onionUrl(params.get("url"));

if (originalUrl) {
  // Issue #99: the Wayback Machine may hold a copy. Only a link: following
  // it is the visitor's choice (it leaves Tor, which the page says).
  document.getElementById("wayback-link").href =
    "https://web.archive.org/web/" + originalUrl;
  document.getElementById("wayback").hidden = false;
}

function tryReconnect() {
  // background.js knows the proxy port (it differs per macOS user)
  api.runtime.sendMessage({ type: "get_status" }, (resp) => {
    if (api.runtime.lastError || !resp || !resp.proxyRunning) return;
    if (originalUrl) {
      window.location.href = originalUrl;
    } else {
      window.location.reload();
    }
  });
}

document.getElementById("retry-btn").addEventListener("click", (e) => {
  e.preventDefault();
  tryReconnect();
});

// Auto-retry every 10 seconds
setInterval(tryReconnect, 10000);
