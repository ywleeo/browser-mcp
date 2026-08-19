// Relay MAIN-world Douyin response observations to the MV3 service worker.

/** Forward one trusted same-window observer event without retaining its body. */
function forwardObservedDouyinResponse(event) {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.__browserMcp !== "douyin-response") return;
  void chrome.runtime
    .sendMessage({
      type: "douyin-response",
      url: data.url,
      body: data.body,
      status: data.status,
      truncated: data.truncated === true,
    })
    .catch(() => {});
}

window.addEventListener("message", forwardObservedDouyinResponse);
