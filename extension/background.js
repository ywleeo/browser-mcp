// Browser MCP Bridge — authenticated fetch and visual-interaction worker.
//
// Read adapters use short-lived inactive tabs. Visual tools reuse one isolated background window,
// capture its active tab without focusing the window, and guard every navigation.

import { BUNDLE_BUILD_ID } from "./build-info.js";

const DEFAULT_BASE_PORT = 17880;
const DEFAULT_POOL_SIZE = 10;
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 15000;
// Public-DNS verification can use two proxy-safe DoH fallbacks before approval.
const URL_CHECK_TIMEOUT_MS = 20000;
const PAGE_LOAD_TIMEOUT_MS = 30000;
const HTML_LARGE_LIMIT = 4_000_000;
const HTML_SMALL_LIMIT = 256_000;
const TEXT_LIMIT = 4_000_000;
const XHR_BODY_LIMIT = 1_000_000;
const XHR_TOTAL_LIMIT = 2_500_000;
const SITE_ACTION_TIMEOUT_MS = 25000;
const INTERACTION_TEXT_LIMIT = 12000;
const INTERACTION_ELEMENT_LIMIT = 200;
const EXTENSION_VERSION = chrome.runtime.getManifest().version;
const sockets = new Map();
const pendingUrlChecks = new Map();
const pendingXhsSearches = new Map();
const pendingXhsComments = new Map();
const pendingXhsUserNotes = new Map();
const pendingDouyinReads = new Map();
const pendingDouyinComments = new Map();
const interactiveTabs = new Map();
const interactionWindows = new Map();
const interactionQueues = new Map();
const interactionDebuggers = new Map();
const lastScreenshotAt = new Map();

/** Load server-generated pairing data embedded in the unpacked directory. */
async function loadPairingConfig() {
  try {
    const response = await fetch(chrome.runtime.getURL("pairing.json"), { cache: "no-store" });
    if (!response.ok) throw new Error(`pairing config returned ${response.status}`);
    const config = await response.json();
    return {
      token: String(config.token || ""),
      buildId: String(config.build_id || ""),
      basePort: Number(config.base_port) || DEFAULT_BASE_PORT,
      poolSize: Number(config.pool_size) || DEFAULT_POOL_SIZE,
      path: String(config.path || "/browser-mcp-extension"),
    };
  } catch (error) {
    console.warn("[browser-mcp-extension] pairing config unavailable", error);
    return null;
  }
}

/** Reload only when pairing metadata points at a different on-disk bundle. */
async function reloadIfBundleChanged() {
  const config = await loadPairingConfig();
  if (config?.buildId && config.buildId !== BUNDLE_BUILD_ID) chrome.runtime.reload();
}

/** Return the inclusive local port set this extension should probe. */
function portsFor(config) {
  return Array.from({ length: config.poolSize }, (_, index) => config.basePort + index).filter(
    (port) => port > 0 && port <= 65535,
  );
}

/** Get or initialize the reconnect state associated with one local port. */
function stateFor(port) {
  let state = sockets.get(port);
  if (!state) {
    state = { port, socket: null, timer: null, delay: RECONNECT_MIN_MS, disabled: false };
    sockets.set(port, state);
  }
  return state;
}

/** Send JSON only while the socket remains open. */
function sendJson(state, value) {
  if (state.socket?.readyState !== WebSocket.OPEN) return;
  try {
    state.socket.send(JSON.stringify(value));
  } catch (error) {
    console.warn("[browser-mcp-extension] websocket send failed", error);
  }
}

/** Ask the Python URL policy to approve one top-level navigation or redirect. */
function requestUrlApproval(state, url) {
  return new Promise((resolve) => {
    const id = crypto.randomUUID();
    const timer = setTimeout(() => {
      pendingUrlChecks.delete(id);
      resolve({ allowed: false, error: "URL policy check timed out" });
    }, URL_CHECK_TIMEOUT_MS);
    pendingUrlChecks.set(id, { state, timer, resolve });
    sendJson(state, { type: "browser.url_check", id, url });
  });
}

/** Resolve a URL-policy callback sent by the authenticated Python bridge. */
function resolveUrlApproval(state, message) {
  const pending = pendingUrlChecks.get(message.id);
  if (!pending || pending.state !== state) return;
  clearTimeout(pending.timer);
  pendingUrlChecks.delete(message.id);
  pending.resolve({
    allowed: message.allowed === true,
    error: typeof message.error === "string" ? message.error : null,
  });
}

/** Fail URL checks tied to a socket that can no longer answer them. */
function failUrlChecksForState(state) {
  for (const [id, pending] of pendingUrlChecks.entries()) {
    if (pending.state !== state) continue;
    clearTimeout(pending.timer);
    pendingUrlChecks.delete(id);
    pending.resolve({ allowed: false, error: "Browser MCP bridge disconnected" });
  }
}

/** Remove only isolated interaction resources owned by one disconnected bridge port. */
async function cleanupBridgeSessionsForPort(port) {
  const prefix = `${port}:`;
  const sessions = new Set([
    ...[...interactiveTabs.keys()].filter((session) => session.startsWith(prefix)),
    ...[...interactionWindows.keys()].filter((session) => session.startsWith(prefix)),
    ...[...interactionDebuggers.keys()].filter((session) => session.startsWith(prefix)),
    ...[...interactionQueues.keys()].filter((session) => session.startsWith(prefix)),
  ]);
  for (const session of sessions) {
    const windowId = interactionWindows.get(session);
    interactiveTabs.delete(session);
    interactionWindows.delete(session);
    interactionQueues.delete(session);
    await closeInteractionDebugger(session);
    if (windowId != null) await chrome.windows.remove(windowId).catch(() => {});
  }
}

/** Schedule one bounded exponential-backoff reconnect. */
function scheduleReconnect(port, config, state) {
  if (state.disabled || state.timer) return;
  state.timer = setTimeout(() => {
    state.timer = null;
    connectPort(port, config);
  }, state.delay);
  state.delay = Math.min(state.delay * 2, RECONNECT_MAX_MS);
}

/** Connect and authenticate to one Browser MCP process. */
function connectPort(port, config) {
  const state = stateFor(port);
  state.disabled = false;
  if (
    state.socket &&
    (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }

  const socket = new WebSocket(`ws://127.0.0.1:${port}${config.path}`);
  state.socket = socket;

  socket.onopen = () => {
    state.delay = RECONNECT_MIN_MS;
    sendJson(state, {
      type: "hello",
      token: config.token,
      version: EXTENSION_VERSION,
      buildId: BUNDLE_BUILD_ID,
      extensionId: chrome.runtime.id,
      userAgent: navigator.userAgent,
      port,
    });
  };

  socket.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.type === "ping") {
      sendJson(state, { type: "pong", id: message.id || null });
    } else if (message.type === "browser.url_check.result") {
      resolveUrlApproval(state, message);
    } else if (message.type === "browser.fetch") {
      void dispatchBrowserFetch(state, message);
    } else if (message.type === "browser.interact") {
      scheduleBrowserInteraction(state, message);
    } else if (message.type === "zhihu.fetch") {
      void dispatchZhihuFetch(state, message);
    } else if (message.type === "bilibili.fetch") {
      void dispatchBilibiliFetch(state, message);
    } else if (message.type === "xhs.fetch") {
      void dispatchXhsFetch(state, message);
    } else if (message.type === "xhs.mutate") {
      void dispatchXhsMutation(state, message);
    } else if (message.type === "douyin.fetch") {
      void dispatchDouyinFetch(state, message);
    } else if (message.type === "douyin.mutate") {
      void dispatchDouyinMutation(state, message);
    } else if (message.type === "bridge.shutdown") {
      void cleanupBridgeSessionsForPort(port);
    } else if (message.type === "reload") {
      void reloadIfBundleChanged();
    }
  };

  socket.onclose = () => {
    if (state.socket !== socket) return;
    state.socket = null;
    failUrlChecksForState(state);
    void cleanupBridgeSessionsForPort(port);
    scheduleReconnect(port, config, state);
  };
  socket.onerror = () => {};
}

/** Serialize actions per managed tab so concurrent agents cannot attach twice or reorder input. */
function scheduleBrowserInteraction(state, message) {
  const session = `${state.port}:${String(message.tab_id || "default")}`;
  const previous = interactionQueues.get(session) || Promise.resolve();
  const current = previous
    .catch(() => {})
    .then(() => dispatchBrowserInteraction(state, message));
  interactionQueues.set(session, current);
  void current.finally(() => {
    if (interactionQueues.get(session) === current) interactionQueues.delete(session);
    if (state.socket?.readyState !== WebSocket.OPEN) {
      void cleanupBridgeSessionsForPort(state.port);
    }
  });
}

