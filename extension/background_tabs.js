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
// behind, instead of leaking read tabs into the user's tab strip. They are tracked per bridge
// port: several paired processes can drive one Chrome profile at once, and one of them shutting
// down must not kill another's in-flight read — that surfaces as "No tab with id" on a read that
// was doing nothing wrong.

const managedTabs = new Map();

/** Create one inactive read tab in the user's current window, owned by one bridge port. */
export async function openBackgroundTab(createProperties, port) {
  const tab = await chrome.tabs.create({ ...createProperties, active: false });
  if (tab?.id != null) managedTabs.set(tab.id, port ?? null);
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

/** Close the read tabs one bridge port opened, leaving every other client's reads alone. */
export async function closeBackgroundTabsForPort(port) {
  const tabIds = [];
  for (const [tabId, owner] of managedTabs) {
    if (owner !== port) continue;
    tabIds.push(tabId);
  }
  for (const tabId of tabIds) managedTabs.delete(tabId);
  await Promise.all(tabIds.map((tabId) => chrome.tabs.remove(tabId).catch(() => {})));
}
