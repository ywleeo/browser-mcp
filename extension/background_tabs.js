// Read-only adapters each need a real Chrome tab, opened inactive in the user's current window.
//
// These tabs used to live in a separate minimized window, on the theory that it would keep
// Chrome's debugging infobar off the user's work. That theory was wrong: the infobar is drawn by
// Chromium's GlobalConfirmInfoBar, which paints it into every window of the profile for as long
// as any extension holds a chrome.debugger attachment — where the debugged tab lives is
// irrelevant. What actually removed the banner from reads was not attaching in the first place
// (see dispatchBrowserFetch), so the extra window bought nothing and cost the user one more
// window to step over.
//
// Tabs stay tracked so a bridge disconnect can close whatever an evicted service worker left
// behind, instead of leaking read tabs into the user's tab strip.

const managedTabs = new Set();

/** Create one inactive read tab in the user's current window. */
export async function openBackgroundTab(createProperties) {
  const tab = await chrome.tabs.create({ ...createProperties, active: false });
  if (tab?.id != null) managedTabs.add(tab.id);
  return tab;
}

/** Close one read tab once its adapter is done with it. */
export async function closeBackgroundTab(tabId) {
  if (tabId == null) return;
  managedTabs.delete(tabId);
  await chrome.tabs.remove(tabId).catch(() => {});
}

/** Forget a tab Chrome or the user closed underneath us. */
export function forgetBackgroundTab(tabId) {
  managedTabs.delete(tabId);
}

/** Close every read tab still open, used when the paired process goes away. */
export async function closeAllBackgroundTabs() {
  const tabIds = [...managedTabs];
  managedTabs.clear();
  await Promise.all(tabIds.map((tabId) => chrome.tabs.remove(tabId).catch(() => {})));
}