/** Wait for one tab to reach complete while bounding tracker-heavy pages. */
async function waitForTabComplete(tabId, timeoutMs = PAGE_LOAD_TIMEOUT_MS) {
  const tab = await chrome.tabs.get(tabId);
  if (tab.status === "complete") return;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error(`tab load timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId !== tabId || changeInfo.status !== "complete") return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
}

/** Clip a browser string and record explicit loss instead of overflowing the bridge. */
function clipString(value, limit, label, warnings) {
  const text = typeof value === "string" ? value : "";
  if (text.length <= limit) return text;
  warnings.push(`${label} truncated by extension: ${text.length - limit} characters omitted.`);
  return text.slice(0, limit);
}

/** Execute one read-only fetch request in a fresh inactive Chrome tab. */
async function dispatchBrowserFetch(state, message) {
  const url = message.args?.url;
  const extract = typeof message.args?.extract === "string" ? message.args.extract : "readability";
  const waitMs = Number.isFinite(message.args?.wait_ms)
    ? Math.max(0, Math.min(30000, Number(message.args.wait_ms)))
    : 800;
  const reply = (payload) =>
    sendJson(state, { type: "browser.fetch.result", id: message.id, ...payload });
  if (typeof url !== "string" || !/^https?:\/\//i.test(url)) {
    reply({ ok: false, error: "browser.fetch requires an absolute http(s) URL" });
    return;
  }
  if (!["readability", "text", "raw", "xhr"].includes(extract)) {
    reply({ ok: false, error: `unsupported extract mode: ${extract}` });
    return;
  }

  let tabId = null;
  let debuggerTarget = null;
  let debuggerListener = null;
  const guardTasks = new Set();
  let policyFailure = null;
  try {
    const tab = await chrome.tabs.create({ url: "about:blank", active: false });
    tabId = tab.id;
    if (tabId == null) throw new Error("Chrome did not create a fetch tab");
    debuggerTarget = { tabId };
    await chrome.debugger.attach(debuggerTarget, "1.3");
    await chrome.debugger.sendCommand(debuggerTarget, "Network.enable", {});
    await chrome.debugger.sendCommand(debuggerTarget, "Fetch.enable", {
      patterns: [{ urlPattern: "*", resourceType: "Document", requestStage: "Request" }],
    });

    const requests = new Map();
    const responses = new Map();
    debuggerListener = (source, method, params) => {
      if (!source || source.tabId !== tabId) return;
      if (method === "Fetch.requestPaused") {
        const task = (async () => {
          const approval = await requestUrlApproval(state, params.request?.url || "");
          if (approval.allowed) {
            await chrome.debugger.sendCommand(debuggerTarget, "Fetch.continueRequest", {
              requestId: params.requestId,
            });
          } else {
            policyFailure = approval.error || "navigation blocked by URL policy";
            await chrome.debugger.sendCommand(debuggerTarget, "Fetch.failRequest", {
              requestId: params.requestId,
              errorReason: "BlockedByClient",
            });
          }
        })().catch((error) => {
          policyFailure = `URL guard failed: ${error?.message || error}`;
        });
        guardTasks.add(task);
        void task.finally(() => guardTasks.delete(task));
      } else if (method === "Network.requestWillBeSent") {
        requests.set(params.requestId, {
          url: params.request?.url || "",
          method: params.request?.method || "GET",
        });
      } else if (method === "Network.responseReceived") {
        responses.set(params.requestId, {
          status: params.response?.status || 0,
          mime: params.response?.mimeType || "",
          type: params.type || "",
        });
      }
    };
    chrome.debugger.onEvent.addListener(debuggerListener);
    await chrome.tabs.update(tabId, { url });

    let loadTimedOut = false;
    try {
      await waitForTabComplete(tabId);
    } catch (error) {
      if (!String(error?.message || error).includes("tab load timeout")) throw error;
      loadTimedOut = true;
    }
    await Promise.all([...guardTasks]);
    if (policyFailure) throw new Error(`navigation blocked: ${policyFailure}`);
    if (waitMs > 0) await new Promise((resolve) => setTimeout(resolve, waitMs));

    const htmlLimit = extract === "readability" || extract === "raw"
      ? HTML_LARGE_LIMIT
      : HTML_SMALL_LIMIT;
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [extract, htmlLimit, TEXT_LIMIT],
      func: (mode, maxHtml, maxText) => {
        const warnings = [];
        const clip = (value, limit, label) => {
          const text = typeof value === "string" ? value : "";
          if (text.length <= limit) return text;
          warnings.push(`${label} truncated by extension: ${text.length - limit} characters omitted.`);
          return text.slice(0, limit);
        };
        const output = {
          final_url: location.href,
          html: mode === "readability" || mode === "raw"
            ? clip(document.documentElement?.outerHTML || "", maxHtml, "Rendered HTML")
            : (document.documentElement?.outerHTML || "").slice(0, maxHtml),
          load_timed_out: false,
          warnings,
        };
        if (mode === "text") {
          output.text = clip(document.body?.innerText || "", maxText, "Visible text");
        }
        return output;
      },
    });
    if (!result || typeof result.html !== "string" || typeof result.final_url !== "string") {
      throw new Error("Chrome script returned no rendered document");
    }
    result.load_timed_out = loadTimedOut;
    if (extract === "xhr") {
      const capture = await collectXhrBodies(debuggerTarget, requests, responses);
      result.xhr = capture.entries;
      result.warnings.push(...capture.warnings);
    }
    reply({ ok: true, data: result });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (debuggerListener) chrome.debugger.onEvent.removeListener(debuggerListener);
    if (debuggerTarget) {
      try {
        await chrome.debugger.detach(debuggerTarget);
      } catch {}
    }
    if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Collect bounded textual XHR/Fetch bodies after the page has settled. */
async function collectXhrBodies(debuggerTarget, requests, responses) {
  const entries = [];
  const warnings = [];
  let totalCharacters = 0;
  for (const [requestId, metadata] of responses) {
    if (metadata.type !== "XHR" && metadata.type !== "Fetch") continue;
    const request = requests.get(requestId) || {};
    const entry = {
      url: request.url || "",
      method: request.method || "GET",
      status: metadata.status,
      mime: metadata.mime,
      type: metadata.type,
      body: null,
    };
    try {
      const response = await chrome.debugger.sendCommand(
        debuggerTarget,
        "Network.getResponseBody",
        { requestId },
      );
      if (!response?.base64Encoded) {
        const remaining = Math.max(0, XHR_TOTAL_LIMIT - totalCharacters);
        const limit = Math.min(XHR_BODY_LIMIT, remaining);
        entry.body = clipString(response?.body || "", limit, `XHR ${entry.url}`, warnings);
        totalCharacters += entry.body.length;
      }
    } catch (error) {
      warnings.push(`XHR body unavailable for ${entry.url}: ${error?.message || error}`);
    }
    entries.push(entry);
    if (totalCharacters >= XHR_TOTAL_LIMIT) {
      warnings.push("XHR capture reached the extension total-body limit; later bodies were omitted.");
      break;
    }
  }
  return { entries, warnings };
}

/** Dispatch one allowlisted Bilibili read adapter through the current Chrome session. */
async function dispatchBilibiliFetch(state, message) {
  const reply = (payload) =>
    sendJson(state, { type: "bilibili.fetch.result", id: message.id, ...payload });
  if (message.action === "search") {
    await runBilibiliSearch(state, message.args || {}, reply);
  } else if (message.action === "video") {
    await runBilibiliVideo(state, message.args || {}, false, reply);
  } else if (message.action === "media") {
    await runBilibiliVideo(state, message.args || {}, true, reply);
  } else {
    reply({ ok: false, error: `unsupported bilibili action: ${message.action}` });
  }
}

/** Search Bilibili videos through its public API inside a short-lived Chrome tab. */
async function runBilibiliSearch(state, args, reply) {
  const keyword = String(args.keyword || "").trim();
  if (!keyword) {
    reply({ ok: false, error: "bilibili search keyword is required" });
    return;
  }
  const page = Math.max(1, Math.min(50, Number(args.page) || 1));
  const supportedOrders = new Set(["totalrank", "click", "pubdate", "dm", "stow"]);
  const order = supportedOrders.has(String(args.order || ""))
    ? String(args.order)
    : "totalrank";
  const apiTarget = new URL("https://api.bilibili.com/x/web-interface/search/type");
  apiTarget.searchParams.set("search_type", "video");
  apiTarget.searchParams.set("keyword", keyword);
  apiTarget.searchParams.set("page", String(page));
  apiTarget.searchParams.set("order", order);
  const pageTarget = new URL("https://search.bilibili.com/video");
  pageTarget.searchParams.set("keyword", keyword);
  pageTarget.searchParams.set("page", String(page));
  pageTarget.searchParams.set("order", order);
  const [apiApproval, pageApproval] = await Promise.all([
    requestUrlApproval(state, apiTarget.toString()),
    requestUrlApproval(state, pageTarget.toString()),
  ]);
  if (!apiApproval.allowed || !pageApproval.allowed) {
    const detail = apiApproval.error || pageApproval.error || "URL policy";
    reply({ ok: false, error: `navigation blocked: ${detail}` });
    return;
  }

  let tabId = null;
  try {
    const tab = await chrome.tabs.create({ url: apiTarget.toString(), active: false });
    tabId = tab.id;
    if (tabId == null) throw new Error("Chrome did not create a Bilibili search tab");
    await waitForTabComplete(tabId);
    let result = await readBilibiliSearchApiTab(tabId);
    if (!result?.data || result.data.code !== 0) {
      await chrome.tabs.update(tabId, { url: pageTarget.toString() });
      await waitForTabComplete(tabId);
      await new Promise((resolve) => setTimeout(resolve, 1200));
      result = await readBilibiliSearchPageTab(tabId, page);
    }
    if (!result?.data) throw new Error(result?.error || "Bilibili search returned no data");
    reply({ ok: true, data: result.data });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Decode one direct Bilibili search API tab without trusting non-JSON error pages. */
async function readBilibiliSearchApiTab(tabId) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: () => {
      const text = document.querySelector("pre")?.textContent || document.body?.innerText || "";
      try {
        return { data: JSON.parse(text) };
      } catch (error) {
        return { error: `Bilibili search JSON could not be decoded: ${error?.message || error}` };
      }
    },
  });
  return result || { error: "Bilibili search API returned no script result" };
}

/** Normalize rendered Bilibili search cards when the public API applies HTTP 412 throttling. */
async function readBilibiliSearchPageTab(tabId, page) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [page],
    func: (selectedPage) => {
      /** Parse one compact rendered statistic such as 17.1万 without locale globals. */
      const count = (value) => {
        const normalized = String(value || "").replace(/[,\s]/g, "");
        const match = normalized.match(/([0-9]+(?:\.[0-9]+)?)(万|亿)?/);
        if (!match) return 0;
        const multiplier = match[2] === "亿" ? 100000000 : match[2] === "万" ? 10000 : 1;
        return Math.round(Number(match[1]) * multiplier);
      };
      const cards = [...document.querySelectorAll(".bili-video-card")];
      const items = cards.flatMap((card) => {
        const anchor = card.querySelector('a[href*="/video/BV"]');
        const href = anchor instanceof HTMLAnchorElement ? anchor.href : "";
        const identity = href.match(/\/video\/(BV[0-9A-Za-z]{10})/);
        if (!identity) return [];
        const titleNode = card.querySelector(".bili-video-card__info--tit, h3[title]");
        const authorNode = card.querySelector(
          ".bili-video-card__info--author, .bili-video-card__info--owner",
        );
        const authorLink = authorNode?.closest("a[href]") || authorNode?.querySelector("a[href]");
        const authorHref = authorLink instanceof HTMLAnchorElement ? authorLink.href : "";
        const authorId = Number(authorHref.match(/space\.bilibili\.com\/(\d+)/)?.[1] || 0);
        const statNodes = [...card.querySelectorAll(".bili-video-card__stats--item")];
        const image = card.querySelector("img");
        const imageUrl = image instanceof HTMLImageElement
          ? image.currentSrc || image.src || image.getAttribute("data-src") || ""
          : "";
        return [{
          bvid: identity[1],
          aid: 0,
          title: titleNode?.getAttribute("title") || titleNode?.textContent?.trim() || "",
          description: "",
          author: authorNode?.textContent?.replace(/\s*[·•].*$/, "").trim() || "",
          mid: authorId,
          typename: "",
          duration: card.querySelector(".bili-video-card__stats__duration")?.textContent?.trim() || "",
          pubdate: 0,
          play: count(statNodes[0]?.textContent),
          danmaku: count(statNodes[1]?.textContent),
          favorites: 0,
          review: 0,
          like: 0,
          pic: imageUrl.replace(/^http:/, "https:").split("@")[0],
          tag: "",
        }];
      });
      const pageNumbers = [...document.querySelectorAll(".vui_pagenation--btn-num")]
        .map((node) => Number(node.textContent?.trim()))
        .filter((value) => Number.isFinite(value));
      const totalPages = Math.max(selectedPage, ...pageNumbers);
      if (!items.length) return { error: "Bilibili rendered search page exposed no video cards" };
      return {
        data: {
          code: 0,
          message: "rendered fallback",
          data: {
            numResults: (selectedPage - 1) * 20 + items.length,
            numPages: totalPages,
            result: items,
          },
        },
      };
    },
  });
  return result || { error: "Bilibili rendered search returned no script result" };
}

/** Read Bilibili metadata and optional DASH playinfo for one selected multipart page. */
async function runBilibiliVideo(state, args, includePlayinfo, reply) {
  const videoId = String(args.videoId || "").trim();
  if (!/^(?:BV[0-9A-Za-z]{10}|av\d+)$/.test(videoId)) {
    reply({ ok: false, error: "bilibili videoId must be one canonical BV or AV id" });
    return;
  }
  const page = Math.max(1, Math.min(1000, Number(args.page) || 1));
  const target = new URL(`https://www.bilibili.com/video/${videoId}/`);
  if (page > 1) target.searchParams.set("p", String(page));
  const approval = await requestUrlApproval(state, target.toString());
  if (!approval.allowed) {
    reply({ ok: false, error: `navigation blocked: ${approval.error || "URL policy"}` });
    return;
  }

  let tabId = null;
  try {
    const tab = await chrome.tabs.create({ url: target.toString(), active: false });
    tabId = tab.id;
    if (tabId == null) throw new Error("Chrome did not create a Bilibili video tab");
    await waitForTabComplete(tabId);
    await new Promise((resolve) => setTimeout(resolve, 800));
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [videoId, includePlayinfo],
      func: async (requestedId, wantsPlayinfo) => {
        const query = requestedId.startsWith("BV")
          ? `bvid=${encodeURIComponent(requestedId)}`
          : `aid=${encodeURIComponent(requestedId.slice(2))}`;
        const fetchJson = async (url) => {
          const response = await fetch(url, { credentials: "include" });
          if (!response.ok) throw new Error(`${url} returned HTTP ${response.status}`);
          return response.json();
        };
        try {
          const [view, tags] = await Promise.all([
            fetchJson(`https://api.bilibili.com/x/web-interface/view?${query}`),
            fetchJson(`https://api.bilibili.com/x/tag/archive/tags?${query}`).catch(() => ({
              code: -1,
              data: [],
            })),
          ]);
          let playinfo = null;
          if (wantsPlayinfo) {
            for (let attempt = 0; attempt < 20; attempt += 1) {
              playinfo = window.__playinfo__ || null;
              if (playinfo?.data?.dash || playinfo?.data?.durl) break;
              await new Promise((resolve) => setTimeout(resolve, 250));
            }
            if (!playinfo?.data?.dash && !playinfo?.data?.durl) {
              throw new Error("Bilibili page exposed no downloadable playinfo");
            }
          }
          return { data: { view, tags, ...(wantsPlayinfo ? { playinfo } : {}) } };
        } catch (error) {
          return { error: String(error?.message || error) };
        }
      },
    });
    if (!result || result.error || !result.data) {
      throw new Error(result?.error || "Bilibili video returned no data");
    }
    reply({ ok: true, data: result.data });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Execute one visual action and always return the resulting screenshot and element map. */
async function dispatchBrowserInteraction(state, message) {
  const action = String(message.action || "");
  const args = message.args || {};
  const session = `${state.port}:${String(message.tab_id || "default")}`;
  const reply = (payload) =>
    sendJson(state, { type: "browser.interact.result", id: message.id, ...payload });
  if (!["snapshot", "click", "scroll", "type", "press", "select"].includes(action)) {
    reply({ ok: false, error: `unsupported browser interaction action: ${action}` });
    return;
  }

  let debuggerSession = null;
  let stage = "resolve tab";
  try {
    const tab = await interactionTab(state, message.tab_id, action, args.url);
    const tabId = tab.id;
    if (tabId == null) throw new Error("interactive Chrome tab has no id");
    const currentUrl = String(tab.url || "");
    if (
      currentUrl
      && currentUrl !== "about:blank"
    ) {
      const currentApproval = await requestUrlApproval(state, currentUrl);
      if (!currentApproval.allowed) {
        throw new Error(`current page blocked: ${currentApproval.error || "URL policy"}`);
      }
    }

    const navigationTarget = await interactionNavigationTarget(tabId, action, args);
    if (navigationTarget) {
      const approval = await requestUrlApproval(state, navigationTarget);
      if (!approval.allowed) {
        throw new Error(`action target blocked: ${approval.error || "URL policy"}`);
      }
    }
    const currentTab = await chrome.tabs.get(tabId);
    const needsDebugger = (
      action === "snapshot"
      && args.url != null
      && String(currentTab.url || "") !== String(args.url)
    ) || (action !== "snapshot" && navigationTarget != null);
    if (needsDebugger) {
      stage = "attach debugger";
      debuggerSession = await interactionDebuggerSession(state, session, tabId);
      debuggerSession.policyFailure = null;
    }

    stage = "execute action";
    await executeInteractionAction(tabId, action, args);
    stage = "wait for page";
    await settleInteractionTab(tabId, boundedWait(args.wait_ms, action === "snapshot" ? 500 : 300));
    if (debuggerSession) await Promise.all([...debuggerSession.guardTasks]);
    if (debuggerSession?.policyFailure) {
      throw new Error(`navigation blocked: ${debuggerSession.policyFailure}`);
    }

    stage = "validate resulting page";
    const latestTab = await chrome.tabs.get(tabId);
    const finalUrl = String(latestTab.url || "");
    const finalApproval = await requestUrlApproval(state, finalUrl);
    if (!finalApproval.allowed) {
      throw new Error(`resulting page blocked: ${finalApproval.error || "URL policy"}`);
    }
    stage = "capture resulting state";
    if (debuggerSession) {
      await closeInteractionDebugger(session);
      debuggerSession = null;
    }
    const screenshotData = await captureInteractionScreenshot(tabId, session);
    const visual = await captureInteractionState(tabId, action, screenshotData);
    reply({ ok: true, data: visual });
  } catch (error) {
    if (debuggerSession) await closeInteractionDebugger(session);
    reply({ ok: false, error: `${action} failed during ${stage}: ${error?.message || error}` });
  }
}

/** Return the explicit anchor or form destination associated with one interaction. */
async function interactionNavigationTarget(tabId, action, args) {
  if (action === "snapshot" && typeof args.url === "string") return args.url;
  const maySubmit = action === "click"
    || (action === "type" && args.submit === true)
    || (action === "press" && args.key === "Enter");
  if (!maySubmit) return null;
  const elementId = typeof args.element_id === "string" ? args.element_id : null;
  const x = Number.isFinite(args.x) ? Number(args.x) : null;
  const y = Number.isFinite(args.y) ? Number(args.y) : null;
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [elementId, x, y, action],
    func: (targetId, clickX, clickY, actionName) => {
      const escaped = targetId ? CSS.escape(targetId) : null;
      const element = escaped
        ? document.querySelector(`[data-browser-mcp-ref="${escaped}"]`)
        : Number.isFinite(clickX) && Number.isFinite(clickY)
          ? document.elementFromPoint(clickX, clickY)
          : null;
      if (!(element instanceof Element)) return null;
      const anchor = element.closest("a[href]");
      if (anchor instanceof HTMLAnchorElement && /^https?:/i.test(anchor.href)) return anchor.href;
      const submitsForm = actionName !== "click"
        || element instanceof HTMLButtonElement && element.type === "submit"
        || element instanceof HTMLInputElement && ["submit", "image"].includes(element.type);
      if (!submitsForm) return null;
      const form = element.closest("form");
      if (form instanceof HTMLFormElement && /^https?:/i.test(form.action)) return form.action;
      return null;
    },
  });
  return typeof result === "string" ? result : null;
}

