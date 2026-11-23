/** Exercise visual action focus ordering with the Chrome window APIs mocked. */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../../extension/background.js", import.meta.url), "utf8");
const focusPath = source.slice(
  source.indexOf("const INTERACTION_ACTIONS ="),
  source.indexOf("/** Resolve an element reference or screenshot coordinate"),
);

/** Run the worker's real dispatcher and focus logic with unrelated browser work stubbed. */
async function dispatch(action, { optIn, args = {}, previous, managed = true, active = true } = {}) {
  const calls = [];
  const tab = { id: 11, windowId: 21, url: "https://example.com/", active };
  const captureState = async () => ({ state: { viewport: {}, elements: [] } });
  const run = runInNewContext(`${focusPath}\ndispatchBrowserInteraction`, {
    chrome: {
      tabs: {
        get: async () => tab,
        update: async (id) => { calls.push(`activate:${id}`); },
      },
      windows: {
        getLastFocused: async () => previous ?? { id: 21, focused: false },
        update: async (id) => { calls.push(`focus:${id}`); },
      },
    },
    setTimeout: (resolve, ms) => { calls.push(`wait:${ms}`); resolve(); },
    interactionWindows: new Map(managed ? [["17880:default", 21]] : []),
    interactionDebuggers: new Map(),
    lastInteractionScreenshots: new Map(),
    interactionTab: async () => tab,
    requestUrlApproval: async () => ({ allowed: true }),
    removeForeignExtensionFrames: async () => {},
    interactionClickPoint: () => ({ x: 10, y: 10 }),
    interactionNavigationTarget: async () => null,
    interactionDebuggerSession: async () => ({ guardTasks: new Set() }),
    executeInteractionAction: async (_tabId, kind) => { calls.push(kind); },
    boundedWait: () => 0,
    settleInteractionTabOrDialog: async () => {},
    captureInteractionScreenshot: async () => ({}),
    captureInteractionState: captureState,
    captureVisualClickState: captureState,
    closeInteractionDebugger: async () => {},
    sendJson: (_state, reply) => assert.equal(reply.ok, true, reply.error),
  });
  await run({ port: 17880 }, { action, args, focus_before_input: optIn });
  return calls;
}

test("opt-in refocuses the last Chrome window before click and wheel input", async () => {
  for (const action of ["click", "scroll"]) {
    assert.deepEqual(await dispatch(action, { optIn: true }), ["focus:21", "wait:150", action]);
    assert.deepEqual(await dispatch(action), [action]);
  }
});

test("opt-in activates the managed tab and restores the previously focused Chrome window", async () => {
  const options = { optIn: true, previous: { id: 42, focused: true }, active: false };
  assert.deepEqual(await dispatch("scroll", options), [
    "activate:11", "focus:21", "wait:150", "scroll", "focus:42",
  ]);
  assert.deepEqual(await dispatch("scroll", { ...options, previous: { id: 42, focused: false } }), [
    "activate:11", "focus:21", "wait:150", "scroll",
  ]);
});

test("opt-in leaves snapshots, element scrolling and user-owned windows in the background", async () => {
  assert.deepEqual(await dispatch("snapshot", { optIn: true }), ["snapshot"]);
  assert.deepEqual(await dispatch("scroll", { optIn: true, args: { element_id: "e1" } }), ["scroll"]);
  assert.deepEqual(await dispatch("click", { optIn: true, managed: false }), ["click"]);
});
