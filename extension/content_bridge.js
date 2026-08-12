// Relay MAIN-world XHS response observations to the MV3 service worker.

/** Forward one trusted same-window observer event without retaining its body. */
function forwardObservedResponse(event) {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.__browserMcp !== "xhs-response") return;
  void chrome.runtime
    .sendMessage({
      type: "xhs-response",
      url: data.url,
      body: data.body,
      status: data.status,
      truncated: data.truncated === true,
    })
    .catch(() => {});
}

window.addEventListener("message", forwardObservedResponse);