/** Return one session-owned debugger that guards navigations for the tab's lifetime. */
async function interactionDebuggerSession(state, session, tabId) {
  const existing = interactionDebuggers.get(session);
  if (existing?.target?.tabId === tabId) return existing;
  if (existing) await closeInteractionDebugger(session);
  const target = await attachInteractionDebugger(tabId);
  const debuggerSession = {
    target,
    guardTasks: new Set(),
    policyFailure: null,
    listener: null,
  };
  debuggerSession.listener = (source, method, params) => {
    if (!source || source.tabId !== tabId || method !== "Fetch.requestPaused") return;
    const task = (async () => {
      const approval = await requestUrlApproval(state, params.request?.url || "");
      if (approval.allowed) {
        await chrome.debugger.sendCommand(target, "Fetch.continueRequest", {
          requestId: params.requestId,
        });
      } else {
        debuggerSession.policyFailure = approval.error || "navigation blocked by URL policy";
        await chrome.debugger.sendCommand(target, "Fetch.failRequest", {
          requestId: params.requestId,
          errorReason: "BlockedByClient",
        });
      }
    })().catch((error) => {
      debuggerSession.policyFailure = `URL guard failed: ${error?.message || error}`;
    });
    debuggerSession.guardTasks.add(task);
    void task.finally(() => debuggerSession.guardTasks.delete(task));
  };
  chrome.debugger.onEvent.addListener(debuggerSession.listener);
  interactionDebuggers.set(session, debuggerSession);
  return debuggerSession;
}

/** Release one persistent interaction debugger and its navigation listener. */
async function closeInteractionDebugger(session) {
  const debuggerSession = interactionDebuggers.get(session);
  if (!debuggerSession) return;
  interactionDebuggers.delete(session);
  if (debuggerSession.listener) chrome.debugger.onEvent.removeListener(debuggerSession.listener);
  try {
    await chrome.debugger.sendCommand(debuggerSession.target, "Fetch.disable", {});
  } catch {}
  try {
    await chrome.debugger.detach(debuggerSession.target);
  } catch {}
}

/** Attach and initialize CDP atomically, retrying Chrome's short detach handoff window. */
async function attachInteractionDebugger(tabId) {
  const target = { tabId };
  let lastError = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      await chrome.debugger.attach(target, "1.3");
      await chrome.debugger.sendCommand(target, "Page.enable", {});
      await chrome.debugger.sendCommand(target, "Fetch.enable", {
        patterns: [{ urlPattern: "*", resourceType: "Document", requestStage: "Request" }],
      });
      return target;
    } catch (error) {
      lastError = error;
      try {
        await chrome.debugger.detach(target);
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, attempt * 150));
    }
  }
  throw new Error(`unable to attach Chrome interaction debugger: ${lastError?.message || lastError}`);
}

/** Resolve or create the one managed interaction tab for a bridge session. */
async function interactionTab(state, requestedSession, action, requestedUrl) {
  const session = `${state.port}:${String(requestedSession || "default")}`;
  const existingTabId = interactiveTabs.get(session);
  if (existingTabId != null) {
    try {
      const existing = await chrome.tabs.get(existingTabId);
      const isolatedWindowId = interactionWindows.get(session);
      const needsIsolatedTab = typeof requestedUrl === "string"
        && isolatedWindowId !== existing.windowId;
      if (
        /^https?:\/\//i.test(String(existing.url || ""))
        && !needsIsolatedTab
      ) return existing;
      if (needsIsolatedTab) {
        interactiveTabs.delete(session);
        await closeInteractionDebugger(session);
      } else if (!(action === "snapshot" && typeof requestedUrl === "string")) {
        throw new Error("managed tab is no longer on a public webpage; provide url to browser_snapshot");
      } else {
        interactiveTabs.delete(session);
        interactionWindows.delete(session);
        await closeInteractionDebugger(session);
        if (isolatedWindowId === existing.windowId) {
          await chrome.tabs.remove(existingTabId).catch(() => {});
        }
      }
    } catch {
      interactiveTabs.delete(session);
      interactionWindows.delete(session);
    }
  }
  if (action !== "snapshot") {
    throw new Error("no interactive tab; call browser_snapshot before browser actions");
  }
  if (typeof requestedUrl === "string" && /^https?:\/\//i.test(requestedUrl)) {
    const createdWindow = await chrome.windows.create({
      url: "about:blank",
      focused: false,
      type: "normal",
    });
    const [created] = createdWindow.tabs || [];
    if (created?.id == null || createdWindow.id == null) {
      throw new Error("Chrome did not create an isolated interaction window");
    }
    interactiveTabs.set(session, created.id);
    interactionWindows.set(session, createdWindow.id);
    return created;
  }
  const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (active?.id == null || !/^https?:\/\//i.test(String(active.url || ""))) {
    throw new Error("no public webpage is active; provide url to browser_snapshot")
  }
  interactiveTabs.set(session, active.id);
  interactionWindows.delete(session);
  return active;
}

/** Execute the requested action while the debugger navigation guard is attached. */
async function executeInteractionAction(tabId, action, args) {
  if (action === "snapshot") {
    if (args.url != null) {
      const url = String(args.url);
      if (!/^https?:\/\//i.test(url)) throw new Error("browser_snapshot requires an http(s) URL");
      const current = await chrome.tabs.get(tabId);
      if (String(current.url || "") !== url) {
        await chrome.tabs.update(tabId, { url });
      }
    }
    return;
  }
  if (action === "click") {
    await executeDomClick(tabId, args);
    return;
  }
  if (action === "scroll") {
    await executeScroll(tabId, args);
    return;
  }
  if (action === "type") {
    try {
      await enterText(
        tabId,
        String(args.element_id || ""),
        typeof args.text === "string" ? args.text : "",
        args.clear !== false,
      );
    } catch (error) {
      throw new Error(`enter text: ${error?.message || error}`);
    }
    if (args.submit === true) {
      try {
        await executeDomPress(tabId, "Enter", String(args.element_id || ""));
      } catch (error) {
        throw new Error(`submit text: ${error?.message || error}`);
      }
    }
    return;
  }
  if (action === "press") {
    await executeDomPress(tabId, String(args.key || ""), args.element_id || null);
    return;
  }
  if (action === "select") {
    await executeSelect(tabId, String(args.element_id || ""), String(args.value || ""));
  }
}

/** Click a referenced or coordinate-located element through its native DOM activation path. */
async function executeDomClick(tabId, args) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [args.element_id || null, args.x ?? null, args.y ?? null],
    func: (elementId, x, y) => {
      let element = null;
      if (elementId) {
        const escaped = CSS.escape(elementId);
        element = document.querySelector(`[data-browser-mcp-ref="${escaped}"]`);
        if (!element) return { error: `element reference is stale: ${elementId}; take a new snapshot` };
        element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
      } else if (Number.isFinite(x) && Number.isFinite(y)) {
        if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) {
          return { error: `click coordinate is outside viewport ${innerWidth}x${innerHeight}` };
        }
        const referenced = [...document.querySelectorAll("[data-browser-mcp-ref]")]
          .filter((candidate) => {
            const rect = candidate.getBoundingClientRect();
            return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
          })
          .sort((left, right) => {
            const leftRect = left.getBoundingClientRect();
            const rightRect = right.getBoundingClientRect();
            return leftRect.width * leftRect.height - rightRect.width * rightRect.height;
          });
        const hit = document.elementFromPoint(x, y);
        element = referenced[0] || hit?.closest(
          "button,input,textarea,select,a[href],label,[role='button'],[role='link'],[onclick]",
        ) || hit;
        if (!element) return { error: `no element found at viewport coordinate ${x},${y}` };
      } else {
        return { error: "browser_click requires element_id or non-negative x and y" };
      }
      if (("disabled" in element && element.disabled) || element.getAttribute("aria-disabled") === "true") {
        return { error: `element is disabled: ${elementId || `${x},${y}`}` };
      }
      if (element instanceof HTMLElement) {
        element.focus({ preventScroll: true });
        element.click();
      } else {
        element.dispatchEvent(new MouseEvent("click", {
          bubbles: true,
          cancelable: true,
          composed: true,
          view: window,
        }));
      }
      return { ok: true };
    },
  });
  if (!result || result.error) throw new Error(result?.error || "click failed");
}

/** Apply one bounded keyboard behavior and dispatch matching DOM keyboard events. */
async function executeDomPress(tabId, key, elementId) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [key, elementId],
    func: (pressedKey, targetId) => {
      const supported = new Set([
        "Enter", "Escape", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
        "PageUp", "PageDown", "Home", "End", "Backspace", "Delete", " ",
      ]);
      if (!supported.has(pressedKey)) return { error: `unsupported browser key: ${pressedKey}` };
      let element = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      if (targetId) {
        const escaped = CSS.escape(targetId);
        const referenced = document.querySelector(`[data-browser-mcp-ref="${escaped}"]`);
        if (!(referenced instanceof HTMLElement)) {
          return { error: `element reference is stale: ${targetId}; take a new snapshot` };
        }
        element = referenced;
        element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
        element.focus();
      }
      const target = element || document.body;
      target.dispatchEvent(new KeyboardEvent("keydown", {
        key: pressedKey,
        bubbles: true,
        cancelable: true,
        composed: true,
      }));
      if (pressedKey === "Enter") {
        const form = target.closest("form");
        if (form instanceof HTMLFormElement) form.requestSubmit();
        else if (target instanceof HTMLElement) target.click();
      } else if (pressedKey === "Tab") {
        const focusable = [...document.querySelectorAll(
          "a[href],button,input,textarea,select,[tabindex]:not([tabindex='-1'])",
        )].filter((candidate) => candidate instanceof HTMLElement && !candidate.hidden);
        const index = focusable.indexOf(target);
        const next = focusable[(index + 1 + focusable.length) % focusable.length];
        if (next instanceof HTMLElement) next.focus();
      } else if (["PageUp", "PageDown", "Home", "End"].includes(pressedKey)) {
        const top = pressedKey === "Home" ? 0
          : pressedKey === "End" ? document.documentElement.scrollHeight
          : scrollY + (pressedKey === "PageUp" ? -innerHeight * 0.8 : innerHeight * 0.8);
        window.scrollTo({ top, behavior: "instant" });
      }
      target.dispatchEvent(new KeyboardEvent("keyup", {
        key: pressedKey,
        bubbles: true,
        cancelable: true,
        composed: true,
      }));
      return { ok: true };
    },
  });
  if (!result || result.error) throw new Error(result?.error || "key press failed");
}

/** Scroll relatively in CSS pixels or bring one referenced element into view. */
async function executeScroll(tabId, args) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [args.element_id || null, String(args.direction || "down"), Number(args.amount) || 600],
    func: (elementId, direction, amount) => {
      if (elementId) {
        const escaped = CSS.escape(elementId);
        const element = document.querySelector(`[data-browser-mcp-ref="${escaped}"]`);
        if (!element) return { error: `element reference is stale: ${elementId}; take a new snapshot` };
        element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
        return { ok: true };
      }
      const deltas = {
        up: [0, -amount],
        down: [0, amount],
        left: [-amount, 0],
        right: [amount, 0],
      };
      const [left, top] = deltas[direction] || deltas.down;
      window.scrollBy({ left, top, behavior: "instant" });
      return { ok: true };
    },
  });
  if (!result || result.error) throw new Error(result?.error || "scroll failed");
}

/** Enter text through native setters and standard input events used by controlled forms. */
async function enterText(tabId, elementId, text, clear) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [elementId, text, clear],
    func: (targetId, insertedText, shouldClear) => {
      const escaped = CSS.escape(targetId);
      const element = document.querySelector(`[data-browser-mcp-ref="${escaped}"]`);
      if (!(element instanceof HTMLElement)) {
        return { error: `element reference is stale: ${targetId}; take a new snapshot` };
      }
      const editable = element instanceof HTMLInputElement
        || element instanceof HTMLTextAreaElement
        || element.isContentEditable;
      if (!editable) return { error: `element is not editable: ${targetId}` };
      if (element instanceof HTMLInputElement && element.disabled) {
        return { error: `element is disabled: ${targetId}` };
      }
      element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
      element.focus();
      if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
        const oldValue = element.value;
        const nextValue = shouldClear ? insertedText : oldValue + insertedText;
        element.dispatchEvent(new InputEvent("beforeinput", {
          bubbles: true,
          composed: true,
          cancelable: true,
          data: insertedText,
          inputType: "insertText",
        }));
        const prototype = element instanceof HTMLInputElement
          ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
        if (setter) setter.call(element, nextValue);
        else element.value = nextValue;
        element.dispatchEvent(new InputEvent("input", {
          bubbles: true,
          composed: true,
          data: insertedText,
          inputType: "insertText",
        }));
        element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
        const position = nextValue.length;
        element.setSelectionRange(position, position);
      } else {
        element.dispatchEvent(new InputEvent("beforeinput", {
          bubbles: true,
          composed: true,
          cancelable: true,
          data: insertedText,
          inputType: "insertText",
        }));
        element.textContent = shouldClear ? insertedText : (element.textContent || "") + insertedText;
        element.dispatchEvent(new InputEvent("input", {
          bubbles: true,
          composed: true,
          data: insertedText,
          inputType: "insertText",
        }));
        const selection = getSelection();
        const range = document.createRange();
        range.selectNodeContents(element);
        range.collapse(false);
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
      return { ok: true };
    },
  });
  if (!result || result.error) throw new Error(result?.error || "text target unavailable");
}

