/**
 * OnionPress Extension Popup
 *
 * Shows proxy status, the user's .onion address (if running),
 * or a "not running" message with a help link.
 */

(function () {
  "use strict";

  const dot = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");
  const content = document.getElementById("content");

  chrome.runtime.sendMessage({ type: "get_status" }, (resp) => {
    if (chrome.runtime.lastError || !resp) {
      showStopped();
      return;
    }

    if (resp.proxyRunning) {
      showRunning(resp.onionAddress);
    } else {
      showStopped();
    }
  });

  function showRunning(address) {
    dot.className = "status-dot running";
    statusText.textContent = "Running";

    if (address) {
      const addrDiv = document.createElement("div");
      addrDiv.className = "address";
      addrDiv.textContent = address;
      addrDiv.title = "Click to copy";
      addrDiv.addEventListener("click", () => {
        navigator.clipboard.writeText(address).then(() => {
          addrDiv.textContent = "Copied!";
          setTimeout(() => { addrDiv.textContent = address; }, 1500);
        });
      });
      content.appendChild(addrDiv);
    }
  }

  function showStopped() {
    dot.className = "status-dot stopped";
    statusText.textContent = "Not running";

    const div = document.createElement("div");
    div.className = "not-running";
    div.innerHTML = `
      <p>OnionPress is not running.<br>Launch it from your menu bar.</p>
      <a class="help-link" href="https://github.com/brewsterkahle/onionpress/releases/latest" target="_blank">
        Download OnionPress
      </a>
    `;
    content.appendChild(div);
  }
})();
