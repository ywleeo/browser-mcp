// Resumable comment-collection sessions shared by every platform collector.
//
// Collecting a busy comment thread is a wall-clock-bound scroll loop: hundreds of comments plus
// their expanded replies take minutes, which is longer than an MCP client will wait for one tool
// call. Running past the client's own timeout used to discard every comment already collected.
//
// So collection is budgeted instead. When a run's time budget expires the collector suspends its
// session rather than failing: the background window, its scroll position, the debugger
// attachment and the deduplication state all stay alive, the comments gathered so far are
// returned, and the caller receives a session id that resumes exactly where the run stopped.
//
// Suspended sessions are a resource, so they expire. An idle session is closed by the sweep alarm
// after SESSION_IDLE_TTL_MS, and mirrored into chrome.storage.session so that a service-worker
// restart can either recover the session or close the window Chrome is still holding open.

const SESSION_STATE_KEY = "browserMcpCommentSessions";
const SESSION_IDLE_TTL_MS = 5 * 60 * 1000;
const SWEEP_ALARM_NAME = "browser-mcp-comment-sweep";

const commentSessions = new Map();

/** Encode collector state, preserving the Sets platform collectors track ids in. */
function encodeState(value) {
  if (value instanceof Set) return { __set: [...value] };
  if (Array.isArray(value)) return value.map(encodeState);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, entry]) => [key, encodeState(entry)]),
    );
  }
  return value;
}

/** Restore state encoded by encodeState, rebuilding Sets from their marker objects. */
function decodeState(value) {
  if (Array.isArray(value)) return value.map(decodeState);
  if (value && typeof value === "object") {
    if (Array.isArray(value.__set)) return new Set(value.__set);
    return Object.fromEntries(
      Object.entries(value).map(([key, entry]) => [key, decodeState(entry)]),
    );
  }
  return value;
}

/** Mirror every live session to session storage so a worker restart can recover or clean up. */
async function persistCommentSessions() {
  const state = {};
  for (const [id, session] of commentSessions.entries()) {
    state[id] = {
      platform: session.platform,
      fingerprint: session.fingerprint,
      tabId: session.tabId,
      windowId: session.windowId,
      seen: [...session.seen],
      progress: encodeState(session.progress),
      loop: encodeState(session.loop),
      expiresAt: session.expiresAt,
    };
  }
  try {
    await chrome.storage.session.set({ [SESSION_STATE_KEY]: state });
  } catch (error) {
    console.warn("[browser-mcp-extension] failed to persist comment sessions", error);
  }
}

/** Recover sessions that outlived the service worker, dropping ones whose tab Chrome closed. */
async function hydrateCommentSessions() {
  try {
    const stored = (await chrome.storage.session.get(SESSION_STATE_KEY))[SESSION_STATE_KEY];
    if (!stored) return;
    let changed = false;
    for (const [id, entry] of Object.entries(stored)) {
      if (entry?.tabId == null) continue;
      try {
        await chrome.tabs.get(entry.tabId);
      } catch {
        changed = true;
        continue;
      }
      commentSessions.set(id, {
        id,
        platform: String(entry.platform || ""),
        fingerprint: String(entry.fingerprint || ""),
        tabId: entry.tabId,
        windowId: entry.windowId ?? null,
        seen: new Set(Array.isArray(entry.seen) ? entry.seen : []),
        progress: decodeState(entry.progress) || {},
        loop: decodeState(entry.loop) || {},
        expiresAt: Number(entry.expiresAt) || 0,
        release: null,
      });
    }
    if (changed) await persistCommentSessions();
  } catch (error) {
    console.warn("[browser-mcp-extension] comment session hydration failed", error);
  }
}

/** Resolved once sessions that survived a worker restart are back in memory. */
export const commentSessionHydration = hydrateCommentSessions();

/**
 * Return the live session for one resume request, or null when it can no longer be resumed.
 *
 * The fingerprint check keeps a session bound to the exact post it was opened for, so a stale
 * id from another note can never silently return that note's comments.
 */
export function findCommentSession(sessionId, { platform, fingerprint }) {
  const session = commentSessions.get(sessionId);
  if (!session) return null;
  if (session.platform !== platform || session.fingerprint !== fingerprint) return null;
  return session;
}

/** Register one freshly opened collection window as a resumable session. */
export async function createCommentSession({
  platform,
  fingerprint,
  tabId,
  windowId,
  seen,
  progress,
  loop,
  release,
}) {
  // seen/progress/loop stay shared by reference with the running collector, so a suspend
  // persists exactly the state the loop was mutating.
  const session = {
    id: crypto.randomUUID(),
    platform,
    fingerprint,
    tabId,
    windowId: windowId ?? null,
    seen: seen || new Set(),
    progress,
    loop,
    expiresAt: Date.now() + SESSION_IDLE_TTL_MS,
    release: release || null,
  };
  commentSessions.set(session.id, session);
  return session;
}

/** Keep one session's window and collector state alive for the next resuming call. */
export async function suspendCommentSession(session) {
  session.expiresAt = Date.now() + SESSION_IDLE_TTL_MS;
  commentSessions.set(session.id, session);
  await persistCommentSessions();
}

/** Release one session's debugger, window and platform buffers for good. */
export async function closeCommentSession(session) {
  commentSessions.delete(session.id);
  try {
    session.release?.(session);
  } catch (error) {
    console.warn("[browser-mcp-extension] comment session release failed", error);
  }
  if (session.tabId != null) {
    await chrome.debugger.detach({ tabId: session.tabId }).catch(() => {});
  }
  if (session.windowId != null) await chrome.windows.remove(session.windowId).catch(() => {});
  else if (session.tabId != null) await chrome.tabs.remove(session.tabId).catch(() => {});
  await persistCommentSessions();
}

/** Close every session whose idle deadline passed without a resuming call. */
export async function sweepCommentSessions() {
  const now = Date.now();
  for (const session of [...commentSessions.values()]) {
    if (session.expiresAt > now) continue;
    await closeCommentSession(session);
  }
}

/** Drop every session, used when the paired Python process announces shutdown. */
export async function closeAllCommentSessions() {
  for (const session of [...commentSessions.values()]) await closeCommentSession(session);
}

/** Forget one session whose tab Chrome closed underneath us, without touching Chrome again. */
export async function forgetCommentSessionByTab(tabId) {
  for (const session of commentSessions.values()) {
    if (session.tabId !== tabId) continue;
    commentSessions.delete(session.id);
    try {
      session.release?.(session);
    } catch {
      // The platform buffer is already gone; nothing else to release.
    }
    await persistCommentSessions();
  }
}

/** Attach the debugger unless this tab already carries our attachment from an earlier call. */
export async function ensureDebuggerAttached(target) {
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (error) {
    if (!/already attached/i.test(String(error?.message || error))) throw error;
  }
}

export { SESSION_IDLE_TTL_MS, SWEEP_ALARM_NAME };