/** Select a native option by exact value first, then by visible label. */
async function executeSelect(tabId, elementId, requestedValue) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [elementId, requestedValue],
    func: (targetId, desired) => {
      const escaped = CSS.escape(targetId);
      const element = document.querySelector(`[data-browser-mcp-ref="${escaped}"]`);
      if (!(element instanceof HTMLSelectElement)) {
        return { error: `element is not a select: ${targetId}` };
      }
      const option = [...element.options].find(
        (candidate) => candidate.value === desired || candidate.text.trim() === desired,
      );
      if (!option) return { error: `select option not found: ${desired}` };
      element.value = option.value;
      element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      return { ok: true };
    },
  });
  if (!result || result.error) throw new Error(result?.error || "select failed");
}

/** Wait for top-level navigation when present, then allow bounded SPA rendering time. */
async function settleInteractionTab(tabId, waitMs) {
  try {
    await waitForTabComplete(tabId);
  } catch (error) {
    if (!String(error?.message || error).includes("tab load timeout")) throw error;
  }
  if (waitMs > 0) await new Promise((resolve) => setTimeout(resolve, waitMs));
}

/** Clamp agent-controlled rendering waits to the documented interaction limit. */
function boundedWait(value, fallback) {
  if (!Number.isFinite(value)) return fallback;
  return Math.max(0, Math.min(30000, Number(value)));
}

/** Capture the isolated window without focusing it or changing the user's selected tab. */
async function captureInteractionScreenshot(tabId, session) {
  const tab = await chrome.tabs.get(tabId);
  if (tab.windowId == null) throw new Error("interactive tab has no window");
  if (tab.active !== true) {
    if (interactionWindows.get(session) !== tab.windowId) {
      throw new Error("refusing to activate an interaction tab in the user's current window");
    }
    await chrome.tabs.update(tabId, { active: true });
  }
  const elapsed = Date.now() - (lastScreenshotAt.get(tab.windowId) || 0);
  if (elapsed < 600) {
    await new Promise((resolve) => setTimeout(resolve, 600 - elapsed));
  }
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
    format: "jpeg",
    quality: 80,
  });
  lastScreenshotAt.set(tab.windowId, Date.now());
  const separator = dataUrl.indexOf(",");
  const screenshotData = separator >= 0 ? dataUrl.slice(separator + 1) : "";
  if (!screenshotData) throw new Error("Chrome returned no screenshot data");
  return screenshotData;
}

/** Extract a fresh element map after the guarded action and screenshot have completed. */
async function captureInteractionState(tabId, action, screenshotData) {
  let execution;
  try {
    execution = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [INTERACTION_ELEMENT_LIMIT, INTERACTION_TEXT_LIMIT],
      func: (elementLimit, textLimit) => {
      const referenceAttribute = "data-browser-mcp-ref";
      const referencePrefix = crypto.randomUUID().slice(0, 8);
      for (const previous of document.querySelectorAll(`[${referenceAttribute}]`)) {
        previous.removeAttribute(referenceAttribute);
      }
      const selectors = [
        "a[href]", "button", "input:not([type='hidden'])", "textarea", "select", "summary",
        "[contenteditable='true']", "[role='button']", "[role='link']", "[role='checkbox']",
        "[role='radio']", "[role='tab']", "[role='menuitem']", "[role='option']",
        "[role='switch']", "[onclick]",
      ].join(",");
      const visible = (element, rect) => {
        const style = getComputedStyle(element);
        return rect.width > 2 && rect.height > 2
          && style.visibility !== "hidden" && style.display !== "none"
          && Number(style.opacity || "1") > 0
          && rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
      };
      const implicitRole = (element) => {
        if (element instanceof HTMLAnchorElement) return "link";
        if (element instanceof HTMLButtonElement) return "button";
        if (element instanceof HTMLSelectElement) return "combobox";
        if (element instanceof HTMLTextAreaElement) return "textbox";
        if (element instanceof HTMLInputElement) {
          if (["checkbox", "radio", "button", "submit", "reset"].includes(element.type)) {
            return element.type === "submit" || element.type === "reset" ? "button" : element.type;
          }
          return "textbox";
        }
        return element.isContentEditable ? "textbox" : element.tagName.toLowerCase();
      };
      const accessibleName = (element) => {
        const labelledBy = element.getAttribute("aria-labelledby");
        const labelled = labelledBy
          ? labelledBy.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" ")
          : "";
        const label = element instanceof HTMLElement && "labels" in element
          ? [...(element.labels || [])].map((item) => item.innerText || "").join(" ")
          : "";
        const valueLabel = element instanceof HTMLInputElement
          && ["button", "submit", "reset"].includes(element.type) ? element.value : "";
        return String(
          element.getAttribute("aria-label") || labelled || label
          || element.getAttribute("placeholder") || element.getAttribute("alt")
          || element.getAttribute("title") || valueLabel || element.innerText || "",
        ).replace(/\s+/g, " ").trim().slice(0, 240);
      };
      const candidates = [...document.querySelectorAll(selectors)];
      const elements = [];
      for (const element of candidates) {
        if (elements.length >= elementLimit) break;
        const rect = element.getBoundingClientRect();
        if (!visible(element, rect)) continue;
        const elementId = `${referencePrefix}-e${elements.length + 1}`;
        element.setAttribute(referenceAttribute, elementId);
        const href = element instanceof HTMLAnchorElement && /^https?:/i.test(element.href)
          ? element.href : null;
        const checkable = element instanceof HTMLInputElement
          && (element.type === "checkbox" || element.type === "radio");
        const selectable = element instanceof HTMLOptionElement;
        const value = element instanceof HTMLInputElement && element.type !== "password"
          ? element.value
          : element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement
            ? element.value
            : element.isContentEditable ? (element.innerText || "").slice(0, 1000) : null;
        elements.push({
          element_id: elementId,
          tag: element.tagName.toLowerCase(),
          role: element.getAttribute("role") || implicitRole(element),
          name: accessibleName(element),
          input_type: element instanceof HTMLInputElement ? element.type : null,
          value,
          href,
          disabled: Boolean(element.disabled || element.getAttribute("aria-disabled") === "true"),
          checked: checkable ? element.checked : null,
          selected: selectable ? element.selected : null,
          x: Math.round(rect.left * 10) / 10,
          y: Math.round(rect.top * 10) / 10,
          width: Math.round(rect.width * 10) / 10,
          height: Math.round(rect.height * 10) / 10,
        });
      }
      const allVisibleCount = candidates.reduce((count, element) => {
        const rect = element.getBoundingClientRect();
        return count + (visible(element, rect) ? 1 : 0);
      }, 0);
      const warnings = allVisibleCount > elementLimit
        ? [`Visible element list limited to ${elementLimit} of ${allVisibleCount}; scroll to inspect more.`]
        : [];
      return {
        url: location.href,
        title: document.title || "",
        viewport: {
          width: Math.max(1, innerWidth),
          height: Math.max(1, innerHeight),
          device_scale_factor: Math.max(0.1, devicePixelRatio || 1),
          scroll_x: Math.max(0, Math.round(scrollX)),
          scroll_y: Math.max(0, Math.round(scrollY)),
          document_width: Math.max(1, document.documentElement?.scrollWidth || innerWidth),
          document_height: Math.max(1, document.documentElement?.scrollHeight || innerHeight),
        },
        elements,
        visible_text: String(document.body?.innerText || "").slice(0, textLimit),
        warnings,
      };
      },
    });
  } catch (error) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    throw new Error(
      `page-state script failed at ${String(tab?.url || "closed")}: ${error?.message || error}`,
    );
  }
  const [{ result: page } = {}] = execution;
  if (!page || typeof page.url !== "string") throw new Error("page snapshot script returned no data");
  return {
    state: {
      action,
      ...page,
      screenshot_mime_type: "image/jpeg",
    },
    screenshot_data: screenshotData,
  };
}

/** Dispatch one Zhihu read adapter without exposing arbitrary page-context fetches. */
async function dispatchZhihuFetch(state, message) {
  const reply = (payload) =>
    sendJson(state, { type: "zhihu.fetch.result", id: message.id, ...payload });
  if (message.action === "search") {
    await runZhihuSearch(state, message.args || {}, reply);
  } else if (message.action === "invitations") {
    await runZhihuInvitations(state, message.args || {}, reply);
  } else {
    reply({ ok: false, error: `unsupported zhihu action: ${message.action}` });
  }
}

/** Execute Zhihu search_v3 inside an authenticated www.zhihu.com tab. */
async function runZhihuSearch(state, args, reply) {
  const keyword = String(args.keyword || "").trim();
  if (!keyword) {
    reply({ ok: false, error: "zhihu search keyword is required" });
    return;
  }
  const searchType = String(args.type || "general");
  const offset = Math.max(0, Number(args.offset) || 0);
  const endpoint = new URL("https://www.zhihu.com/api/v4/search_v3");
  endpoint.searchParams.set("t", searchType);
  endpoint.searchParams.set("q", keyword);
  endpoint.searchParams.set("correction", "1");
  endpoint.searchParams.set("offset", String(offset));
  endpoint.searchParams.set("limit", "20");
  const approval = await requestUrlApproval(state, endpoint.toString());
  if (!approval.allowed) {
    reply({ ok: false, error: `request blocked: ${approval.error || "URL policy"}` });
    return;
  }

  let tabId = null;
  try {
    const tab = await chrome.tabs.create({ url: "https://www.zhihu.com/", active: false });
    tabId = tab.id;
    if (tabId == null) throw new Error("Chrome did not create a Zhihu search tab");
    await waitForTabComplete(tabId);
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [endpoint.toString()],
      func: async (url) => {
        try {
          const response = await fetch(url, {
            credentials: "include",
            headers: { Accept: "application/json, text/plain, */*" },
          });
          const body = await response.text();
          if (!response.ok) return { error: `HTTP ${response.status}: ${body.slice(0, 300)}` };
          return { data: JSON.parse(body) };
        } catch (error) {
          return { error: String(error?.message || error) };
        }
      },
    });
    if (!result || result.error) {
      throw new Error(result?.error || "Zhihu search script returned no data");
    }
    reply({ ok: true, data: result.data });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Read paged answer invitations through Zhihu's current authenticated notification API. */
async function runZhihuInvitations(state, args, reply) {
  const startTimestamp = Math.max(0, Number(args.startTimestamp) || 0);
  const maxPages = Math.max(1, Math.min(10, Number(args.maxPages) || 5));
  const endpoint = new URL("https://www.zhihu.com/api/v4/notifications/v2/recent");
  endpoint.searchParams.set("entry_name", "invite");
  endpoint.searchParams.set("limit", "20");
  const approval = await requestUrlApproval(state, endpoint.toString());
  if (!approval.allowed) {
    reply({ ok: false, error: `request blocked: ${approval.error || "URL policy"}` });
    return;
  }

  let tabId = null;
  try {
    const tab = await chrome.tabs.create({ url: "https://www.zhihu.com/", active: false });
    tabId = tab.id;
    if (tabId == null) throw new Error("Chrome did not create a Zhihu invitations tab");
    await waitForTabComplete(tabId);
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [endpoint.toString(), startTimestamp, maxPages],
      func: async (initialUrl, requestedStart, pageLimit) => {
        const pages = [];
        let currentUrl = initialUrl;
        let complete = false;
        let nextOffset = null;
        try {
          for (let pageNumber = 0; pageNumber < pageLimit; pageNumber += 1) {
            const response = await fetch(currentUrl, {
              credentials: "include",
              headers: { Accept: "application/json, text/plain, */*" },
            });
            const body = await response.text();
            if (!response.ok) return { error: `HTTP ${response.status}: ${body.slice(0, 300)}` };
            const payload = JSON.parse(body);
            pages.push(payload);
            const records = Array.isArray(payload?.data) ? payload.data : [];
            const timestamps = records
              .map((record) => Number(record?.create_time) || 0)
              .filter((timestamp) => timestamp > 0);
            if (timestamps.length && Math.min(...timestamps) < requestedStart) {
              complete = true;
              break;
            }
            if (payload?.paging?.is_end === true) {
              complete = true;
              break;
            }
            const next = new URL(String(payload?.paging?.next || ""), initialUrl);
            const offset = next.searchParams.get("offset");
            if (!offset || !/^\d+$/.test(offset)) break;
            nextOffset = offset;
            const generated = new URL(initialUrl);
            generated.searchParams.set("offset", offset);
            currentUrl = generated.toString();
          }
          return { data: { pages, complete, next_offset: nextOffset } };
        } catch (error) {
          return { error: String(error?.message || error) };
        }
      },
    });
    if (!result || result.error) {
      throw new Error(result?.error || "Zhihu invitations script returned no data");
    }
    reply({ ok: true, data: result.data });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Dispatch one trusted click at an already validated viewport point without retrying it. */
async function dispatchTrustedPointClick(debugTarget, point) {
  const position = { x: Number(point.x), y: Number(point.y) };
  await chrome.debugger.sendCommand(debugTarget, "Input.dispatchMouseEvent", {
    type: "mouseMoved",
    button: "none",
    buttons: 0,
    ...position,
  });
  await chrome.debugger.sendCommand(debugTarget, "Input.dispatchMouseEvent", {
    type: "mousePressed",
    button: "left",
    buttons: 1,
    clickCount: 1,
    ...position,
  });
  await chrome.debugger.sendCommand(debugTarget, "Input.dispatchMouseEvent", {
    type: "mouseReleased",
    button: "left",
    buttons: 0,
    clickCount: 1,
    ...position,
  });
}

/** Read one site control in the page's MAIN world and retain its marker diagnostics. */
async function readEngagementControl(tabId, reader, action) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [action],
    func: reader,
  });
  return result || { error: "engagement control script returned no data" };
}

