/**
 * OnionPress Content Script
 *
 * Runs on pages served through the proxy (localhost:9077).
 * Rewrites any remaining .onion links so they route through the proxy too.
 * Also rewrites src, action, and srcset attributes.
 */

(function () {
  "use strict";

  const PROXY_BASE = "http://localhost:9077";
  const ONION_RE = /https?:\/\/([a-z2-7]{56}\.onion)(\/[^\s"'<>]*)?/gi;

  function toProxyUrl(match, host, path) {
    return `${PROXY_BASE}/proxy/${host.toLowerCase()}${path || "/"}`;
  }

  function rewriteElement(el) {
    // href (links, base)
    if (el.href && ONION_RE.test(el.getAttribute("href"))) {
      ONION_RE.lastIndex = 0;
      el.setAttribute("href", el.getAttribute("href").replace(ONION_RE, toProxyUrl));
    }

    // src (images, scripts, iframes)
    const src = el.getAttribute("src");
    if (src && ONION_RE.test(src)) {
      ONION_RE.lastIndex = 0;
      el.setAttribute("src", src.replace(ONION_RE, toProxyUrl));
    }

    // action (forms)
    const action = el.getAttribute("action");
    if (action && ONION_RE.test(action)) {
      ONION_RE.lastIndex = 0;
      el.setAttribute("action", action.replace(ONION_RE, toProxyUrl));
    }

    // srcset (responsive images)
    const srcset = el.getAttribute("srcset");
    if (srcset && ONION_RE.test(srcset)) {
      ONION_RE.lastIndex = 0;
      el.setAttribute("srcset", srcset.replace(ONION_RE, toProxyUrl));
    }
  }

  // Initial pass over existing elements
  function rewriteAll() {
    const selectors = "a[href], img[src], script[src], link[href], iframe[src], form[action], source[srcset], source[src]";
    document.querySelectorAll(selectors).forEach(rewriteElement);
  }

  rewriteAll();

  // Watch for dynamically added elements
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        rewriteElement(node);
        // Also check children
        const selectors = "a[href], img[src], script[src], link[href], iframe[src], form[action], source[srcset], source[src]";
        node.querySelectorAll?.(selectors)?.forEach(rewriteElement);
      }
    }
  });

  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
