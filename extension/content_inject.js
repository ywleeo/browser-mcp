// Browser MCP XHS response observer — runs in MAIN world at document_start.
//
// Xiaohongshu's own client remains responsible for cookies, request signing,
// and transport. This observer clones response text only while preserving the
// native call's behavior for the page.

(() => {
  if (window.__browserMcpXhsObserverInstalled) return;
  window.__browserMcpXhsObserverInstalled = true;

  const BODY_LIMIT = 4 * 1024 * 1024;

  /** Relay one bounded response body to the isolated content-script bridge. */
  function postResponse(url, body, status) {
    try {
      const text = typeof body === "string" ? body : "";
      const bounded = text.length > BODY_LIMIT ? text.slice(0, BODY_LIMIT) : text;
      window.postMessage(
        {
          __browserMcp: "xhs-response",
          url,
          body: bounded,
          status,
          truncated: bounded.length !== text.length,
        },
        "*",
      );
    } catch {}
  }

  const nativeFetch = window.fetch;

  /** Preserve native fetch while asynchronously observing a cloned response. */
  async function observedFetch(input, init) {
    const requestUrl = typeof input === "string" ? input : input?.url;
    const response = await nativeFetch.apply(this, arguments);
    if (requestUrl) {
      try {
        const clone = response.clone();
        void clone.text().then((body) => postResponse(requestUrl, body, response.status));
      } catch {}
    }
    return response;
  }

  const NativeXhr = window.XMLHttpRequest;

  /** Construct a native XHR whose completed text response is observed read-only. */
  function ObservedXhr() {
    const xhr = new NativeXhr();
    let requestUrl = "";
    const nativeOpen = xhr.open;

    /** Retain the target URL while delegating every argument to native XHR. */
    xhr.open = function observedOpen(method, url) {
      requestUrl = String(url || "");
      return nativeOpen.apply(this, arguments);
    };
    xhr.addEventListener("load", () => {
      if (!requestUrl) return;
      try {
        postResponse(requestUrl, xhr.responseText, xhr.status);
      } catch {}
    });
    return xhr;
  }

  ObservedXhr.prototype = NativeXhr.prototype;
  window.fetch = observedFetch;
  window.XMLHttpRequest = ObservedXhr;
})();