/** Wait until hydration exposes the same valid engagement state twice in succession. */
async function waitForStableEngagementControl(tabId, reader, action, platform) {
  let previous = null;
  let last = null;
  for (let attempt = 0; attempt < 12; attempt += 1) {
    last = await readEngagementControl(tabId, reader, action);
    if (!last.error) {
      if (
        previous
        && previous.active === last.active
        && previous.stateMarker === last.stateMarker
      ) {
        return last;
      }
      previous = last;
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  throw new Error(last?.error || `${platform} ${action} control did not stabilize`);
}

/** Poll only for verification after one click; never resend a mutation. */
async function waitForEngagementState(tabId, reader, action, expected, platform) {
  let last = null;
  for (let attempt = 0; attempt < 12; attempt += 1) {
    last = await readEngagementControl(tabId, reader, action);
    if (!last.error && last.active === expected) return last;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  const marker = last?.stateMarker || last?.error || "unknown";
  throw new Error(
    `${platform} ${action} click was sent once but final state was not verified (${marker}); `
      + "the click was not retried",
  );
}

/** Create a non-focused desktop window so site engagement controls keep their stable layout. */
async function createEngagementWindow(url, platform) {
  const createdWindow = await chrome.windows.create({
    url,
    focused: false,
    type: "normal",
    width: 1512,
    height: 900,
  });
  const [tab] = createdWindow.tabs || [];
  if (tab?.id == null || createdWindow.id == null) {
    throw new Error(`Chrome did not create an isolated ${platform} engagement window`);
  }
  return { tabId: tab.id, windowId: createdWindow.id };
}

/** Dispatch one Xiaohongshu read adapter without exposing generic page mutation. */
async function dispatchXhsFetch(state, message) {
  const reply = (payload) =>
    sendJson(state, { type: "xhs.fetch.result", id: message.id, ...payload });
  if (message.action === "search") {
    await runXhsSearch(state, message.args || {}, reply);
  } else if (message.action === "note") {
    await runXhsNote(state, message.args || {}, reply);
  } else if (message.action === "comments") {
    await runXhsComments(state, message.args || {}, reply);
  } else if (message.action === "user_notes") {
    await runXhsUserNotes(state, message.args || {}, reply);
  } else {
    reply({ ok: false, error: `unsupported xhs action: ${message.action}` });
  }
}

/** Return one visible XHS note-level engagement control and its semantic icon state. */
function readXhsEngagementControl(action) {
  const contract = action === "like"
    ? { selector: ".like-wrapper", inactive: "#like", active: "#liked" }
    : action === "collect"
      ? { selector: ".collect-wrapper", inactive: "#collect", active: "#collected" }
      : null;
  if (!contract) return { error: `unsupported XHS engagement action: ${action}` };
  const candidates = Array.from(document.querySelectorAll(
    `.interact-container .buttons.engage-bar-style ${contract.selector}`,
  )).filter((element) => {
    if (!(element instanceof HTMLElement) || element.offsetParent === null) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  });
  const control = candidates.at(-1);
  if (!(control instanceof HTMLElement)) {
    return { error: `XHS note-level ${action} control was not found` };
  }
  control.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
  const iconUse = control.querySelector("svg use");
  const marker = iconUse?.getAttribute("href") || iconUse?.getAttribute("xlink:href") || "";
  if (marker !== contract.inactive && marker !== contract.active) {
    return { error: `XHS ${action} control exposed an unknown state marker: ${marker || "empty"}` };
  }
  const rect = control.getBoundingClientRect();
  return {
    action,
    active: marker === contract.active,
    stateMarker: marker,
    point: {
      x: Math.max(1, Math.min(innerWidth - 1, Math.round(rect.left + rect.width / 2))),
      y: Math.max(1, Math.min(innerHeight - 1, Math.round(rect.top + rect.height / 2))),
    },
  };
}

/** Dispatch one allowlisted XHS account mutation through a dedicated namespace. */
async function dispatchXhsMutation(state, message) {
  const reply = (payload) =>
    sendJson(state, { type: "xhs.mutate.result", id: message.id, ...payload });
  if (message.action !== "like" && message.action !== "collect") {
    reply({ ok: false, error: `unsupported xhs mutation: ${message.action}` });
    return;
  }
  await runXhsMutation(state, message.action, message.args || {}, reply);
}

/** Set one XHS engagement control to its requested state with at most one trusted click. */
async function runXhsMutation(state, action, args, reply) {
  const noteId = String(args.noteId || "").trim();
  if (!noteId) {
    reply({ ok: false, error: "xhs noteId is required" });
    return;
  }
  const enabled = args.enabled !== false;
  const xsecToken = String(args.xsecToken || "");
  const xsecSource = String(args.xsecSource || "pc_search");
  const target = new URL(`https://www.xiaohongshu.com/explore/${encodeURIComponent(noteId)}`);
  if (xsecToken) target.searchParams.set("xsec_token", xsecToken);
  if (xsecSource) target.searchParams.set("xsec_source", xsecSource);
  const approval = await requestUrlApproval(state, target.toString());
  if (!approval.allowed) {
    reply({ ok: false, error: `navigation blocked: ${approval.error || "URL policy"}` });
    return;
  }

  let tabId = null;
  let windowId = null;
  let debugTarget = null;
  try {
    ({ tabId, windowId } = await createEngagementWindow(
      "https://www.xiaohongshu.com/explore",
      "XHS",
    ));
    await waitForTabComplete(tabId);
    await chrome.tabs.update(tabId, { url: target.toString() });
    await waitForTabComplete(tabId);
    await new Promise((resolve) => setTimeout(resolve, 1200));
    const before = await waitForStableEngagementControl(
      tabId,
      readXhsEngagementControl,
      action,
      "XHS",
    );
    if (before.active === enabled) {
      reply({
        ok: true,
        data: {
          platform: "xhs",
          post_id: noteId,
          action,
          requested_state: enabled,
          active: before.active,
          changed: false,
          url: target.toString(),
        },
      });
      return;
    }

    debugTarget = { tabId };
    await chrome.debugger.attach(debugTarget, "1.3");
    try {
      await dispatchTrustedPointClick(debugTarget, before.point);
    } finally {
      await chrome.debugger.detach(debugTarget).catch(() => {});
      debugTarget = null;
    }
    const after = await waitForEngagementState(
      tabId,
      readXhsEngagementControl,
      action,
      enabled,
      "XHS",
    );
    reply({
      ok: true,
      data: {
        platform: "xhs",
        post_id: noteId,
        action,
        requested_state: enabled,
        active: after.active,
        changed: true,
        url: target.toString(),
      },
    });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (debugTarget) await chrome.debugger.detach(debugTarget).catch(() => {});
    if (windowId != null) {
      await chrome.windows.remove(windowId).catch(() => {});
    } else if (tabId != null) {
      await chrome.tabs.remove(tabId).catch(() => {});
    }
  }
}

/** Navigate the real XHS search UI and capture its own signed search response. */
async function runXhsSearch(state, args, reply) {
  const keyword = String(args.keyword || "").trim();
  if (!keyword) {
    reply({ ok: false, error: "xhs search keyword is required" });
    return;
  }
  const page = Math.max(1, Number(args.page) || 1);
  const sort = String(args.sort || "general");
  const url = new URL("https://www.xiaohongshu.com/search_result");
  url.searchParams.set("keyword", keyword);
  url.searchParams.set("source", "web_explore_feed");
  url.searchParams.set("type", "0");
  url.searchParams.set("page", String(page));
  url.searchParams.set("sort", sort);

  const approval = await requestUrlApproval(state, url.toString());
  if (!approval.allowed) {
    reply({ ok: false, error: `navigation blocked: ${approval.error || "URL policy"}` });
    return;
  }
  let tabId = null;
  try {
    const tab = await chrome.tabs.create({
      url: "https://www.xiaohongshu.com/explore",
      active: false,
    });
    tabId = tab.id;
    if (tabId == null) throw new Error("Chrome did not create an XHS search tab");
    await waitForTabComplete(tabId);
    const responseBody = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const pending = pendingXhsSearches.get(tabId);
        pendingXhsSearches.delete(tabId);
        const seen = pending?.seenUrls?.slice(-20) || [];
        reject(
          new Error(
            `timeout waiting for XHS signed search response; URLs seen: ${
              seen.join(" | ") || "(none)"
            }`,
          ),
        );
      }, SITE_ACTION_TIMEOUT_MS);
      pendingXhsSearches.set(tabId, { resolve, reject, timer, seenUrls: [] });
    });
    await chrome.tabs.update(tabId, { url: url.toString() });
    const body = await responseBody;
    const data = JSON.parse(body);
    reply({ ok: true, data });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    const pending = tabId == null ? null : pendingXhsSearches.get(tabId);
    if (pending) {
      clearTimeout(pending.timer);
      pendingXhsSearches.delete(tabId);
    }
    if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Resolve an in-flight XHS search from the page's own signed response. */
function handleObservedXhsResponse(message, sender) {
  if (message?.type !== "xhs-response") return false;
  const tabId = sender?.tab?.id;
  const url = String(message.url || "");
  const pendingSearch = tabId == null ? null : pendingXhsSearches.get(tabId);
  if (pendingSearch) {
    pendingSearch.seenUrls.push(url);
    if (/\/api\/sns\/web\/v[12]\/search\/notes(?:\?|$)/.test(url)) {
      clearTimeout(pendingSearch.timer);
      pendingXhsSearches.delete(tabId);
      if (message.truncated === true) {
        pendingSearch.reject(new Error("XHS signed search response exceeded the 4 MiB safety limit"));
      } else if (Number(message.status) < 200 || Number(message.status) >= 300) {
        pendingSearch.reject(new Error(`XHS signed search returned HTTP ${message.status || 0}`));
      } else {
        pendingSearch.resolve(String(message.body || ""));
      }
    }
  }

  const pendingUserNotes = tabId == null ? null : pendingXhsUserNotes.get(tabId);
  if (
    pendingUserNotes &&
    /\/api\/sns\/web\/v1\/user_posted(?:\?|$)/.test(url)
  ) {
    if (message.truncated === true) {
      pendingUserNotes.errors.push("XHS user-posted response exceeded the 4 MiB safety limit");
    } else if (Number(message.status) < 200 || Number(message.status) >= 300) {
      pendingUserNotes.errors.push(`XHS user-posted request returned HTTP ${message.status || 0}`);
    } else {
      pendingUserNotes.responses.push({ url, body: String(message.body || "") });
    }
  }

  const pendingComments = tabId == null ? null : pendingXhsComments.get(tabId);
  if (
    pendingComments &&
    /\/api\/sns\/web\/v[12]\/comment\/(?:page|sub\/page)(?:\?|$)/.test(url)
  ) {
    if (message.truncated === true) {
      pendingComments.errors.push("XHS comment response exceeded the 4 MiB safety limit");
    } else if (Number(message.status) < 200 || Number(message.status) >= 300) {
      pendingComments.errors.push(`XHS comment request returned HTTP ${message.status || 0}`);
    } else {
      pendingComments.responses.push({ url, body: String(message.body || "") });
    }
  }
  return false;
}

/** Add comment ids from one API record and its inline replies to the collected set. */
function trackXhsCommentRecord(rawComment, seenIds) {
  if (!rawComment || typeof rawComment !== "object") return;
  const commentId = String(rawComment.id || rawComment.comment_id || "");
  if (commentId) seenIds.add(commentId);
  const replies = Array.isArray(rawComment.sub_comments)
    ? rawComment.sub_comments
    : Array.isArray(rawComment.subComments) ? rawComment.subComments : [];
  for (const reply of replies) trackXhsCommentRecord(reply, seenIds);
}

/** Parse newly observed comment responses and retain their pagination metadata. */
function drainXhsCommentResponses(pending, noteId, pages, seenIds, progress) {
  while (pending.responses.length) {
    const response = pending.responses.shift();
    const responseUrl = new URL(response.url, "https://edith.xiaohongshu.com");
    const responseNoteId = responseUrl.searchParams.get("note_id");
    if (responseNoteId && responseNoteId !== noteId) continue;
    const payload = JSON.parse(response.body);
    const data = payload?.data && typeof payload.data === "object" ? payload.data : payload;
    const comments = Array.isArray(data?.comments)
      ? data.comments
      : Array.isArray(data?.comment_list) ? data.comment_list : [];
    const kind = /\/comment\/sub\/page(?:\?|$)/.test(responseUrl.pathname) ? "sub" : "root";
    const rootCommentId = responseUrl.searchParams.get("root_comment_id") || "";
    pages.push({
      kind,
      url: responseUrl.toString(),
      root_comment_id: rootCommentId,
      payload,
    });
    for (const comment of comments) trackXhsCommentRecord(comment, seenIds);
    if (kind === "root") {
      progress.sawRootPage = true;
      const hasMore = data?.has_more ?? data?.hasMore;
      if (typeof hasMore === "boolean") progress.rootComplete = !hasMore;
    }
  }
}

/** Collect XHS comments by scrolling the note stream instead of the page body. */
async function runXhsComments(state, args, reply) {
  const noteId = String(args.noteId || "").trim();
  if (!noteId) {
    reply({ ok: false, error: "xhs noteId is required" });
    return;
  }
  const xsecToken = String(args.xsecToken || "");
  const xsecSource = String(args.xsecSource || "pc_search");
  const maxComments = Math.max(1, Math.min(5000, Number(args.maxComments) || 500));
  const maxScrolls = Math.max(40, Math.min(220, Math.ceil(maxComments * 0.6)));
  const target = new URL(`https://www.xiaohongshu.com/explore/${encodeURIComponent(noteId)}`);
  if (xsecToken) target.searchParams.set("xsec_token", xsecToken);
  if (xsecSource) target.searchParams.set("xsec_source", xsecSource);
  const approval = await requestUrlApproval(state, target.toString());
  if (!approval.allowed) {
    reply({ ok: false, error: `navigation blocked: ${approval.error || "URL policy"}` });
    return;
  }

  let tabId = null;
  let windowId = null;
  let debugTarget = null;
  const pages = [];
  const seenIds = new Set();
  const progress = { sawRootPage: false, rootComplete: false };
  let expectedCount = null;
  let scrolls = 0;
  let complete = false;
  let limitReached = false;
  let stableRounds = 0;
  let previousHeight = -1;
  let previousCount = -1;
  let scrollDirection = 1;
  let sweepTurns = 0;

  try {
    const createdWindow = await chrome.windows.create({
      url: "https://www.xiaohongshu.com/explore",
      focused: false,
      type: "normal",
    });
    const [tab] = createdWindow.tabs || [];
    tabId = tab?.id;
    windowId = createdWindow.id;
    if (tabId == null || windowId == null) {
      throw new Error("Chrome did not create an isolated XHS comments window");
    }
    await waitForTabComplete(tabId);
    const pending = { responses: [], errors: [] };
    pendingXhsComments.set(tabId, pending);
    await chrome.tabs.update(tabId, { url: target.toString() });
    await waitForTabComplete(tabId);
    await new Promise((resolve) => setTimeout(resolve, 1200));
    debugTarget = { tabId };
    await chrome.debugger.attach(debugTarget, "1.3");

    for (scrolls = 0; scrolls < maxScrolls; scrolls += 1) {
      if (pending.errors.length) throw new Error(pending.errors.shift());
      drainXhsCommentResponses(pending, noteId, pages, seenIds, progress);

      const [{ result: ui } = {}] = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: () => {
          const scroller = document.querySelector(".note-scroller");
          if (!(scroller instanceof HTMLElement)) {
            return { error: "XHS .note-scroller comment stream was not found" };
          }
          const countMatch = scroller.textContent?.match(/共\s*(\d+)\s*条评论/);
          const scrollerRect = scroller.getBoundingClientRect();
          const expanders = Array.from(scroller.querySelectorAll(".show-more")).filter((element) => {
            if (
              !/展开.*回复/.test(element.textContent || "")
              || !(element instanceof HTMLElement)
              || element.offsetParent === null
            ) return false;
            const rect = element.getBoundingClientRect();
            return rect.bottom >= scrollerRect.top && rect.top <= scrollerRect.bottom;
          });
          let clicked = 0;
          for (const element of expanders.slice(0, 4)) {
            element.click();
            clicked += 1;
          }
          return {
            expectedCount: countMatch ? Number(countMatch[1]) : null,
            clicked,
            wheelX: Math.round(scrollerRect.left + scrollerRect.width / 2),
            wheelY: Math.round(scrollerRect.top + scrollerRect.height / 2),
            clientHeight: scroller.clientHeight,
            scrollHeight: scroller.scrollHeight,
            atTop: scroller.scrollTop <= 4,
            atBottom: scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 4,
            endText: /(到底了|没有更多)/.test(scroller.textContent || ""),
          };
        },
      });
      if (!ui || ui.error) throw new Error(ui?.error || "XHS comment stream returned no state");
      if (Number.isFinite(ui.expectedCount)) expectedCount = Number(ui.expectedCount);

      let directionChanged = false;
      const needsReplySweep = expectedCount != null
        && seenIds.size < expectedCount
        && progress.rootComplete;
      if (ui.clicked === 0 && needsReplySweep && sweepTurns < 4) {
        if (scrollDirection > 0 && ui.atBottom) {
          scrollDirection = -1;
          sweepTurns += 1;
          directionChanged = true;
        } else if (scrollDirection < 0 && ui.atTop) {
          scrollDirection = 1;
          sweepTurns += 1;
          directionChanged = true;
        }
      }
      if (ui.clicked === 0) {
        const wheelDelta = Math.max(320, Math.floor(Number(ui.clientHeight || 0) * 0.75));
        await chrome.debugger.sendCommand(debugTarget, "Input.dispatchMouseEvent", {
          type: "mouseWheel",
          x: Number(ui.wheelX),
          y: Number(ui.wheelY),
          deltaX: 0,
          deltaY: wheelDelta * scrollDirection,
        });
      }

      await new Promise((resolve) => setTimeout(resolve, 800));
      if (pending.errors.length) throw new Error(pending.errors.shift());
      drainXhsCommentResponses(pending, noteId, pages, seenIds, progress);

      const unchanged = previousCount === seenIds.size && previousHeight === ui.scrollHeight;
      const atSweepBoundary = scrollDirection > 0 ? ui.atBottom : ui.atTop;
      stableRounds = unchanged && ui.clicked === 0 && atSweepBoundary && !directionChanged
        ? stableRounds + 1
        : 0;
      previousCount = seenIds.size;
      previousHeight = ui.scrollHeight;

      const expectedReached = expectedCount != null && seenIds.size >= expectedCount;
      const reachedNaturalEnd = expectedCount != null
        ? expectedReached
        : progress.rootComplete || ui.endText;
      if (seenIds.size >= maxComments && !expectedReached) {
        limitReached = true;
        break;
      }
      if (
        stableRounds >= 2
        && ui.clicked === 0
        && reachedNaturalEnd
      ) {
        complete = true;
        break;
      }
      if (stableRounds >= 10) break;
    }

    drainXhsCommentResponses(pending, noteId, pages, seenIds, progress);
    if (expectedCount != null && seenIds.size >= expectedCount && !limitReached) complete = true;
    reply({
      ok: true,
      data: {
        expected_count: expectedCount,
        complete,
        limit_reached: limitReached,
        scrolls: Math.min(scrolls + 1, maxScrolls),
        pages,
      },
    });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (tabId != null) pendingXhsComments.delete(tabId);
    if (debugTarget != null) await chrome.debugger.detach(debugTarget).catch(() => {});
    if (windowId != null) await chrome.windows.remove(windowId).catch(() => {});
    else if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Read a captured signed user-posted response for one requested cursor. */
function consumeXhsUserNotesResponse(pending, cursor) {
  const index = pending.responses.findIndex((response) => {
    try {
      return new URL(response.url, "https://edith.xiaohongshu.com").searchParams.get("cursor") === cursor;
    } catch {
      return false;
    }
  });
  if (index < 0) return null;
  const [response] = pending.responses.splice(index, 1);
  const parsed = JSON.parse(response.body);
  const data = parsed?.data && typeof parsed.data === "object" ? parsed.data : parsed;
  if (!Array.isArray(data?.notes)) throw new Error("XHS user-posted response has no notes array");
  return {
    notes: data.notes,
    cursor: String(data.cursor || ""),
    has_more: data.has_more === true,
  };
}

/** Wait briefly for the page's own signed user-posted request to finish. */
async function waitForXhsUserNotesResponse(pending, cursor, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (pending.errors.length) throw new Error(pending.errors.shift());
    const response = consumeXhsUserNotesResponse(pending, cursor);
    if (response) return response;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return null;
}

/** Read SSR notes and signed pagination responses for one Xiaohongshu account. */
async function runXhsUserNotes(state, args, reply) {
  let userId = String(args.userId || "").trim();
  const maxPages = Math.max(1, Math.min(10, Number(args.maxPages) || 5));
  if (userId && !/^[A-Za-z0-9_-]{1,128}$/.test(userId)) {
    reply({ ok: false, error: "xhs userId contains unsupported characters" });
    return;
  }

  let tabId = null;
  try {
    const tab = await chrome.tabs.create({
      url: "https://www.xiaohongshu.com/explore",
      active: false,
    });
    tabId = tab.id;
    if (tabId == null) throw new Error("Chrome did not create an XHS profile tab");
    await waitForTabComplete(tabId);

    if (!userId) {
      const [{ result: currentUser } = {}] = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: () => {
          const user = window.__INITIAL_STATE__?.user;
          const info = user?.userInfo || {};
          const profileLink = Array.from(
            document.querySelectorAll('a[href^="/user/profile/"]'),
          ).find((element) => element.textContent?.trim() === "我");
          const profileMatch = profileLink
            ?.getAttribute("href")
            ?.match(/^\/user\/profile\/([^/?#]+)/);
          return {
            userId: String(info.userId || info.user_id || profileMatch?.[1] || ""),
          };
        },
      });
      if (!currentUser?.userId) {
        throw new Error("XHS logged-in account was not found in the current Chrome profile");
      }
      userId = currentUser.userId;
    }

    const target = new URL(
      `https://www.xiaohongshu.com/user/profile/${encodeURIComponent(userId)}`,
    );
    const approval = await requestUrlApproval(state, target.toString());
    if (!approval.allowed) {
      throw new Error(`navigation blocked: ${approval.error || "URL policy"}`);
    }

    const [{ result: profile } = {}] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [target.toString()],
      func: async (url) => {
        try {
          const response = await fetch(url, { credentials: "include" });
          if (!response.ok) return { error: `HTTP ${response.status}` };
          const html = await response.text();
          const match = html.match(
            /window\.__INITIAL_STATE__\s*=\s*(\{[\s\S]+?\})\s*;?\s*(?:<\/script>|\n)/,
          );
          if (!match) return { error: "profile SSR state not found" };
          const state = JSON.parse(match[1].replace(/:\s*undefined/g, ": null"));
          const store = state?.user || {};
          const query = Array.isArray(store.noteQueries) ? store.noteQueries[0] || {} : {};
          const noteTabs = Array.isArray(store.notes) ? store.notes : [];
          const notes = Array.isArray(noteTabs[0]) ? noteTabs[0] : [];
          const basic = store.userPageData?.basicInfo || {};
          return {
            userId: String(query.userId || ""),
            nickname: String(basic.nickname || ""),
            redId: String(basic.redId || basic.red_id || ""),
            notes,
            cursor: String(query.cursor || ""),
            hasMore: query.hasMore === true,
          };
        } catch (error) {
          return { error: String(error?.message || error) };
        }
      },
    });
    if (!profile || profile.error || !Array.isArray(profile.notes)) {
      throw new Error(
        profile?.error || "XHS profile SSR state did not contain a published-note list",
      );
    }

    const pending = { responses: [], errors: [] };
    pendingXhsUserNotes.set(tabId, pending);
    await chrome.tabs.update(tabId, { url: target.toString() });
    await waitForTabComplete(tabId);

    const pages = [
      {
        notes: profile.notes,
        cursor: profile.cursor,
        has_more: profile.hasMore,
      },
    ];
    let cursor = profile.cursor;
    let hasMore = profile.hasMore;

    while (pages.length < maxPages && hasMore) {
      let nextPage = await waitForXhsUserNotesResponse(pending, cursor, 1500);
      if (!nextPage) {
        await chrome.scripting.executeScript({
          target: { tabId },
          world: "MAIN",
          func: () => window.scrollTo(0, document.documentElement.scrollHeight),
        });
        nextPage = await waitForXhsUserNotesResponse(pending, cursor, 8000);
      }
      if (!nextPage) break;
      pages.push(nextPage);
      const previousCursor = cursor;
      cursor = nextPage.cursor;
      hasMore = nextPage.has_more;
      if (hasMore && cursor === previousCursor) break;
    }

    reply({
      ok: true,
      data: {
        user_id: profile.userId || userId,
        nickname: profile.nickname,
        red_id: profile.redId,
        complete: !hasMore,
        pages_fetched: pages.length,
        pages,
      },
    });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (tabId != null) pendingXhsUserNotes.delete(tabId);
    if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Read one JSON-safe note snapshot from XHS's already evaluated runtime state. */
function readXhsNoteRuntimeState(noteId) {
  const detailMap = window.__INITIAL_STATE__?.note?.noteDetailMap;
  const detail = detailMap instanceof Map ? detailMap.get(noteId) : detailMap?.[noteId];
  const note = detail?.note;
  if (!note || typeof note !== "object") {
    return { error: "note was not found in the evaluated XHS runtime state" };
  }
  const selected = {
    type: note.type,
    title: note.title,
    desc: note.desc,
    time: note.time,
    user: note.user,
    interactInfo: note.interactInfo,
    imageList: note.imageList,
    video: note.video,
  };
  try {
    const safeNote = JSON.parse(JSON.stringify(selected, (_key, value) => {
      if (value instanceof Map) return Object.fromEntries(value);
      if (value instanceof Set) return Array.from(value);
      if (typeof value === "bigint") return String(value);
      return value;
    }));
    return {
      state: {
        note: {
          noteDetailMap: {
            [noteId]: { note: safeNote },
          },
        },
      },
    };
  } catch (error) {
    return { error: `unable to serialize XHS note state: ${error?.message || error}` };
  }
}

/** Navigate an authenticated XHS tab and read its evaluated note state. */
async function runXhsNote(state, args, reply) {
  const noteId = String(args.noteId || "").trim();
  if (!noteId) {
    reply({ ok: false, error: "xhs noteId is required" });
    return;
  }
  const xsecToken = String(args.xsecToken || "");
  const xsecSource = String(args.xsecSource || "pc_search");
  const target = new URL(`https://www.xiaohongshu.com/explore/${encodeURIComponent(noteId)}`);
  if (xsecToken) target.searchParams.set("xsec_token", xsecToken);
  if (xsecSource) target.searchParams.set("xsec_source", xsecSource);
  const approval = await requestUrlApproval(state, target.toString());
  if (!approval.allowed) {
    reply({ ok: false, error: `navigation blocked: ${approval.error || "URL policy"}` });
    return;
  }

  let tabId = null;
  try {
    const tab = await chrome.tabs.create({
      url: "https://www.xiaohongshu.com/explore",
      active: false,
    });
    tabId = tab.id;
    if (tabId == null) throw new Error("Chrome did not create an XHS note tab");
    await waitForTabComplete(tabId);
    await chrome.tabs.update(tabId, { url: target.toString() });
    await waitForTabComplete(tabId);
    await new Promise((resolve) => setTimeout(resolve, 800));
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [noteId],
      func: readXhsNoteRuntimeState,
    });
    if (!result || result.error) throw new Error(result?.error || "XHS note script returned no data");
    reply({ ok: true, data: result.state });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Dispatch one Douyin read adapter while leaving signing and transport to the page. */
async function dispatchDouyinFetch(state, message) {
  const reply = (payload) =>
    sendJson(state, { type: "douyin.fetch.result", id: message.id, ...payload });
  if (message.action === "search") {
    await runDouyinSearch(state, message.args || {}, reply);
  } else if (message.action === "video") {
    await runDouyinVideo(state, message.args || {}, reply);
  } else if (message.action === "comments") {
    await runDouyinComments(state, message.args || {}, reply);
  } else {
    reply({ ok: false, error: `unsupported douyin action: ${message.action}` });
  }
}

/** Return one visible Douyin post-level engagement control and its e2e state marker. */
function readDouyinEngagementControl(action) {
  const contract = action === "like"
    ? { selector: '[data-e2e="video-player-digg"]', inactive: "video-player-no-digged" }
    : action === "collect"
      ? { selector: '[data-e2e="video-player-collect"]', inactive: "video-player-no-collect" }
      : null;
  if (!contract) return { error: `unsupported Douyin engagement action: ${action}` };
  /** Return whether a candidate can receive a trusted click in the current viewport. */
  const isVisible = (element) => {
    if (!(element instanceof HTMLElement) || element.offsetParent === null) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const semanticCandidates = Array.from(document.querySelectorAll(contract.selector));
  const stateControl = semanticCandidates.find((element) => {
    const marker = String(element.getAttribute("data-e2e-state") || "");
    return marker === contract.inactive
      || (action === "like" ? /digged/.test(marker) : /collect/.test(marker));
  });
  if (!(stateControl instanceof HTMLElement)) {
    return { error: `Douyin post-level ${action} state marker was not found` };
  }
  const visibleSemantic = semanticCandidates.filter(isVisible);
  let control = visibleSemantic.find((element) => {
    const rect = element.getBoundingClientRect();
    return rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth;
  }) || visibleSemantic.at(-1);
  if (!(control instanceof HTMLElement)) {
    const compactShare = Array.from(document.querySelectorAll(
      '[data-e2e="detail-video-info"] [data-e2e="video-share-icon-container"]',
    )).find(isVisible);
    const compactGroup = compactShare?.parentElement;
    const compactControls = compactGroup instanceof HTMLElement
      ? Array.from(compactGroup.children).filter(isVisible)
      : [];
    const shareIndex = compactShare instanceof HTMLElement
      ? compactControls.indexOf(compactShare)
      : -1;
    const compactIndex = action === "like" ? 0 : 2;
    if (shareIndex >= 3 && compactIndex < shareIndex) {
      control = compactControls[compactIndex];
    }
  }
  if (!(control instanceof HTMLElement)) {
    return { error: `Douyin post-level ${action} control was not found` };
  }
  control.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
  const marker = String(stateControl.getAttribute("data-e2e-state") || "");
  const activeMarker = action === "like" ? /digged/.test(marker) : /collect/.test(marker);
  if (!marker || (marker !== contract.inactive && !activeMarker)) {
    return {
      error: `Douyin ${action} control exposed an unknown state marker: ${marker || "empty"}`,
    };
  }
  const rect = control.getBoundingClientRect();
  return {
    action,
    active: marker !== contract.inactive,
    stateMarker: marker,
    point: {
      x: Math.max(1, Math.min(innerWidth - 1, Math.round(rect.left + rect.width / 2))),
      y: Math.max(1, Math.min(innerHeight - 1, Math.round(rect.top + rect.height / 2))),
    },
  };
}

/** Dispatch one allowlisted Douyin account mutation through a dedicated namespace. */
async function dispatchDouyinMutation(state, message) {
  const reply = (payload) =>
    sendJson(state, { type: "douyin.mutate.result", id: message.id, ...payload });
  if (message.action !== "like" && message.action !== "collect") {
    reply({ ok: false, error: `unsupported douyin mutation: ${message.action}` });
    return;
  }
  await runDouyinMutation(state, message.action, message.args || {}, reply);
}

/** Set one Douyin engagement control to its requested state with at most one trusted click. */
async function runDouyinMutation(state, action, args, reply) {
  const awemeId = String(args.awemeId || "").trim();
  const pageKind = String(args.pageKind || "video") === "note" ? "note" : "video";
  if (!/^\d+$/.test(awemeId)) {
    reply({ ok: false, error: "douyin awemeId must contain digits only" });
    return;
  }
  const enabled = args.enabled !== false;
  const target = new URL(`https://www.douyin.com/${pageKind}/${awemeId}`);
  const approval = await requestUrlApproval(state, target.toString());
  if (!approval.allowed) {
    reply({ ok: false, error: `navigation blocked: ${approval.error || "URL policy"}` });
    return;
  }

  let tabId = null;
  let windowId = null;
  let debugTarget = null;
  try {
    ({ tabId, windowId } = await createEngagementWindow(
      "https://www.douyin.com/",
      "Douyin",
    ));
    await waitForTabComplete(tabId);
    await chrome.tabs.update(tabId, { url: target.toString() });
    await waitForTabComplete(tabId);
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const before = await waitForStableEngagementControl(
      tabId,
      readDouyinEngagementControl,
      action,
      "Douyin",
    );
    if (before.active === enabled) {
      reply({
        ok: true,
        data: {
          platform: "douyin",
          post_id: awemeId,
          action,
          requested_state: enabled,
          active: before.active,
          changed: false,
          url: target.toString(),
        },
      });
      return;
    }

    debugTarget = { tabId };
    await chrome.debugger.attach(debugTarget, "1.3");
    try {
      await dispatchTrustedPointClick(debugTarget, before.point);
    } finally {
      await chrome.debugger.detach(debugTarget).catch(() => {});
      debugTarget = null;
    }
    const after = await waitForEngagementState(
      tabId,
      readDouyinEngagementControl,
      action,
      enabled,
      "Douyin",
    );
    reply({
      ok: true,
      data: {
        platform: "douyin",
        post_id: awemeId,
        action,
        requested_state: enabled,
        active: after.active,
        changed: true,
        url: target.toString(),
      },
    });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (debugTarget) await chrome.debugger.detach(debugTarget).catch(() => {});
    if (windowId != null) {
      await chrome.windows.remove(windowId).catch(() => {});
    } else if (tabId != null) {
      await chrome.tabs.remove(tabId).catch(() => {});
    }
  }
}

/** Return whether one observed response is the signed endpoint required by a read action. */
function matchesDouyinRead(kind, url) {
  if (kind === "search") {
    return /\/aweme\/v1\/web\/general\/search\/stream\/(?:\?|$)/.test(url);
  }
  return /\/aweme\/v1\/web\/aweme\/detail\/(?:\?|$)/.test(url);
}

/** Wait for one page-signed Douyin response while retaining useful timeout diagnostics. */
function waitForDouyinRead(tabId, kind) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      const pending = pendingDouyinReads.get(tabId);
      pendingDouyinReads.delete(tabId);
      const seen = pending?.seenUrls?.slice(-20) || [];
      reject(
        new Error(
          `timeout waiting for Douyin ${kind} response; URLs seen: ${
            seen.join(" | ") || "(none)"
          }`,
        ),
      );
    }, SITE_ACTION_TIMEOUT_MS);
    pendingDouyinReads.set(tabId, { kind, resolve, reject, timer, seenUrls: [] });
  });
}

