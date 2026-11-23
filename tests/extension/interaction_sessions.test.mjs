/** Verify capacity, ownership and lifecycle in the Chrome worker's session registry. */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../../extension/background.js", import.meta.url), "utf8");

const registrySource = [
  source.slice(source.indexOf("const interactiveTabs"), source.indexOf("/** Load server-generated pairing")),
  source.slice(source.indexOf("async function runInteractionTask"), source.indexOf("/** Keep same-session actions")),
  source.slice(source.indexOf("async function cleanupBridgeSessionsForPort"), source.indexOf("/** Release only")),
  source.slice(source.indexOf("function checkInteractionDeadline"), source.indexOf("/** Execute one visual action")),
  source.slice(source.indexOf("async function closeManagedInteractionTab"), source.indexOf("/** Execute the requested action")),
].join("\n");

const page = "https://example.com/";

/** Run the real worker registry against a fake browser with persistent session storage. */
function makeRegistry(browser = { tabs: new Map(), stored: {}, nextId: 0, windows: 0 }) {
  const context = runInNewContext(
    `${registrySource}
    ({ interactionTab, interactionHydration, interactionQueues, interactionDebuggers,
       closeManagedInteractionTab, cleanupBridgeSessionsForPort })`,
    {
      Date: { now: () => ++browser.nextId },
      console,
      closeInteractionDebugger: async () => {},
      chrome: {
        storage: { session: {
          get: async () => browser.stored,
          set: async (value) => { browser.stored = value; },
        } },
        tabs: {
          get: async (id) => {
            if (!browser.tabs.has(id)) throw new Error("tab closed");
            return browser.tabs.get(id);
          },
          query: async () => [...browser.tabs.values()].filter((tab) => tab.active),
          create: async ({ windowId }) => {
            const tab = { id: ++browser.nextId, windowId, url: page };
            browser.tabs.set(tab.id, tab);
            return tab;
          },
          move: async (id, { windowId }) => {
            const tab = { ...browser.tabs.get(id), windowId };
            browser.tabs.set(id, tab);
            return tab;
          },
          remove: async (id) => { browser.tabs.delete(id); },
        },
        windows: {
          create: async () => {
            const id = ++browser.windows;
            const tab = { id: ++browser.nextId, windowId: id, url: page };
            browser.tabs.set(tab.id, tab);
            return { id, tabs: [tab] };
          },
        },
      },
    },
  );
  return { ...context, browser };
}

/** Open or refresh one session as a snapshot would. */
function snapshot(registry, name, { port = 7, url = page, keepOpen } = {}) {
  return registry.interactionTab({ port }, name, "snapshot", url, keepOpen);
}

test("concurrent opens respect capacity and preserve pinned, busy and dialog sessions", async () => {
  const registry = makeRegistry();
  const [pinned, busy, dialog, oldest, recent] = await Promise.all([
    snapshot(registry, "pinned", { keepOpen: true }),
    snapshot(registry, "busy"),
    snapshot(registry, "dialog"),
    snapshot(registry, "oldest"),
    snapshot(registry, "recent"),
  ]);
  assert.equal(registry.browser.windows, 1);
  registry.interactionQueues.set("7:busy", Promise.resolve());
  registry.interactionDebuggers.set("7:dialog", { dialog: {} });
  const opened = await snapshot(registry, "new");
  assert.equal(opened.windowId, pinned.windowId);
  assert.deepEqual([...registry.browser.tabs.keys()], [pinned.id, busy.id, dialog.id, recent.id, opened.id]);
  assert.equal(registry.browser.tabs.has(oldest.id), false);
  await assert.rejects(registry.interactionTab({ port: 7 }, "oldest", "scroll"), /not open/);
  const replacement = await snapshot(registry, "oldest");
  assert.notEqual(replacement.id, oldest.id);
});

