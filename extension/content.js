/**
 * OnionPress Content Script
 *
 * With the proxy API approach, .onion URLs route through the proxy
 * automatically. This script only needs to downgrade https .onion
 * links to http in the DOM so the browser sends them as proxy
 * requests rather than CONNECT tunnels.
 */

(function () {
  "use strict";

  const HTTPS_ONION_RE = /https:\/\/((?:[a-z0-9-]+\.)*[a-z0-9]{16,56}\.onion)/gi;

  function downgradeElement(el) {
    const href = el.getAttribute("href");
    if (href && HTTPS_ONION_RE.test(href)) {
      HTTPS_ONION_RE.lastIndex = 0;
      el.setAttribute("href", href.replace(HTTPS_ONION_RE, "http://$1"));
    }

    const src = el.getAttribute("src");
    if (src && HTTPS_ONION_RE.test(src)) {
      HTTPS_ONION_RE.lastIndex = 0;
      el.setAttribute("src", src.replace(HTTPS_ONION_RE, "http://$1"));
    }

    const action = el.getAttribute("action");
    if (action && HTTPS_ONION_RE.test(action)) {
      HTTPS_ONION_RE.lastIndex = 0;
      el.setAttribute("action", action.replace(HTTPS_ONION_RE, "http://$1"));
    }
  }

  function downgradeAll() {
    document.querySelectorAll("a[href], img[src], script[src], link[href], iframe[src], form[action]")
      .forEach(downgradeElement);
  }

  downgradeAll();

  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        downgradeElement(node);
        node.querySelectorAll?.("a[href], img[src], script[src], link[href], iframe[src], form[action]")
          ?.forEach(downgradeElement);
      }
    }
  });

  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