/** Navigate the real Douyin search UI and capture its signed streaming response. */
async function runDouyinSearch(state, args, reply) {
  const keyword = String(args.keyword || "").trim();
  if (!keyword) {
    reply({ ok: false, error: "douyin search keyword is required" });
    return;
  }
  const target = new URL(`https://www.douyin.com/search/${encodeURIComponent(keyword)}`);
  target.searchParams.set("type", "general");
  await runDouyinObservedRead(state, target, "search", reply);
}

/** Navigate one canonical Douyin post and capture its signed aweme-detail response. */
async function runDouyinVideo(state, args, reply) {
  const awemeId = String(args.awemeId || "").trim();
  const pageKind = String(args.pageKind || "video") === "note" ? "note" : "video";
  if (!/^\d+$/.test(awemeId)) {
    reply({ ok: false, error: "douyin awemeId must contain digits only" });
    return;
  }
  const target = new URL(`https://www.douyin.com/${pageKind}/${awemeId}`);
  if (pageKind === "note") {
    await runDouyinNoteRead(state, target, awemeId, reply);
    return;
  }
  await runDouyinObservedRead(state, target, "video", reply);
}

/** Read one Douyin image post from its server-rendered RENDER_DATA payload. */
async function runDouyinNoteRead(state, target, awemeId, reply) {
  const approval = await requestUrlApproval(state, target.toString());
  if (!approval.allowed) {
    reply({ ok: false, error: `navigation blocked: ${approval.error || "URL policy"}` });
    return;
  }
  let tabId = null;
  try {
    const tab = await chrome.tabs.create({ url: "https://www.douyin.com/", active: false });
    tabId = tab.id;
    if (tabId == null) throw new Error("Chrome did not create a Douyin note tab");
    await waitForTabComplete(tabId);
    await chrome.tabs.update(tabId, { url: target.toString() });
    await waitForTabComplete(tabId);
    await new Promise((resolve) => setTimeout(resolve, 600));
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [awemeId],
      func: (expectedId) => {
        const findAweme = (root) => {
          const queue = [root];
          const visited = new Set();
          while (queue.length) {
            const current = queue.shift();
            if (!current || typeof current !== "object" || visited.has(current)) continue;
            visited.add(current);
            if (
              String(current.aweme_id || current.awemeId || "") === expectedId
              && (Array.isArray(current.images) || current.video || current.desc)
            ) {
              return current;
            }
            if (visited.size > 50_000) break;
            for (const value of Object.values(current)) {
              if (value && typeof value === "object") queue.push(value);
            }
          }
          return null;
        };
        const script = document.querySelector("script#RENDER_DATA");
        const encoded = script?.textContent || "";
        let aweme = null;
        if (encoded) {
          let renderState = null;
          try {
            renderState = JSON.parse(decodeURIComponent(encoded));
          } catch {
            try {
              renderState = JSON.parse(encoded);
            } catch {
              // Current note routes keep post data in React Flight scripts instead.
            }
          }
          if (renderState) aweme = findAweme(renderState);
        }

        if (!aweme) {
          for (const flightScript of document.scripts) {
            const source = flightScript.textContent || "";
            if (!source.includes("self.__pace_f.push") || !source.includes(expectedId)) continue;
            const match = source.match(/self\.__pace_f\.push\(([\s\S]+)\)\s*;?\s*$/);
            if (!match) continue;
            try {
              const pushed = JSON.parse(match[1]);
              const flight = Array.isArray(pushed) ? pushed[1] : null;
              if (typeof flight !== "string") continue;
              const separator = flight.indexOf(":");
              if (separator < 0) continue;
              aweme = findAweme(JSON.parse(flight.slice(separator + 1).trim()));
              if (aweme) break;
            } catch {
              // Flight scripts also carry module records; only JSON data records are relevant.
            }
          }
        }
        if (!aweme) return { error: "Douyin note aweme was not found in page state" };
        const selected = {
          aweme_id: aweme.aweme_id || aweme.awemeId,
          aweme_type: aweme.aweme_type ?? aweme.awemeType,
          desc: aweme.desc,
          create_time: aweme.create_time ?? aweme.createTime,
          duration: aweme.duration,
          author: aweme.author || aweme.authorInfo,
          statistics: aweme.statistics || aweme.stats,
          images: aweme.images,
          video: aweme.video,
          music: aweme.music,
        };
        try {
          return { aweme_detail: JSON.parse(JSON.stringify(selected)) };
        } catch (error) {
          return { error: `Douyin note serialization failed: ${error?.message || error}` };
        }
      },
    });
    if (!result || result.error) {
      throw new Error(result?.error || "Douyin note script returned no data");
    }
    reply({ ok: true, data: { body: JSON.stringify(result) } });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Run one isolated Douyin navigation and return the observed bounded response body. */