test("pin changes survive a worker restart and a full retained window stays intact", async () => {
  const registry = makeRegistry();
  const names = ["a", "b", "c", "d", "e"];
  for (const name of names) await snapshot(registry, name, { keepOpen: true });
  const restarted = makeRegistry(registry.browser);
  await assert.rejects(snapshot(restarted, "f"), /kept open or busy; sessions in this connection: a, b, c, d, e/);
  const a = await restarted.interactionTab({ port: 7 }, "a", "snapshot", undefined, false);
  const f = await snapshot(restarted, "f");
  assert.equal(a.windowId, f.windowId);
  assert.equal(restarted.browser.tabs.has(a.id), false);
  assert.equal(restarted.browser.tabs.size, 5);
});

test("explicit close never removes a borrowed user tab and cleanup stays within its port", async () => {
  const registry = makeRegistry();
  const user = { id: 1000, active: true, windowId: 50, url: page };
  registry.browser.tabs.set(user.id, user);
  await registry.interactionTab({ port: 7 }, undefined, "snapshot");
  assert.equal(await registry.closeManagedInteractionTab("7:default", { requireOwned: true }), false);
  const mine = await snapshot(registry, "mine");
  const other = await snapshot(registry, "other", { port: 8 });
  assert.equal(await registry.closeManagedInteractionTab("7:mine", { requireOwned: true }), true);
  assert.equal(await registry.closeManagedInteractionTab("7:mine", { requireOwned: true }), false);
  assert.equal(registry.browser.tabs.has(mine.id), false);
  await snapshot(registry, "new");
  await registry.cleanupBridgeSessionsForPort(7);
  assert.deepEqual([...registry.browser.tabs.keys()], [user.id, other.id]);
});


test("different ports open in one window and share the capacity without exposing session names", async () => {
  const registry = makeRegistry();
  const [first, second, ...rest] = await Promise.all([
    snapshot(registry, "oldest", { port: 8 }),
    snapshot(registry, "same-name", { port: 7, keepOpen: true }),
    ...["c", "d", "e"].map((name) => snapshot(registry, name, { port: 9, keepOpen: true })),
  ]);
  assert.equal(registry.browser.windows, 1);
  assert.notEqual(first.id, second.id);
  assert.ok(rest.every((tab) => tab.windowId === first.windowId));
  const added = await snapshot(registry, "same-name", { port: 10, keepOpen: true });
  assert.equal(added.windowId, first.windowId);
  assert.equal(registry.browser.tabs.has(first.id), false);
  assert.equal(registry.browser.tabs.has(second.id), true);
  await assert.rejects(snapshot(registry, "overflow"), (error) => {
    assert.match(error.message, /kept open or busy/);
    assert.match(error.message, /sessions in this connection: same-name$/);
    return true;
  });
  await registry.cleanupBridgeSessionsForPort(7);
  assert.equal(registry.browser.tabs.has(second.id), false);
  assert.equal(registry.browser.tabs.has(added.id), true);
});

test("startup consolidates existing managed tabs without moving user tabs", async () => {
  const entries = {
    "7:default": { tabId: 21, windowId: 1, keepOpen: true },
    "8:default": { tabId: 22, windowId: 2, keepOpen: true },
    "9:default": { tabId: 23, windowId: 3, keepOpen: true },
  };
  const browser = {
    tabs: new Map([
      [21, { id: 21, windowId: 1, url: page }],
      [22, { id: 22, windowId: 2, url: page }],
      [23, { id: 23, windowId: 9, url: page }],
      [24, { id: 24, windowId: 2, url: page }],
    ]),
    stored: { browserMcpInteractionState: entries },
    nextId: 25,
    windows: 9,
  };
  const registry = makeRegistry(browser);
  await registry.interactionHydration;
  assert.equal(browser.tabs.get(22).windowId, 1);
  assert.equal(browser.tabs.get(23).windowId, 9);
  assert.equal(browser.tabs.get(24).windowId, 2);
  assert.equal(browser.stored.browserMcpInteractionState["8:default"].windowId, 1);
});
