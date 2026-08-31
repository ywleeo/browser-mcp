// Browser MCP Bridge — authenticated fetch and visual-interaction worker.
//
// Read adapters use short-lived inactive tabs. Visual tools reuse one isolated background window,
// capture its active tab without focusing the window, and guard every navigation.

import { BUNDLE_BUILD_ID } from "./build-info.js";
import {
  closeAllBackgroundTabs,
  closeBackgroundTab,
  forgetBackgroundTab,
  openBackgroundTab,
} from "./background_tabs.js";
import {
  SWEEP_ALARM_NAME,
  closeAllCommentSessions,
  closeCommentSession,
  commentSessionHydration,
  createCommentSession,
  ensureDebuggerAttached,
  findCommentSession,
  forgetCommentSessionByTab,
  suspendCommentSession,
  sweepCommentSessions,
} from "./comment_sessions.js";

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
// One comment-collection call runs at most this long before suspending into a resumable session.
const DEFAULT_COMMENT_BUDGET_MS = 40000;
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
const lastInteractionScreenshots = new Map();

// The MV3 service worker can be evicted (idle timeout, crash, chrome.runtime.reload() on
// bundle upgrade) at any moment, wiping interactiveTabs/interactionWindows. The window Chrome
// already opened survives that eviction, but nothing remembers it anymore, so the next
// interactionTab() call used to create a second window instead of reusing the first — leaking
// one orphaned window per eviction. chrome.storage.session survives worker restarts (it only
// clears when the browser itself closes), so mirror both maps there and replay them on wake.
const INTERACTION_STATE_KEY = "browserMcpInteractionState";

/** Mirror the in-memory tab/window maps to session storage so a worker restart can recover them. */
async function persistInteractionState() {
  const state = {};
  for (const [session, tabId] of interactiveTabs.entries()) {
    state[session] = { tabId, windowId: interactionWindows.get(session) ?? null };
  }
  try {
    await chrome.storage.session.set({ [INTERACTION_STATE_KEY]: state });
  } catch (error) {
    console.warn("[browser-mcp-extension] failed to persist interaction state", error);
  }
}

/** Record one session's managed tab/window in memory and in session storage together. */
async function setInteractionSession(session, tabId, windowId) {
  interactiveTabs.set(session, tabId);
  if (windowId != null) interactionWindows.set(session, windowId);
  else interactionWindows.delete(session);
  await persistInteractionState();
}

/** Forget one session's managed tab/window in memory and in session storage together. */
async function deleteInteractionSession(session) {
  if (!interactiveTabs.has(session) && !interactionWindows.has(session)) return;
  interactiveTabs.delete(session);
  interactionWindows.delete(session);
  await persistInteractionState();
}

/** Recover tab/window bindings that survived a service-worker restart, dropping any Chrome already closed. */
async function hydrateInteractionState() {
  try {
    const stored = (await chrome.storage.session.get(INTERACTION_STATE_KEY))[INTERACTION_STATE_KEY];
    if (!stored) return;
    for (const [session, entry] of Object.entries(stored)) {
      const tabId = entry?.tabId;
      if (tabId == null) continue;
      try {
        await chrome.tabs.get(tabId);
        interactiveTabs.set(session, tabId);
        if (entry.windowId != null) interactionWindows.set(session, entry.windowId);
      } catch {
        // The tab Chrome remembered is already gone; nothing to recover or close.
      }
    }
  } catch (error) {
    console.warn("[browser-mcp-extension] interaction state hydration failed", error);
  }
}

/** Resolved once recovered tab/window bindings are back in memory; awaited before first use. */
const interactionHydration = hydrateInteractionState();

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