async function runDouyinObservedRead(state, target, kind, reply) {
  const approval = await requestUrlApproval(state, target.toString());
  if (!approval.allowed) {
    reply({ ok: false, error: `navigation blocked: ${approval.error || "URL policy"}` });
    return;
  }
  let tabId = null;
  try {
    const tab = await chrome.tabs.create({ url: "https://www.douyin.com/", active: false });
    tabId = tab.id;
    if (tabId == null) throw new Error(`Chrome did not create a Douyin ${kind} tab`);
    await waitForTabComplete(tabId);
    const responseBody = waitForDouyinRead(tabId, kind);
    await chrome.tabs.update(tabId, { url: target.toString() });
    const body = await responseBody;
    reply({ ok: true, data: { body } });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    const pending = tabId == null ? null : pendingDouyinReads.get(tabId);
    if (pending) {
      clearTimeout(pending.timer);
      pendingDouyinReads.delete(tabId);
    }
    if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Resolve pending Douyin reads and retain signed comment pages for active collectors. */
function handleObservedDouyinResponse(message, sender) {
  if (message?.type !== "douyin-response") return false;
  const tabId = sender?.tab?.id;
  const url = String(message.url || "");
  const pendingRead = tabId == null ? null : pendingDouyinReads.get(tabId);
  if (pendingRead) {
    pendingRead.seenUrls.push(url);
    if (matchesDouyinRead(pendingRead.kind, url)) {
      clearTimeout(pendingRead.timer);
      pendingDouyinReads.delete(tabId);
      if (message.truncated === true) {
        pendingRead.reject(new Error("Douyin response exceeded the 4 MiB safety limit"));
      } else if (Number(message.status) < 200 || Number(message.status) >= 300) {
        pendingRead.reject(new Error(`Douyin request returned HTTP ${message.status || 0}`));
      } else {
        pendingRead.resolve(String(message.body || ""));
      }
    }
  }

  const pendingComments = tabId == null ? null : pendingDouyinComments.get(tabId);
  if (
    pendingComments
    && /\/aweme\/v1\/web\/comment\/(?:list|list\/reply)\/(?:\?|$)/.test(url)
  ) {
    if (message.truncated === true) {
      pendingComments.errors.push("Douyin comment response exceeded the 4 MiB safety limit");
    } else if (Number(message.status) < 200 || Number(message.status) >= 300) {
      pendingComments.errors.push(`Douyin comment request returned HTTP ${message.status || 0}`);
    } else {
      pendingComments.responses.push({ url, body: String(message.body || "") });
    }
  }
  return false;
}

/** Keep only fields required by the public comment contract before bridge serialization. */
function compactDouyinComment(rawComment) {
  const comment = rawComment && typeof rawComment === "object" ? rawComment : {};
  const user = comment.user && typeof comment.user === "object" ? comment.user : {};
  const replies = Array.isArray(comment.reply_comment)
    ? comment.reply_comment.map(compactDouyinComment)
    : [];
  return {
    cid: String(comment.cid || comment.comment_id || ""),
    reply_id: String(comment.reply_id || ""),
    reply_comment_id: String(comment.reply_comment_id || ""),
    reply_to_user_name: String(comment.reply_to_user_name || ""),
    text: String(comment.text || ""),
    create_time: Number(comment.create_time) || 0,
    ip_label: String(comment.ip_label || ""),
    digg_count: Number(comment.digg_count) || 0,
    reply_comment_total: Number(comment.reply_comment_total) || 0,
    user: {
      uid: String(user.uid || ""),
      nickname: String(user.nickname || ""),
    },
    reply_comment: replies,
  };
}

/** Add one compact comment and its inline replies to the collector's id set. */
function trackDouyinCommentRecord(rawComment, seenIds) {
  if (!rawComment || typeof rawComment !== "object") return;
  const commentId = String(rawComment.cid || rawComment.comment_id || "");
  if (commentId) seenIds.add(commentId);
  const replies = Array.isArray(rawComment.reply_comment) ? rawComment.reply_comment : [];
  for (const reply of replies) trackDouyinCommentRecord(reply, seenIds);
}

/** Parse newly observed root and reply responses into bounded normalized pages. */
function drainDouyinCommentResponses(pending, awemeId, pages, seenIds, progress) {
  while (pending.responses.length) {
    const response = pending.responses.shift();
    const responseUrl = new URL(response.url, "https://www.douyin.com");
    const responseAwemeId = responseUrl.searchParams.get("aweme_id");
    if (responseAwemeId && responseAwemeId !== awemeId) continue;
    const body = String(response.body || "").trim();
    if (!body) continue;
    let payload;
    try {
      payload = JSON.parse(body);
    } catch (error) {
      throw new Error(
        `Douyin comment JSON parse failed (${body.length} chars, ${responseUrl.pathname}): ${
          error?.message || error
        }`,
      );
    }
    const comments = Array.isArray(payload?.comments) ? payload.comments : [];
    const compactComments = comments.map(compactDouyinComment);
    const kind = /\/comment\/list\/reply\/(?:\?|$)/.test(responseUrl.pathname)
      ? "reply"
      : "root";
    const rootCommentId = responseUrl.searchParams.get("comment_id") || "";
    pages.push({
      kind,
      root_comment_id: rootCommentId,
      payload: {
        comments: compactComments,
        total: Number.isFinite(Number(payload?.total)) ? Number(payload.total) : null,
        cursor: Number(payload?.cursor) || 0,
        has_more: payload?.has_more === true || payload?.has_more === 1,
      },
    });
    for (const comment of compactComments) trackDouyinCommentRecord(comment, seenIds);
    if (kind === "root") {
      progress.sawRootPage = true;
      for (const comment of compactComments) {
        const commentId = String(comment.cid || "");
        if (commentId && Number(comment.reply_comment_total) > 0) {
          progress.replyRoots.add(commentId);
        }
      }
      if (Number.isFinite(Number(payload?.total))) {
        progress.expectedCount = Math.max(progress.expectedCount || 0, Number(payload.total));
      }
      if (payload?.has_more === false || payload?.has_more === 0) progress.rootComplete = true;
    } else if (
      rootCommentId
      && (payload?.has_more === false || payload?.has_more === 0)
    ) {
      progress.completedReplyRoots.add(rootCommentId);
    }
  }
}

/** Return whether every discovered reply stream has reached its signed terminal page. */
function douyinReplyStreamsComplete(progress) {
  return Array.from(progress.replyRoots).every((rootId) => (
    progress.completedReplyRoots.has(rootId)
  ));
}

/** Click one reply expander with a trusted Chrome input event. */
async function clickDouyinReplyExpander(debugTarget, point) {
  try {
    await dispatchTrustedPointClick(debugTarget, point);
  } catch (firstError) {
    if (!/not attached/i.test(String(firstError?.message || firstError))) throw firstError;
    await chrome.debugger.attach(debugTarget, "1.3");
    await dispatchTrustedPointClick(debugTarget, point);
  }
}

/** Advance Douyin's scoped comment stream, using DOM scrolling only as a debugger fallback. */
async function scrollDouyinCommentStream(tabId, debugTarget, ui, deltaY) {
  const command = {
    type: "mouseWheel",
    x: Number(ui.wheelX),
    y: Number(ui.wheelY),
    deltaX: 0,
    deltaY,
  };
  try {
    await chrome.debugger.sendCommand(debugTarget, "Input.dispatchMouseEvent", command);
    return;
  } catch (firstError) {
    if (!/not attached/i.test(String(firstError?.message || firstError))) throw firstError;
  }
  try {
    await chrome.debugger.attach(debugTarget, "1.3");
    await chrome.debugger.sendCommand(debugTarget, "Input.dispatchMouseEvent", command);
    return;
  } catch {}

  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [deltaY],
    func: (delta) => {
      const marker = document.querySelector('[data-scroll="comment"]');
      if (!(marker instanceof HTMLElement)) return false;
      let current = marker;
      while (current instanceof HTMLElement && current !== document.body) {
        const style = getComputedStyle(current);
        if (
          /(auto|scroll)/.test(style.overflowY)
          && current.scrollHeight > current.clientHeight + 40
        ) break;
        current = current.parentElement;
      }
      const scroller = current instanceof HTMLElement && current !== document.body
        ? current
        : marker;
      const before = scroller.scrollTop;
      scroller.scrollBy({ top: delta, behavior: "instant" });
      scroller.dispatchEvent(new Event("scroll", { bubbles: false }));
      return scroller.scrollTop !== before;
    },
  });
  if (result !== true) throw new Error("Douyin comment stream could not be scrolled");
}

