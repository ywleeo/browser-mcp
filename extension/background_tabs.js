// One shared, minimized window hosting every read-only tab the bridge opens.
//
// Read adapters need a real tab, and browser.fetch additionally attaches chrome.debugger so it
// can approve every navigation hop before it commits. Chrome shows its "is debugging this
// browser" infobar in whichever window the attached tab lives in, and these tabs used to be
// created in the user's current window — so the banner, plus a stream of background tabs, landed
// on top of whatever the user was doing. Chrome does not let an extension suppress that banner,
// and the navigation guard is not optional, so the tabs move instead: a separate minimized
// window keeps both out of the way without weakening anything.
//
// The window is created on first use, reused across calls, closed once it has been idle, and
// mirrored into chrome.storage.session so a service-worker restart reclaims it instead of
// leaking one minimized window per eviction.

const WINDOW_STATE_KEY = "browserMcpBackgroundWindow";
const IDLE_CLOSE_MS = 120000;

let backgroundWindowId = null;
let placeholderTabId = null;
let lastReleasedAt = 0;
const managedTabs = new Set();

/** Mirror the shared window's identity so a worker restart can reuse or close it. */
async function persistBackgroundWindow() {
  try {
    if (backgroundWindowId == null) await chrome.storage.session.remove(WINDOW_STATE_KEY);
    else {
      await chrome.storage.session.set({
        [WINDOW_STATE_KEY]: { windowId: backgroundWindowId, placeholderTabId },
      });
    }
  } catch (error) {
    console.warn("[browser-mcp-extension] failed to persist background window", error);
  }
}

/** Recover the shared window that outlived the service worker, if Chrome still has it. */
async function hydrateBackgroundWindow() {
  try {
    const stored = (await chrome.storage.session.get(WINDOW_STATE_KEY))[WINDOW_STATE_KEY];
    if (stored?.windowId == null) return;
    await chrome.windows.get(stored.windowId);
    backgroundWindowId = stored.windowId;
    placeholderTabId = stored.placeholderTabId ?? null;
    lastReleasedAt = Date.now();
  } catch {
    backgroundWindowId = null;
    placeholderTabId = null;
    await persistBackgroundWindow();
  }
}

/** Resolved once a shared window that survived a worker restart is usable again. */
export const backgroundWindowHydration = hydrateBackgroundWindow();

/** Return the shared minimized window, creating it when Chrome no longer has one. */
async function ensureBackgroundWindow() {
  if (backgroundWindowId != null) {
    try {
      await chrome.windows.get(backgroundWindowId);
      return backgroundWindowId;
    } catch {
      backgroundWindowId = null;
      placeholderTabId = null;
    }
  }
  const created = await chrome.windows.create({
    url: "about:blank",
    focused: false,
    type: "normal",
  });
  if (created?.id == null) throw new Error("Chrome did not create the background read window");
  backgroundWindowId = created.id;
  placeholderTabId = created.tabs?.[0]?.id ?? null;
  // Minimize separately: Chrome rejects creating a window that is minimized and unfocused.
  await chrome.windows.update(backgroundWindowId, { state: "minimized" }).catch(() => {});
  await persistBackgroundWindow();
  return backgroundWindowId;
}

/**
 * Create one inactive read tab inside the shared window.
 *
 * A read must never fail just because the shared window could not be created, so this falls back
 * to the caller's current window — the tab still works, only the isolation is lost.
 */
export async function openBackgroundTab(createProperties) {
  await backgroundWindowHydration;
  let tab = null;
  try {
    const windowId = await ensureBackgroundWindow();
    tab = await chrome.tabs.create({ ...createProperties, windowId, active: false });
  } catch (error) {
    console.warn("[browser-mcp-extension] background window unavailable", error);
    tab = await chrome.tabs.create({ ...createProperties, active: false });
  }
  if (tab?.id != null) managedTabs.add(tab.id);
  return tab;
}

/** Close one read tab and start the shared window's idle countdown. */
export async function closeBackgroundTab(tabId) {
  if (tabId == null) return;
  managedTabs.delete(tabId);
  lastReleasedAt = Date.now();
  await chrome.tabs.remove(tabId).catch(() => {});
}

/** Close the shared window once no read has needed it for a while. */
export async function sweepBackgroundWindow() {
  if (backgroundWindowId == null || managedTabs.size > 0) return;
  if (Date.now() - lastReleasedAt < IDLE_CLOSE_MS) return;
  await closeBackgroundWindow();
}

/** Release the shared window, used on idle timeout and when the paired process shuts down. */
export async function closeBackgroundWindow() {
  const windowId = backgroundWindowId;
  backgroundWindowId = null;
  placeholderTabId = null;
  managedTabs.clear();
  await persistBackgroundWindow();
  if (windowId != null) await chrome.windows.remove(windowId).catch(() => {});
}

/** Forget a tab Chrome closed underneath us, including the window's own placeholder. */
export function forgetBackgroundTab(tabId) {
  if (managedTabs.delete(tabId)) lastReleasedAt = Date.now();
  if (tabId === placeholderTabId) placeholderTabId = null;
}

/** Forget the shared window after Chrome or the user closed it. */
export async function forgetBackgroundWindow(windowId) {
  if (windowId !== backgroundWindowId) return;
  backgroundWindowId = null;
  placeholderTabId = null;
  managedTabs.clear();
  await persistBackgroundWindow();
}