/** Clamp one collection run's wall-clock budget requested by the Python adapter. */
function commentBudgetMs(args) {
  const requested = Number(args.budgetMs);
  if (!Number.isFinite(requested) || requested <= 0) return DEFAULT_COMMENT_BUDGET_MS;
  return Math.max(5000, Math.min(600000, Math.round(requested)));
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
    ...[...lastInteractionScreenshots.keys()].filter((session) => session.startsWith(prefix)),
  ]);
  for (const session of sessions) {
    const windowId = interactionWindows.get(session);
    await deleteInteractionSession(session);
    interactionQueues.delete(session);
    lastInteractionScreenshots.delete(session);
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
      void closeAllCommentSessions();
      void closeAllBackgroundTabs();
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
async function waitForTabComplete(tabId, timeoutMs = PAGE_LOAD_TIMEOUT_MS, signal = null) {
  const tab = await chrome.tabs.get(tabId);
  if (tab.status === "complete" || signal?.aborted) return;
  await new Promise((resolve, reject) => {
    const cleanup = () => {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      signal?.removeEventListener("abort", onAbort);
    };
    const onAbort = () => {
      cleanup();
      resolve();
    };
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error(`tab load timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId !== tabId || changeInfo.status !== "complete") return;
      cleanup();
      resolve();
    };
    chrome.tabs.onUpdated.addListener(listener);
    signal?.addEventListener("abort", onAbort, { once: true });
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
  try {
    const approval = await requestUrlApproval(state, url);
    if (!approval.allowed) {
      throw new Error(approval.error || "navigation blocked by URL policy");
    }

    // Only `xhr` needs chrome.debugger — Network.getResponseBody has no extension-API
    // equivalent — and it must be attached before the target URL is requested, so that mode
    // alone pays for a blank tab plus a second navigation. Every other read navigates
    // directly: attaching would raise Chrome's debugging infobar, and that infobar is global.
    // It is drawn in every window of the profile, so no amount of window isolation hides it
    // from the user's own tabs; the only way not to show it is not to attach.
    const capturesXhr = extract === "xhr";
    const tab = await openBackgroundTab({ url: capturesXhr ? "about:blank" : url });
    tabId = tab.id;
    if (tabId == null) throw new Error("Chrome did not create a fetch tab");

    const requests = new Map();
    const responses = new Map();
    if (capturesXhr) {
      debuggerTarget = { tabId };
      await chrome.debugger.attach(debuggerTarget, "1.3");
      await chrome.debugger.sendCommand(debuggerTarget, "Network.enable", {});
      debuggerListener = (source, method, params) => {
        if (!source || source.tabId !== tabId) return;
        if (method === "Network.requestWillBeSent") {
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
    }

    let loadTimedOut = false;
    try {
      await waitForTabComplete(tabId);
    } catch (error) {
      if (!String(error?.message || error).includes("tab load timeout")) throw error;
      loadTimedOut = true;
    }
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
    await closeBackgroundTab(tabId);
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
    const tab = await openBackgroundTab({ url: apiTarget.toString() });
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
    await closeBackgroundTab(tabId);
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
    const tab = await openBackgroundTab({ url: target.toString() });
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
    await closeBackgroundTab(tabId);
  }
}

/** Execute one visual action and always return the resulting screenshot and element map. */
async function dispatchBrowserInteraction(state, message) {
  const action = String(message.action || "");
  const args = message.args || {};
  const session = `${state.port}:${String(message.tab_id || "default")}`;
  const reply = (payload) =>
    sendJson(state, { type: "browser.interact.result", id: message.id, ...payload });
  if (!["snapshot", "click", "dialog", "scroll", "type", "press", "select"].includes(action)) {
    reply({ ok: false, error: `unsupported browser interaction action: ${action}` });
    return;
  }

  let debuggerSession = null;
  let restoreFocusedWindowId = null;
  let stage = "resolve tab";
  const replyOpenDialog = async (tabId, screenshotState, dialog) => {
    const visual = interactionDialogVisualState(
      await chrome.tabs.get(tabId),
      screenshotState,
      dialog,
    );
    await restoreInteractionWindowFocus(restoreFocusedWindowId);
    restoreFocusedWindowId = null;
    // The session stored in interactionDebuggers must stay attached until the dialog closes.
    debuggerSession = null;
    reply({
      ok: true,
      data: { state: visual.state, screenshot_data: visual.screenshot_data },
    });
  };
  try {
    const tab = await interactionTab(state, message.tab_id, action, args.url);
    const tabId = tab.id;
    if (tabId == null) throw new Error("interactive Chrome tab has no id");
    const pendingDebugger = interactionDebuggers.get(session) || null;
    if (pendingDebugger?.dialog && action !== "dialog") {
      stage = "report pending native dialog";
      await replyOpenDialog(
        tabId,
        lastInteractionScreenshots.get(session) || null,
        pendingDebugger.dialog,
      );
      return;
    }
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

    // Coordinate clicks are deliberately frame-agnostic: the screenshot point is the
    // authority, so clicking must not inspect or mutate iframe DOM. Other semantic actions
    // still remove only inaccessible extension-owned frames before their targeted scripts.
    if (action !== "click" && action !== "dialog") await removeForeignExtensionFrames(tabId);

    const screenshotState = lastInteractionScreenshots.get(session) || null;
    let refreshReason = null;
    let clickPoint = null;
    if (action === "click") {
      try {
        clickPoint = interactionClickPoint(args, screenshotState);
      } catch (error) {
        if (!isStaleInteractionReferenceError(error)) throw error;
        refreshReason = `${error?.message || error}; click skipped and visual state refreshed`;
        console.debug("[browser-mcp-extension] stale click state detected; refreshing", {
          session,
          reason: String(error?.message || error),
        });
      }
    }
    const navigationTarget = action === "click" || action === "dialog"
      ? null
      : await interactionNavigationTarget(tabId, action, args, clickPoint);
    if (navigationTarget) {
      const approval = await requestUrlApproval(state, navigationTarget);
      if (!approval.allowed) {
        throw new Error(`action target blocked: ${approval.error || "URL policy"}`);
      }
    }
    // Keep one debugger session across the action and state capture. Besides guarding
    // navigations, CDP is required for trusted keyboard input and for resolving element
    // boxes inside cross-origin frames without violating the same-origin boundary.
    stage = "attach debugger";
    debuggerSession = await interactionDebuggerSession(state, session, tabId);
    debuggerSession.policyFailure = null;

    if (action === "click" || action === "dialog") {
      stage = "focus managed interaction window";
      restoreFocusedWindowId = await focusManagedInteractionWindow(session, tab);
    }

    if (action === "dialog") {
      stage = "handle native dialog";
      const handled = await executeNativeDialog(debuggerSession, args);
      if (!handled) {
        refreshReason = "No Chrome-native dialog is open; visual state refreshed";
      }
    } else if (!refreshReason) {
      stage = "execute action";
      try {
        await executeInteractionAction(
          tabId,
          action,
          args,
          debuggerSession?.target || null,
          clickPoint,
        );
      } catch (error) {
        if (!debuggerSession?.dialog) {
          if (!isStaleInteractionReferenceError(error)) throw error;
          refreshReason = `${error?.message || error}; action skipped and visual state refreshed`;
          console.debug("[browser-mcp-extension] stale action state detected; refreshing", {
            session,
            action,
            reason: String(error?.message || error),
          });
        }
      }
    }
    if (!refreshReason) {
      stage = "wait for page";
      const settleResult = await settleInteractionTabOrDialog(
        tabId,
        boundedWait(args.wait_ms, action === "snapshot" ? 500 : 300),
        debuggerSession,
      );
      if (settleResult === "dialog" && debuggerSession.dialog) {
        await replyOpenDialog(
          tabId,
          screenshotState,
          debuggerSession.dialog,
        );
        return;
      }
    }
    if (debuggerSession) await Promise.all([...debuggerSession.guardTasks]);
    if (debuggerSession?.policyFailure) {
      throw new Error(`navigation blocked: ${debuggerSession.policyFailure}`);
    }
    if (debuggerSession?.dialog) {
      stage = "report native dialog";
      await replyOpenDialog(
        tabId,
        screenshotState,
        debuggerSession.dialog,
      );
      return;
    }

    stage = "validate resulting page";
    const latestTab = await chrome.tabs.get(tabId);
    const finalUrl = String(latestTab.url || "");
    const finalApproval = await requestUrlApproval(state, finalUrl);
    if (!finalApproval.allowed) {
      throw new Error(`resulting page blocked: ${finalApproval.error || "URL policy"}`);
    }
    stage = "capture resulting state";
    let screenshot;
    try {
      screenshot = await captureInteractionScreenshot(debuggerSession?.target || null);
    } catch (error) {
      if (debuggerSession?.dialog) {
        await replyOpenDialog(tabId, screenshotState, debuggerSession.dialog);
        return;
      }
      throw new Error(`screenshot capture: ${error?.message || error}`);
    }
    let visual;
    try {
      visual = action === "click" && !refreshReason
        ? await captureVisualClickState(
          tabId,
          screenshot,
          debuggerSession?.target || null,
        )
        : await captureInteractionState(
          tabId,
          action,
          screenshot,
          debuggerSession?.target || null,
        );
      if (refreshReason) {
        visual.state.action = action;
        visual.state.warnings = [refreshReason, ...(visual.state.warnings || [])];
      }
    } catch (error) {
      if (debuggerSession?.dialog) {
        await replyOpenDialog(tabId, screenshotState, debuggerSession.dialog);
        return;
      }
      throw new Error(`page-state capture: ${error?.message || error}`);
    }
    if (debuggerSession) {
      await closeInteractionDebugger(session);
      debuggerSession = null;
    }
    await restoreInteractionWindowFocus(restoreFocusedWindowId);
    restoreFocusedWindowId = null;
    lastInteractionScreenshots.set(session, {
      width: screenshot.width,
      height: screenshot.height,
      viewportWidth: visual.state.viewport.width,
      viewportHeight: visual.state.viewport.height,
      elements: visual.state.elements,
      targets: visual.clickTargets,
      screenshotData: screenshot.data,
      state: visual.state,
    });
    reply({
      ok: true,
      data: { state: visual.state, screenshot_data: visual.screenshot_data },
    });
  } catch (error) {
    if (debuggerSession) await closeInteractionDebugger(session);
    await restoreInteractionWindowFocus(restoreFocusedWindowId);
    reply({ ok: false, error: `${action} failed during ${stage}: ${error?.message || error}` });
  }
}

/** Focus only an extension-owned interaction window and remember the user's prior window. */
async function focusManagedInteractionWindow(session, tab) {
  const managedWindowId = interactionWindows.get(session);
  if (managedWindowId == null || managedWindowId !== tab.windowId) return null;
  const previous = await chrome.windows.getLastFocused({ windowTypes: ["normal"] }).catch(() => null);
  if (previous?.id === managedWindowId) return null;
  await chrome.windows.update(managedWindowId, { focused: true });
  // Let Chrome finish activating the page surface before trusted input is dispatched.
  // The screenshot itself is captured through CDP, so this focus transition cannot
  // introduce browser-window chrome into the visual coordinate system.
  await new Promise((resolve) => setTimeout(resolve, 80));
  return previous?.id ?? null;
}

/** Restore the browser window that was focused before one trusted managed click. */
async function restoreInteractionWindowFocus(windowId) {
  if (windowId == null) return;
  await chrome.windows.update(windowId, { focused: true }).catch(() => {});
}

/** Resolve an element reference or screenshot coordinate to one CSS viewport point. */
function interactionClickPoint(args, screenshotState) {
  if (typeof args.element_id === "string") {
    if (!screenshotState || !Array.isArray(screenshotState.elements)) {
      throw new Error("element clicks require a fresh browser_snapshot");
    }
    const element = screenshotState.elements.find(
      (candidate) => candidate.element_id === args.element_id,
    );
    if (!element) {
      throw new Error(`element reference is stale: ${args.element_id}; take a new snapshot`);
    }
    if (element.disabled) throw new Error(`element is disabled: ${args.element_id}`);
    const x = Number(element.x) + Number(element.width) / 2;
    const y = Number(element.y) + Number(element.height) / 2;
    if (
      !Number.isFinite(x)
      || !Number.isFinite(y)
      || x < 0
      || y < 0
      || x >= screenshotState.viewportWidth
      || y >= screenshotState.viewportHeight
    ) {
      throw new Error(`element is outside the current screenshot: ${args.element_id}`);
    }
    return {
      x,
      y,
    };
  }
  const x = Number(args.x);
  const y = Number(args.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    throw new Error("browser_click requires finite x and y coordinates");
  }
  const coordinateSpace = String(args.coordinate_space || "screenshot");
  if (!screenshotState) {
    throw new Error("click coordinates require a fresh browser_snapshot");
  }
  if (coordinateSpace === "viewport") return { x, y };
  if (coordinateSpace !== "screenshot") {
    throw new Error(`unsupported click coordinate space: ${coordinateSpace}`);
  }
  if (
    !screenshotState
    || screenshotState.width <= 0
    || screenshotState.height <= 0
    || screenshotState.viewportWidth <= 0
    || screenshotState.viewportHeight <= 0
  ) {
    throw new Error("screenshot coordinates require a fresh browser_snapshot");
  }
  if (x < 0 || y < 0 || x >= screenshotState.width || y >= screenshotState.height) {
    throw new Error(
      `click coordinate is outside screenshot ${screenshotState.width}x${screenshotState.height}`,
    );
  }
  const scaleX = screenshotState.width / screenshotState.viewportWidth;
  const scaleY = screenshotState.height / screenshotState.viewportHeight;
  const scaleDifference = Math.abs(scaleX - scaleY) / Math.max(scaleX, scaleY);
  if (!Number.isFinite(scaleDifference) || scaleDifference > 0.01) {
    throw new Error("screenshot and viewport coordinate systems are not aligned; take a new snapshot");
  }
  return {
    x: x / scaleX,
    y: y / scaleY,
  };
}

/** Return whether one failed action is safe to replace with a fresh visual snapshot. */
function isStaleInteractionReferenceError(error) {
  const message = String(error?.message || error).toLowerCase();
  return message.includes("require a fresh browser_snapshot")
    || message.includes("requires a fresh browser_snapshot")
    || message.includes("element reference is stale")
    || message.includes("element not found");
}

/** Return the explicit anchor or form destination associated with one interaction. */
async function interactionNavigationTarget(tabId, action, args, clickPoint = null) {
  if (action === "snapshot" && typeof args.url === "string") return args.url;
  const maySubmit = action === "click"
    || (action === "type" && args.submit === true)
    || (action === "press" && args.key === "Enter");
  if (!maySubmit) return null;
  const elementId = typeof args.element_id === "string" ? args.element_id : null;
  const x = clickPoint && Number.isFinite(clickPoint.x) ? Number(clickPoint.x) : null;
  const y = clickPoint && Number.isFinite(clickPoint.y) ? Number(clickPoint.y) : null;
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
    session,
    target,
    guardTasks: new Set(),
    policyFailure: null,
    dialog: null,
    dialogOpened: null,
    resolveDialogOpened: null,
    listener: null,
  };
  resetInteractionDialogSignal(debuggerSession);
  debuggerSession.listener = (source, method, params) => {
    if (!source || source.tabId !== tabId) return;
    if (method === "Page.javascriptDialogOpening") {
      debuggerSession.dialog = {
        type: String(params?.type || "alert"),
        message: String(params?.message || "").slice(0, 10_000),
        default_prompt: String(params?.defaultPrompt || "").slice(0, 10_000),
      };
      console.debug("[browser-mcp-extension] native dialog opened", {
        session,
        type: debuggerSession.dialog.type,
      });
      debuggerSession.resolveDialogOpened?.("dialog");
      return;
    }
    if (method === "Page.javascriptDialogClosed") {
      console.debug("[browser-mcp-extension] native dialog closed; visual state invalidated", {
        session,
        accepted: Boolean(params?.result),
      });
      debuggerSession.dialog = null;
      lastInteractionScreenshots.delete(session);
      resetInteractionDialogSignal(debuggerSession);
      return;
    }
    if (method !== "Fetch.requestPaused") return;
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

/** Reset the one-shot signal used to interrupt page settling when a dialog opens. */
function resetInteractionDialogSignal(debuggerSession) {
  debuggerSession.dialogOpened = new Promise((resolve) => {
    debuggerSession.resolveDialogOpened = resolve;
  });
}

/** Accept or dismiss the currently open Chrome-native dialog exactly once. */
async function executeNativeDialog(debuggerSession, args) {
  if (!debuggerSession?.dialog) return false;
  const action = String(args.action || "");
  if (!["accept", "dismiss"].includes(action)) {
    throw new Error(`unsupported native dialog action: ${action}`);
  }
  const parameters = { accept: action === "accept" };
  if (action === "accept" && typeof args.prompt_text === "string") {
    parameters.promptText = args.prompt_text;
  }
  await chrome.debugger.sendCommand(
    debuggerSession.target,
    "Page.handleJavaScriptDialog",
    parameters,
  );
  debuggerSession.dialog = null;
  lastInteractionScreenshots.delete(debuggerSession.session);
  resetInteractionDialogSignal(debuggerSession);
  return true;
}

/** Return the last page surface plus metadata for one blocking browser-native dialog. */
function interactionDialogVisualState(tab, screenshotState, dialog) {
  if (!screenshotState?.screenshotData || !screenshotState?.state) {
    throw new Error(
      "Chrome-native dialog opened before a visual state was saved; use browser_dialog to handle it",
    );
  }
  const state = structuredClone(screenshotState.state);
  state.action = "dialog";
  state.url = String(tab.url || state.url || "");
  state.title = String(tab.title || state.title || "");
  state.elements = [];
  state.dialog = dialog;
  state.warnings = [
    "Chrome-native dialog blocks page input; use browser_dialog before another page action.",
    ...(state.warnings || []),
  ];
  return { state, screenshot_data: screenshotState.screenshotData };
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
  await interactionHydration;
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
        await deleteInteractionSession(session);
        lastInteractionScreenshots.delete(session);
        await closeInteractionDebugger(session);
      } else if (!(action === "snapshot" && typeof requestedUrl === "string")) {
        throw new Error("managed tab is no longer on a public webpage; provide url to browser_snapshot");
      } else {
        await deleteInteractionSession(session);
        lastInteractionScreenshots.delete(session);
        await closeInteractionDebugger(session);
        if (isolatedWindowId === existing.windowId) {
          await chrome.tabs.remove(existingTabId).catch(() => {});
        }
      }
    } catch {
      await deleteInteractionSession(session);
      lastInteractionScreenshots.delete(session);
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
    await setInteractionSession(session, created.id, createdWindow.id);
    return created;
  }
  const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (active?.id == null || !/^https?:\/\//i.test(String(active.url || ""))) {
    throw new Error("no public webpage is active; provide url to browser_snapshot")
  }
  await setInteractionSession(session, active.id, null);
  return active;
}

/** Execute the requested action while the debugger navigation guard is attached. */
async function executeInteractionAction(tabId, action, args, debuggerTarget, clickPoint) {
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
    await executeTrustedClick(debuggerTarget, clickPoint);
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
        debuggerTarget,
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

/** Return whether one frame is a webpage rather than a browser or password-manager extension. */
function interactionFrameIsInjectable(frame) {
  const locations = [frame?.url, frame?.securityOrigin, frame?.unreachableUrl]
    .map((value) => String(value || ""));
  return !locations.some((location) => (
    /^(chrome-extension|moz-extension|safari-web-extension|chrome|devtools|edge|extension):/i
      .test(location)
  ));
}

/** Return whether Chrome rejected traversal because another extension owns one child frame. */
function isForeignExtensionFrameError(error) {
  const message = String(error?.message || error).toLowerCase();
  return message.includes("chrome-extension://") && message.includes("different extension");
}

/** Remove foreign-extension iframe containers while preserving every ordinary webpage frame. */
async function removeForeignExtensionFrames(tabId) {
  const tab = await chrome.tabs.get(tabId);
  if (!/^https?:\/\//i.test(String(tab.url || ""))) return;
  await executeInteractionFrames(tabId, {
    world: "MAIN",
    func: () => {
      const roots = [document];
      for (let index = 0; index < roots.length; index += 1) {
        for (const element of roots[index].querySelectorAll("*")) {
          if (element.shadowRoot) roots.push(element.shadowRoot);
        }
      }
      let removed = 0;
      for (const root of roots) {
        for (const frame of root.querySelectorAll("iframe[src],frame[src]")) {
          const source = String(frame.getAttribute("src") || frame.src || "");
          if (!/^(chrome-extension|moz-extension|safari-web-extension):/i.test(source)) continue;
          frame.remove();
          removed += 1;
        }
      }
      return { removed };
    },
  }, true);
}

/** Execute one page function in each accessible frame without one foreign extension aborting all. */
async function executeInteractionFrames(tabId, injection, allFrames) {
  if (!allFrames) {
    return chrome.scripting.executeScript({
      ...injection,
      target: { tabId },
    });
  }
  const frames = await chrome.webNavigation.getAllFrames({ tabId });
  const orderedFrames = [...(frames || [])].sort(
    (left, right) => Number(left.frameId !== 0) - Number(right.frameId !== 0),
  );
  const executions = [];
  for (const frame of orderedFrames) {
    if (!interactionFrameIsInjectable(frame)) continue;
    try {
      const frameExecutions = await chrome.scripting.executeScript({
        ...injection,
        target: { tabId, frameIds: [frame.frameId] },
      });
      executions.push(...frameExecutions);
    } catch (error) {
      if (frame.frameId === 0) throw error;
    }
  }
  return executions;
}

/** Flatten one CDP frame tree in top-frame-first order. */
function flattenInteractionFrameTree(frameTree) {
  const frames = [];
  const queue = frameTree ? [frameTree] : [];
  while (queue.length) {
    const current = queue.shift();
    if (current?.frame) frames.push(current.frame);
    if (Array.isArray(current?.childFrames)) queue.push(...current.childFrames);
  }
  return frames;
}

/** Resolve snapshot references in isolated contexts without piercing foreign extension frames. */
async function readInteractionBackendNodes(debuggerTarget) {
  const references = new Map();
  if (!debuggerTarget) return { references };
  const { frameTree } = await chrome.debugger.sendCommand(debuggerTarget, "Page.getFrameTree", {});
  const expression = `(() => {
    const roots = [document];
    for (let index = 0; index < roots.length; index += 1) {
      for (const element of roots[index].querySelectorAll("*")) {
        if (element.shadowRoot) roots.push(element.shadowRoot);
      }
    }
    return roots.flatMap((root) => [
      ...root.querySelectorAll("[data-browser-mcp-ref]"),
    ]);
  })()`;
  for (const frame of flattenInteractionFrameTree(frameTree)) {
    if (!interactionFrameIsInjectable(frame)) continue;
    try {
      const world = await chrome.debugger.sendCommand(debuggerTarget, "Page.createIsolatedWorld", {
        frameId: frame.id,
        worldName: "browser-mcp-interaction",
      });
      const evaluation = await chrome.debugger.sendCommand(debuggerTarget, "Runtime.evaluate", {
        expression,
        contextId: world.executionContextId,
        returnByValue: false,
      });
      const arrayObjectId = evaluation?.result?.objectId;
      if (!arrayObjectId) continue;
      const properties = await chrome.debugger.sendCommand(debuggerTarget, "Runtime.getProperties", {
        objectId: arrayObjectId,
        ownProperties: true,
      });
      for (const property of properties?.result || []) {
        const objectId = property?.value?.objectId;
        if (!objectId || !/^\d+$/.test(String(property.name || ""))) continue;
        const description = await chrome.debugger.sendCommand(debuggerTarget, "DOM.describeNode", {
          objectId,
          depth: 0,
        });
        const node = description?.node;
        const attributes = Array.isArray(node?.attributes) ? node.attributes : [];
        const referenceIndex = attributes.indexOf("data-browser-mcp-ref");
        if (referenceIndex < 0 || node?.backendNodeId == null) continue;
        references.set(String(attributes[referenceIndex + 1]), {
          backendNodeId: node.backendNodeId,
          executionContextId: world.executionContextId,
          objectId,
        });
      }
    } catch {
      // Sandboxed and browser-owned frames are intentionally absent from the element map.
    }
  }
  return { references };
}

/** Return the viewport box CDP reports in main-frame CSS coordinates. */
async function interactionBackendBox(debuggerTarget, target) {
  const { model } = await chrome.debugger.sendCommand(debuggerTarget, "DOM.getBoxModel", {
    objectId: target.objectId,
  });
  const quad = model?.border || model?.content;
  if (!Array.isArray(quad) || quad.length < 8) return null;
  const xs = [quad[0], quad[2], quad[4], quad[6]].map(Number);
  const ys = [quad[1], quad[3], quad[5], quad[7]].map(Number);
  const left = Math.min(...xs);
  const right = Math.max(...xs);
  const top = Math.min(...ys);
  const bottom = Math.max(...ys);
  if (![left, right, top, bottom].every(Number.isFinite)) return null;
  return { left, top, right, bottom };
}

/** Send one trusted Chrome mouse click to a point captured in the latest screenshot. */
async function executeTrustedClick(debuggerTarget, clickPoint) {
  if (!debuggerTarget) throw new Error("trusted click requires an attached Chrome debugger");
  if (!clickPoint || !Number.isFinite(clickPoint.x) || !Number.isFinite(clickPoint.y)) {
    throw new Error("trusted click requires a point from the latest screenshot");
  }
  await dispatchTrustedPointerMove(debuggerTarget, clickPoint);
  const hoverNode = await readInteractionHoverNode(debuggerTarget, clickPoint);
  console.debug("[browser-mcp-extension] trusted visual click", {
    x: clickPoint.x,
    y: clickPoint.y,
    hoverBackendNodeId: hoverNode?.backendNodeId ?? null,
  });
  await dispatchTrustedPointClick(debuggerTarget, clickPoint, false);
}

/** Read only the first topmost hover node at one screenshot-derived coordinate. */
async function readInteractionHoverNode(debuggerTarget, point) {
  const hit = await chrome.debugger.sendCommand(debuggerTarget, "DOM.getNodeForLocation", {
    x: Math.round(point.x),
    y: Math.round(point.y),
    includeUserAgentShadowDOM: true,
  });
  const hitBackendNodeId = Number(hit?.backendNodeId);
  return Number.isInteger(hitBackendNodeId) && hitBackendNodeId > 0
    ? { backendNodeId: hitBackendNodeId }
    : null;
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

/** Enter text through Chrome's trusted input pipeline after selecting the live editor range. */
async function enterText(tabId, elementId, text, clear, debuggerTarget) {
  if (!debuggerTarget) throw new Error("trusted text input requires an attached Chrome debugger");
  const executions = await executeInteractionFrames(tabId, {
    world: "MAIN",
    args: [elementId, clear],
    func: (targetId, shouldClear) => {
      const roots = [document];
      for (let index = 0; index < roots.length; index += 1) {
        for (const candidate of roots[index].querySelectorAll("*")) {
          if (candidate.shadowRoot) roots.push(candidate.shadowRoot);
        }
      }
      const escaped = CSS.escape(targetId);
      let element = null;
      for (const root of roots) {
        element = root.querySelector(`[data-browser-mcp-ref="${escaped}"]`);
        if (element) break;
      }
      if (!(element instanceof HTMLElement)) return { skipped: true };
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
        if (shouldClear) {
          element.select();
        } else {
          try {
            element.setSelectionRange(element.value.length, element.value.length);
          } catch {
            // Email, number and other textual controls can reject setSelectionRange.
          }
        }
      } else {
        const selection = getSelection();
        const range = document.createRange();
        range.selectNodeContents(element);
        range.collapse(!shouldClear);
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
      return { ok: true };
    },
  }, true);
  const result = executions.map((execution) => execution.result).find(
    (candidate) => candidate && !candidate.skipped,
  );
  if (!result || result.error) throw new Error(result?.error || "text target unavailable");
  try {
    await chrome.debugger.sendCommand(debuggerTarget, "Input.insertText", { text });
  } catch (error) {
    if (!String(error?.message || error).toLowerCase().includes("method")) throw error;
    await chrome.debugger.sendCommand(debuggerTarget, "Input.dispatchKeyEvent", {
      type: "char",
      text,
      unmodifiedText: text,
    });
  }
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
async function settleInteractionTab(tabId, waitMs, signal = null) {
  try {
    await waitForTabComplete(tabId, PAGE_LOAD_TIMEOUT_MS, signal);
  } catch (error) {
    if (!String(error?.message || error).includes("tab load timeout")) throw error;
  }
  if (waitMs > 0 && !signal?.aborted) {
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, waitMs);
      signal?.addEventListener("abort", () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
    });
  }
}

/** Settle page rendering unless a Chrome-native dialog starts blocking the tab. */
async function settleInteractionTabOrDialog(tabId, waitMs, debuggerSession) {
  if (debuggerSession?.dialog) return "dialog";
  if (!debuggerSession?.dialogOpened) {
    await settleInteractionTab(tabId, waitMs);
    return "settled";
  }
  const controller = new AbortController();
  const result = await Promise.race([
    settleInteractionTab(tabId, waitMs, controller.signal).then(() => "settled"),
    debuggerSession.dialogOpened,
  ]);
  if (result === "dialog") controller.abort();
  return result;
}

/** Clamp agent-controlled rendering waits to the documented interaction limit. */
function boundedWait(value, fallback) {
  if (!Number.isFinite(value)) return fallback;
  return Math.max(0, Math.min(30000, Number(value)));
}

/** Capture the page surface that shares coordinates with trusted CDP mouse input. */
async function captureInteractionScreenshot(debuggerTarget) {
  if (!debuggerTarget) throw new Error("page-surface screenshot requires an attached Chrome debugger");
  const captured = await chrome.debugger.sendCommand(debuggerTarget, "Page.captureScreenshot", {
    format: "jpeg",
    quality: 80,
    fromSurface: true,
    captureBeyondViewport: false,
  });
  const screenshotData = String(captured?.data || "");
  if (!screenshotData) throw new Error("Chrome returned no screenshot data");
  const dataUrl = `data:image/jpeg;base64,${screenshotData}`;
  const blob = await (await fetch(dataUrl)).blob();
  const bitmap = await createImageBitmap(blob);
  const width = bitmap.width;
  const height = bitmap.height;
  bitmap.close();
  if (width <= 0 || height <= 0) throw new Error("Chrome returned an invalid screenshot size");
  return { data: screenshotData, width, height };
}

/** Return a screenshot-only click result using layout metrics without page DOM traversal. */
async function captureVisualClickState(tabId, screenshot, debuggerTarget) {
  if (!debuggerTarget) throw new Error("visual click capture requires an attached Chrome debugger");
  const [tab, metrics] = await Promise.all([
    chrome.tabs.get(tabId),
    chrome.debugger.sendCommand(debuggerTarget, "Page.getLayoutMetrics", {}),
  ]);
  const viewport = metrics?.cssVisualViewport || metrics?.visualViewport || {};
  const content = metrics?.cssContentSize || metrics?.contentSize || {};
  const width = Math.max(1, Number(viewport.clientWidth) || screenshot.width);
  const height = Math.max(1, Number(viewport.clientHeight) || screenshot.height);
  return {
    state: {
      action: "click",
      url: String(tab.url || ""),
      title: String(tab.title || ""),
      screenshot_mime_type: "image/jpeg",
      viewport: {
        width,
        height,
        screenshot_width: screenshot.width,
        screenshot_height: screenshot.height,
        device_scale_factor: Math.max(0.1, screenshot.width / width),
        scroll_x: Math.max(0, Math.round(Number(viewport.pageX) || 0)),
        scroll_y: Math.max(0, Math.round(Number(viewport.pageY) || 0)),
        document_width: Math.max(width, Math.round(Number(content.width) || width)),
        document_height: Math.max(height, Math.round(Number(content.height) || height)),
      },
      elements: [],
      visible_text: "",
      warnings: ["Visual click result omits DOM metadata; take a new snapshot before a semantic action."],
    },
    screenshot_data: screenshot.data,
    clickTargets: {},
  };
}

/** Extract a fresh, frame-aware element map after the screenshot has completed. */
async function captureInteractionState(tabId, action, screenshot, debuggerTarget) {
  let executions;
  try {
    executions = await executeInteractionFrames(tabId, {
      world: "MAIN",
      args: [INTERACTION_ELEMENT_LIMIT, INTERACTION_TEXT_LIMIT],
      func: (elementLimit, textLimit) => {
      const referenceAttribute = "data-browser-mcp-ref";
      const referencePrefix = crypto.randomUUID().slice(0, 8);
      const roots = [document];
      for (let index = 0; index < roots.length; index += 1) {
        for (const element of roots[index].querySelectorAll("*")) {
          if (element.shadowRoot) roots.push(element.shadowRoot);
        }
      }
      for (const root of roots) {
        for (const previous of root.querySelectorAll(`[${referenceAttribute}]`)) {
          previous.removeAttribute(referenceAttribute);
        }
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
        const nameRoot = element.getRootNode();
        const labelled = labelledBy
          ? labelledBy.split(/\s+/).map(
            (id) => nameRoot.getElementById?.(id)?.innerText || document.getElementById(id)?.innerText || "",
          ).join(" ")
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
      const candidates = roots.flatMap((root) => [...root.querySelectorAll(selectors)]);
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
    }, true);
  } catch (error) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    throw new Error(
      `page-state script failed at ${String(tab?.url || "closed")}: ${error?.message || error}`,
    );
  }
  const mainExecution = executions.find((execution) => execution.frameId === 0) || executions[0];
  const page = mainExecution?.result;
  if (!page || typeof page.url !== "string") throw new Error("page snapshot script returned no data");
  if (!debuggerTarget) throw new Error("page snapshot requires an attached Chrome debugger");

  let backendNodes;
  let skippedForeignExtensionFrames = false;
  try {
    backendNodes = await readInteractionBackendNodes(debuggerTarget);
  } catch (error) {
    if (!isForeignExtensionFrameError(error)) throw error;
    backendNodes = { references: new Map() };
    skippedForeignExtensionFrames = true;
  }
  const resolvedElements = [];
  const clickTargets = {};
  let unresolvedCount = 0;
  const orderedExecutions = [
    mainExecution,
    ...executions.filter((execution) => execution !== mainExecution),
  ];
  for (const execution of orderedExecutions) {
    if (skippedForeignExtensionFrames && execution !== mainExecution) continue;
    for (const element of execution?.result?.elements || []) {
      if (resolvedElements.length >= INTERACTION_ELEMENT_LIMIT) break;
      if (skippedForeignExtensionFrames) {
        resolvedElements.push(element);
        continue;
      }
      const target = backendNodes.references.get(String(element.element_id));
      if (!target) {
        unresolvedCount += 1;
        continue;
      }
      let box = null;
      try {
        box = await interactionBackendBox(debuggerTarget, target);
      } catch {
        unresolvedCount += 1;
        continue;
      }
      if (
        !box
        || box.right <= 0
        || box.bottom <= 0
        || box.left >= page.viewport.width
        || box.top >= page.viewport.height
      ) continue;
      resolvedElements.push({
        ...element,
        x: Math.round(box.left * 10) / 10,
        y: Math.round(box.top * 10) / 10,
        width: Math.round((box.right - box.left) * 10) / 10,
        height: Math.round((box.bottom - box.top) * 10) / 10,
      });
      clickTargets[element.element_id] = target.backendNodeId;
    }
    if (resolvedElements.length >= INTERACTION_ELEMENT_LIMIT) break;
  }

  const frameText = orderedExecutions
    .map((execution) => String(execution?.result?.visible_text || ""))
    .filter(Boolean)
    .join("\n")
    .slice(0, INTERACTION_TEXT_LIMIT);
  const frameWarnings = orderedExecutions.flatMap(
    (execution) => execution?.result?.warnings || [],
  );
  if (skippedForeignExtensionFrames) {
    frameWarnings.push(
      "Child frames were skipped because a browser extension owns an inaccessible frame.",
    );
  }
  if (unresolvedCount > 0) {
    frameWarnings.push(
      `${unresolvedCount} frame or shadow-root elements could not be mapped to the screenshot.`,
    );
  }
  if (resolvedElements.length >= INTERACTION_ELEMENT_LIMIT) {
    frameWarnings.push(`Visible element list limited to ${INTERACTION_ELEMENT_LIMIT} across all frames.`);
  }
  page.elements = resolvedElements;
  page.visible_text = frameText;
  page.warnings = [...new Set(frameWarnings)];
  page.viewport.screenshot_width = screenshot.width;
  page.viewport.screenshot_height = screenshot.height;
  return {
    state: {
      action,
      ...page,
      screenshot_mime_type: "image/jpeg",
    },
    screenshot_data: screenshot.data,
    clickTargets,
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
    const tab = await openBackgroundTab({ url: "https://www.zhihu.com/" });
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
    await closeBackgroundTab(tabId);
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
    const tab = await openBackgroundTab({ url: "https://www.zhihu.com/" });
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
    await closeBackgroundTab(tabId);
  }
}

/** Move Chrome's trusted mouse pointer to one CSS viewport point. */
async function dispatchTrustedPointerMove(debugTarget, point) {
  const position = { x: Number(point.x), y: Number(point.y) };
  await chrome.debugger.sendCommand(debugTarget, "Input.dispatchMouseEvent", {
    type: "mouseMoved",
    button: "none",
    buttons: 0,
    ...position,
  });
}

/** Dispatch one trusted click at an already validated viewport point without retrying it. */
async function dispatchTrustedPointClick(debugTarget, point, movePointer = true) {
  const position = { x: Number(point.x), y: Number(point.y) };
  if (movePointer) await dispatchTrustedPointerMove(debugTarget, position);
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
    const tab = await openBackgroundTab({ url: "https://www.xiaohongshu.com/explore" });
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
    await closeBackgroundTab(tabId);
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
  const deadline = Date.now() + commentBudgetMs(args);
  const requestedSessionId = String(args.sessionId || "").trim();

  await commentSessionHydration;
  let session = requestedSessionId
    ? findCommentSession(requestedSessionId, { platform: "xhs", fingerprint: noteId })
    : null;
  if (requestedSessionId && !session) {
    reply({
      ok: false,
      error: `xhs comment session ${requestedSessionId} expired or belongs to another note; `
        + "call again without a session id to restart collection",
    });
    return;
  }

  const pages = [];
  const seenIds = session ? session.seen : new Set();
  const progress = session ? session.progress : { sawRootPage: false, rootComplete: false };
  const loop = session ? session.loop : {
    expectedCount: null,
    scrolls: 0,
    stableRounds: 0,
    previousHeight: -1,
    previousCount: -1,
    scrollDirection: 1,
    sweepTurns: 0,
  };
  let tabId = session ? session.tabId : null;
  let debugTarget = null;
  let complete = false;
  let limitReached = false;
  let budgetExhausted = false;

  try {
    let pending;
    if (session) {
      pending = pendingXhsComments.get(tabId);
      if (!pending) {
        pending = { responses: [], errors: [] };
        pendingXhsComments.set(tabId, pending);
      }
      debugTarget = { tabId };
      await ensureDebuggerAttached(debugTarget);
    } else {
      const target = new URL(`https://www.xiaohongshu.com/explore/${encodeURIComponent(noteId)}`);
      if (xsecToken) target.searchParams.set("xsec_token", xsecToken);
      if (xsecSource) target.searchParams.set("xsec_source", xsecSource);
      const approval = await requestUrlApproval(state, target.toString());
      if (!approval.allowed) {
        reply({ ok: false, error: `navigation blocked: ${approval.error || "URL policy"}` });
        return;
      }
      const createdWindow = await chrome.windows.create({
        url: "https://www.xiaohongshu.com/explore",
        focused: false,
        type: "normal",
      });
      const [tab] = createdWindow.tabs || [];
      tabId = tab?.id ?? null;
      if (tabId == null || createdWindow.id == null) {
        if (createdWindow.id != null) await chrome.windows.remove(createdWindow.id).catch(() => {});
        throw new Error("Chrome did not create an isolated XHS comments window");
      }
      // Own the window through the session from here on, so every later failure closes it once.
      session = await createCommentSession({
        platform: "xhs",
        fingerprint: noteId,
        tabId,
        windowId: createdWindow.id,
        seen: seenIds,
        progress,
        loop,
        release: (closed) => pendingXhsComments.delete(closed.tabId),
      });
      await waitForTabComplete(tabId);
      pending = { responses: [], errors: [] };
      pendingXhsComments.set(tabId, pending);
      await chrome.tabs.update(tabId, { url: target.toString() });
      await waitForTabComplete(tabId);
      await new Promise((resolve) => setTimeout(resolve, 1200));
      debugTarget = { tabId };
      await ensureDebuggerAttached(debugTarget);
    }

    while (loop.scrolls < maxScrolls) {
      if (Date.now() >= deadline) {
        budgetExhausted = true;
        break;
      }
      loop.scrolls += 1;
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
      if (Number.isFinite(ui.expectedCount)) loop.expectedCount = Number(ui.expectedCount);

      let directionChanged = false;
      const needsReplySweep = loop.expectedCount != null
        && seenIds.size < loop.expectedCount
        && progress.rootComplete;
      if (ui.clicked === 0 && needsReplySweep && loop.sweepTurns < 4) {
        if (loop.scrollDirection > 0 && ui.atBottom) {
          loop.scrollDirection = -1;
          loop.sweepTurns += 1;
          directionChanged = true;
        } else if (loop.scrollDirection < 0 && ui.atTop) {
          loop.scrollDirection = 1;
          loop.sweepTurns += 1;
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
          deltaY: wheelDelta * loop.scrollDirection,
        });
      }

      await new Promise((resolve) => setTimeout(resolve, 800));
      if (pending.errors.length) throw new Error(pending.errors.shift());
      drainXhsCommentResponses(pending, noteId, pages, seenIds, progress);

      const unchanged = loop.previousCount === seenIds.size
        && loop.previousHeight === ui.scrollHeight;
      const atSweepBoundary = loop.scrollDirection > 0 ? ui.atBottom : ui.atTop;
      loop.stableRounds = unchanged && ui.clicked === 0 && atSweepBoundary && !directionChanged
        ? loop.stableRounds + 1
        : 0;
      loop.previousCount = seenIds.size;
      loop.previousHeight = ui.scrollHeight;

      const expectedReached = loop.expectedCount != null && seenIds.size >= loop.expectedCount;
      const reachedNaturalEnd = loop.expectedCount != null
        ? expectedReached
        : progress.rootComplete || ui.endText;
      if (seenIds.size >= maxComments && !expectedReached) {
        limitReached = true;
        break;
      }
      if (
        loop.stableRounds >= 2
        && ui.clicked === 0
        && reachedNaturalEnd
      ) {
        complete = true;
        break;
      }
      if (loop.stableRounds >= 10) break;
    }

    drainXhsCommentResponses(pending, noteId, pages, seenIds, progress);
    if (loop.expectedCount != null && seenIds.size >= loop.expectedCount && !limitReached) {
      complete = true;
    }
    // Only a budget stop is resumable: every other exit means this note has nothing left to give.
    const suspended = budgetExhausted && !complete && !limitReached;
    if (suspended) await suspendCommentSession(session);
    else await closeCommentSession(session);
    reply({
      ok: true,
      data: {
        expected_count: loop.expectedCount,
        complete,
        limit_reached: limitReached,
        budget_exhausted: budgetExhausted,
        session_id: suspended ? session.id : null,
        collected_total: seenIds.size,
        scrolls: loop.scrolls,
        pages,
      },
    });
  } catch (error) {
    if (session) await closeCommentSession(session);
    reply({ ok: false, error: String(error?.message || error) });
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
    const tab = await openBackgroundTab({ url: "https://www.xiaohongshu.com/explore" });
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
    await closeBackgroundTab(tabId);
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
    const tab = await openBackgroundTab({ url: "https://www.xiaohongshu.com/explore" });
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
    await closeBackgroundTab(tabId);
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
    const tab = await openBackgroundTab({ url: "https://www.douyin.com/" });
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
    await closeBackgroundTab(tabId);
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
    const tab = await openBackgroundTab({ url: "https://www.douyin.com/" });
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
    await closeBackgroundTab(tabId);
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
  const deadline = Date.now() + commentBudgetMs(args);
  const requestedSessionId = String(args.sessionId || "").trim();

  await commentSessionHydration;
  let session = requestedSessionId
    ? findCommentSession(requestedSessionId, { platform: "douyin", fingerprint: awemeId })
    : null;
  if (requestedSessionId && !session) {
    reply({
      ok: false,
      error: `douyin comment session ${requestedSessionId} expired or belongs to another post; `
        + "call again without a session id to restart collection",
    });
    return;
  }

  const pages = [];
  const seenIds = session ? session.seen : new Set();
  const progress = session ? session.progress : {
    sawRootPage: false,
    rootComplete: false,
    expectedCount: null,
    replyRoots: new Set(),
    completedReplyRoots: new Set(),
  };
  const loop = session ? session.loop : {
    scrolls: 0,
    stableRounds: 0,
    previousHeight: -1,
    previousCount: -1,
    scrollDirection: 1,
    sweepTurns: 0,
  };
  let tabId = session ? session.tabId : null;
  let debugTarget = null;
  let complete = false;
  let limitReached = false;
  let budgetExhausted = false;

  try {
    let pending;
    if (session) {
      pending = pendingDouyinComments.get(tabId);
      if (!pending) {
        pending = { responses: [], errors: [] };
        pendingDouyinComments.set(tabId, pending);
      }
      debugTarget = { tabId };
      await ensureDebuggerAttached(debugTarget);
    } else {
      const target = new URL(`https://www.douyin.com/${pageKind}/${awemeId}`);
      const approval = await requestUrlApproval(state, target.toString());
      if (!approval.allowed) {
        reply({ ok: false, error: `navigation blocked: ${approval.error || "URL policy"}` });
        return;
      }
      const createdWindow = await chrome.windows.create({
        url: "https://www.douyin.com/",
        focused: false,
        type: "normal",
      });
      const [tab] = createdWindow.tabs || [];
      tabId = tab?.id ?? null;
      if (tabId == null || createdWindow.id == null) {
        if (createdWindow.id != null) await chrome.windows.remove(createdWindow.id).catch(() => {});
        throw new Error("Chrome did not create an isolated Douyin comments window");
      }
      // Own the window through the session from here on, so every later failure closes it once.
      session = await createCommentSession({
        platform: "douyin",
        fingerprint: awemeId,
        tabId,
        windowId: createdWindow.id,
        seen: seenIds,
        progress,
        loop,
        release: (closed) => pendingDouyinComments.delete(closed.tabId),
      });
      await waitForTabComplete(tabId);
      pending = { responses: [], errors: [] };
      pendingDouyinComments.set(tabId, pending);
      await chrome.tabs.update(tabId, { url: target.toString() });
      await waitForTabComplete(tabId);
      await new Promise((resolve) => setTimeout(resolve, 1200));
      debugTarget = { tabId };
      await ensureDebuggerAttached(debugTarget);
    }

    while (loop.scrolls < maxScrolls) {
      if (Date.now() >= deadline) {
        budgetExhausted = true;
        break;
      }
      loop.scrolls += 1;
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
      if (ui.clicked === 0 && needsReplySweep && loop.sweepTurns < 4) {
        if (loop.scrollDirection > 0 && ui.atBottom) {
          loop.scrollDirection = -1;
          loop.sweepTurns += 1;
          directionChanged = true;
        } else if (loop.scrollDirection < 0 && ui.atTop) {
          loop.scrollDirection = 1;
          loop.sweepTurns += 1;
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
          wheelDelta * loop.scrollDirection,
        );
      }

      await new Promise((resolve) => setTimeout(resolve, 750));
      if (pending.errors.length) throw new Error(pending.errors.shift());
      drainDouyinCommentResponses(pending, awemeId, pages, seenIds, progress);

      const unchanged = loop.previousCount === seenIds.size
        && loop.previousHeight === ui.scrollHeight;
      const atBoundary = loop.scrollDirection > 0 ? ui.atBottom : ui.atTop;
      loop.stableRounds = unchanged && ui.clicked === 0 && atBoundary && !directionChanged
        ? loop.stableRounds + 1
        : 0;
      loop.previousCount = seenIds.size;
      loop.previousHeight = ui.scrollHeight;

      const expectedReached = progress.expectedCount != null
        && seenIds.size >= progress.expectedCount;
      const streamsComplete = progress.rootComplete && douyinReplyStreamsComplete(progress);
      if (seenIds.size >= maxComments && !expectedReached) {
        limitReached = true;
        break;
      }
      if (loop.stableRounds >= 2 && (expectedReached || streamsComplete)) {
        complete = true;
        break;
      }
      if (
        loop.stableRounds >= 10
        || (ui.endText && progress.rootComplete && loop.stableRounds >= 2)
      ) break;
    }

    drainDouyinCommentResponses(pending, awemeId, pages, seenIds, progress);
    if (
      progress.rootComplete
      && douyinReplyStreamsComplete(progress)
      && !limitReached
    ) complete = true;
    // Only a budget stop is resumable: every other exit means this post has nothing left to give.
    const suspended = budgetExhausted && !complete && !limitReached;
    if (suspended) await suspendCommentSession(session);
    else await closeCommentSession(session);
    reply({
      ok: true,
      data: {
        complete,
        limit_reached: limitReached,
        budget_exhausted: budgetExhausted,
        session_id: suspended ? session.id : null,
        collected_total: seenIds.size,
        expected_count: progress.expectedCount,
        scrolls: loop.scrolls,
        pages,
      },
    });
  } catch (error) {
    if (session) await closeCommentSession(session);
    reply({ ok: false, error: String(error?.message || error) });
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
chrome.alarms.create(SWEEP_ALARM_NAME, { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "browser-mcp-keepalive") connectAll();
  else if (alarm.name === SWEEP_ALARM_NAME) void sweepCommentSessions();
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
  forgetBackgroundTab(tabId);
  void forgetCommentSessionByTab(tabId);
  for (const [session, managedTabId] of interactiveTabs.entries()) {
    if (managedTabId !== tabId) continue;
    void deleteInteractionSession(session);
    lastInteractionScreenshots.delete(session);
    void closeInteractionDebugger(session);
  }
});
void connectAll();