/** Collect Douyin comments by scrolling the actual rendered comment stream and expanding replies. */
async function runDouyinComments(state, args, reply) {
  const awemeId = String(args.awemeId || "").trim();
  const pageKind = String(args.pageKind || "video") === "note" ? "note" : "video";
  if (!/^\d+$/.test(awemeId)) {
    reply({ ok: false, error: "douyin awemeId must contain digits only" });
    return;
  }
  const maxComments = Math.max(1, Math.min(5000, Number(args.maxComments) || 500));
  const maxScrolls = Math.max(80, Math.min(320, Math.ceil(maxComments * 0.75)));
  const target = new URL(`https://www.douyin.com/${pageKind}/${awemeId}`);
  const approval = await requestUrlApproval(state, target.toString());
  if (!approval.allowed) {
    reply({ ok: false, error: `navigation blocked: ${approval.error || "URL policy"}` });
    return;
  }

  let tabId = null;
  let windowId = null;
  let debugTarget = null;
  const pages = [];
  const seenIds = new Set();
  const progress = {
    sawRootPage: false,
    rootComplete: false,
    expectedCount: null,
    replyRoots: new Set(),
    completedReplyRoots: new Set(),
  };
  let scrolls = 0;
  let complete = false;
  let limitReached = false;
  let stableRounds = 0;
  let previousHeight = -1;
  let previousCount = -1;
  let scrollDirection = 1;
  let sweepTurns = 0;

  try {
    const createdWindow = await chrome.windows.create({
      url: "https://www.douyin.com/",
      focused: false,
      type: "normal",
    });
    const [tab] = createdWindow.tabs || [];
    tabId = tab?.id;
    windowId = createdWindow.id;
    if (tabId == null || windowId == null) {
      throw new Error("Chrome did not create an isolated Douyin comments window");
    }
    await waitForTabComplete(tabId);
    const pending = { responses: [], errors: [] };
    pendingDouyinComments.set(tabId, pending);
    await chrome.tabs.update(tabId, { url: target.toString() });
    await waitForTabComplete(tabId);
    await new Promise((resolve) => setTimeout(resolve, 1200));
    debugTarget = { tabId };
    await chrome.debugger.attach(debugTarget, "1.3");

    for (scrolls = 0; scrolls < maxScrolls; scrolls += 1) {
      if (pending.errors.length) throw new Error(pending.errors.shift());
      drainDouyinCommentResponses(pending, awemeId, pages, seenIds, progress);

      const [{ result: ui } = {}] = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: () => {
          const marker = document.querySelector('[data-scroll="comment"]');
          const candidates = Array.from(document.querySelectorAll("div, main, section"))
            .filter((element) => {
              if (!(element instanceof HTMLElement) || element.offsetParent === null) return false;
              const style = getComputedStyle(element);
              return /(auto|scroll)/.test(style.overflowY)
                && element.clientHeight >= 160
                && element.scrollHeight > element.clientHeight + 40;
            })
            .map((element) => {
              const text = element.textContent || "";
              let score = 0;
              if (text.includes("全部评论")) score += 200;
              if (text.includes("留下你的精彩评论")) score += 100;
              score += Math.min(50, (text.match(/回复/g) || []).length * 2);
              return { element, score };
            })
            .sort((left, right) => right.score - left.score);
          let scopedScroller = marker;
          while (scopedScroller instanceof HTMLElement && scopedScroller !== document.body) {
            const style = getComputedStyle(scopedScroller);
            if (
              /(auto|scroll)/.test(style.overflowY)
              && scopedScroller.scrollHeight > scopedScroller.clientHeight + 40
            ) break;
            scopedScroller = scopedScroller.parentElement;
          }
          const best = candidates[0];
          const scroller = scopedScroller instanceof HTMLElement && scopedScroller !== document.body
            ? scopedScroller
            : best?.score > 0 ? best.element : null;
          if (!(scroller instanceof HTMLElement)) {
            return { error: "Douyin rendered comment stream was not found" };
          }
          const scrollerRect = scroller.getBoundingClientRect();
          const visibleLeft = Math.max(0, scrollerRect.left);
          const visibleRight = Math.min(innerWidth, scrollerRect.right);
          const visibleTop = Math.max(0, scrollerRect.top);
          const visibleBottom = Math.min(innerHeight, scrollerRect.bottom);
          const commentRegion = marker instanceof HTMLElement ? marker : scroller;
          const expanders = Array.from(new Set(commentRegion.querySelectorAll(
            ".comment-reply-expand-btn, button, [role='button']",
          ))).filter((element) => {
            const text = (element.textContent || "").trim();
            if (
              /收起/.test(text)
              || !/(展开|更多)/.test(text)
              || !(element instanceof HTMLElement)
              || element.offsetParent === null
            ) return false;
            const rect = element.getBoundingClientRect();
            return rect.bottom >= scrollerRect.top && rect.top <= scrollerRect.top + scrollerRect.height;
          });
          const expanderPoints = expanders.slice(0, 1).map((element) => {
            const rect = element.getBoundingClientRect();
            return {
              x: Math.max(1, Math.min(innerWidth - 1, Math.round(rect.left + rect.width / 2))),
              y: Math.max(1, Math.min(innerHeight - 1, Math.round(rect.top + rect.height / 2))),
            };
          });
          return {
            clicked: expanderPoints.length,
            expanderPoints,
            wheelX: Math.max(1, Math.round((visibleLeft + visibleRight) / 2)),
            wheelY: Math.max(1, Math.round((visibleTop + visibleBottom) / 2)),
            clientHeight: scroller.clientHeight,
            scrollHeight: scroller.scrollHeight,
            atTop: scroller.scrollTop <= 4,
            atBottom: scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 4,
            endText: /(暂时没有更多了|没有更多|到底了)/.test(scroller.textContent || ""),
          };
        },
      });
      if (!ui || ui.error) throw new Error(ui?.error || "Douyin comment stream returned no state");
      for (const point of ui.expanderPoints || []) {
        await clickDouyinReplyExpander(debugTarget, point);
      }

      let directionChanged = false;
      const needsReplySweep = progress.rootComplete && !douyinReplyStreamsComplete(progress);
      if (ui.clicked === 0 && needsReplySweep && sweepTurns < 4) {
        if (scrollDirection > 0 && ui.atBottom) {
          scrollDirection = -1;
          sweepTurns += 1;
          directionChanged = true;
        } else if (scrollDirection < 0 && ui.atTop) {
          scrollDirection = 1;
          sweepTurns += 1;
          directionChanged = true;
        }
      }
      if (ui.clicked === 0) {
        const wheelDelta = Math.max(
          320,
          Math.min(500, Math.floor(Number(ui.clientHeight || 0) * 0.6)),
        );
        await scrollDouyinCommentStream(
          tabId,
          debugTarget,
          ui,
          wheelDelta * scrollDirection,
        );
      }

      await new Promise((resolve) => setTimeout(resolve, 750));
      if (pending.errors.length) throw new Error(pending.errors.shift());
      drainDouyinCommentResponses(pending, awemeId, pages, seenIds, progress);

      const unchanged = previousCount === seenIds.size && previousHeight === ui.scrollHeight;
      const atBoundary = scrollDirection > 0 ? ui.atBottom : ui.atTop;
      stableRounds = unchanged && ui.clicked === 0 && atBoundary && !directionChanged
        ? stableRounds + 1
        : 0;
      previousCount = seenIds.size;
      previousHeight = ui.scrollHeight;

      const expectedReached = progress.expectedCount != null
        && seenIds.size >= progress.expectedCount;
      const streamsComplete = progress.rootComplete && douyinReplyStreamsComplete(progress);
      if (seenIds.size >= maxComments && !expectedReached) {
        limitReached = true;
        break;
      }
      if (stableRounds >= 2 && (expectedReached || streamsComplete)) {
        complete = true;
        break;
      }
      if (stableRounds >= 10 || (ui.endText && progress.rootComplete && stableRounds >= 2)) break;
    }

    drainDouyinCommentResponses(pending, awemeId, pages, seenIds, progress);
    if (
      progress.rootComplete
      && douyinReplyStreamsComplete(progress)
      && !limitReached
    ) complete = true;
    reply({
      ok: true,
      data: {
        complete,
        limit_reached: limitReached,
        scrolls: Math.min(scrolls + 1, maxScrolls),
        pages,
      },
    });
  } catch (error) {
    reply({ ok: false, error: String(error?.message || error) });
  } finally {
    if (tabId != null) pendingDouyinComments.delete(tabId);
    if (debugTarget != null) await chrome.debugger.detach(debugTarget).catch(() => {});
    if (windowId != null) await chrome.windows.remove(windowId).catch(() => {});
    else if (tabId != null) await chrome.tabs.remove(tabId).catch(() => {});
  }
}

/** Reconcile live sockets with the latest generated pairing configuration. */
async function connectAll() {
  const config = await loadPairingConfig();
  if (!config?.token) return;
  const wanted = new Set(portsFor(config));

  for (const [port, state] of sockets.entries()) {
    if (wanted.has(port)) continue;
    state.disabled = true;
    if (state.timer) clearTimeout(state.timer);
    state.socket?.close();
    sockets.delete(port);
  }
  for (const port of wanted) connectPort(port, config);
}

chrome.alarms.create("browser-mcp-keepalive", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "browser-mcp-keepalive") connectAll();
});
chrome.runtime.onInstalled.addListener(connectAll);
chrome.runtime.onStartup.addListener(connectAll);
chrome.runtime.onMessage.addListener(handleObservedXhsResponse);
chrome.runtime.onMessage.addListener(handleObservedDouyinResponse);
chrome.debugger.onDetach.addListener((source) => {
  for (const [session, debuggerSession] of interactionDebuggers.entries()) {
    if (debuggerSession.target.tabId !== source.tabId) continue;
    if (debuggerSession.listener) chrome.debugger.onEvent.removeListener(debuggerSession.listener);
    interactionDebuggers.delete(session);
  }
});
chrome.tabs.onRemoved.addListener((tabId) => {
  for (const [session, managedTabId] of interactiveTabs.entries()) {
    if (managedTabId !== tabId) continue;
    interactiveTabs.delete(session);
    interactionWindows.delete(session);
    void closeInteractionDebugger(session);
  }
});
void connectAll();
