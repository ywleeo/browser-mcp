/** Exercise visual action focus ordering with the Chrome window APIs mocked. */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../../extension/background.js", import.meta.url), "utf8");
const queuePath = source.slice(
  source.indexOf("async function runInteractionTask"),
  source.indexOf("/** Keep same-session actions ordered"),
);
const focusPath = source.slice(
  source.indexOf("const INTERACTION_ACTIONS ="),
  source.indexOf("/** Resolve an element reference or screenshot coordinate"),
);

/** Run the worker's real dispatcher and focus logic with unrelated browser work stubbed. */
async function dispatch(action, {
  optIn, args = {}, previous, managed = true, active = true, stalled = false, deadline,
} = {}) {
  const calls = [];
  let finishCommand;
  const tab = { id: 11, windowId: 21, url: "https://example.com/", active };
  const captureState = async () => ({ state: { viewport: {}, elements: [] } });
  const run = runInNewContext(`${queuePath}\n${focusPath}\ndispatchBrowserInteraction`, {
    chrome: {
      debugger: { detach: async () => { calls.push("detach"); finishCommand(); } },
      tabs: {
        get: async () => tab,
        update: async (id) => { calls.push(`activate:${id}`); },
      },
      windows: {
        getLastFocused: async () => previous ?? { id: 21, focused: false },
        update: async (id) => { calls.push(`focus:${id}`); },
      },
    },
    INTERACTION_INPUT_TIMEOUT_MS: 20_000,
    setTimeout: (resolve, ms) => {
      if (ms === 20_000) {
        if (stalled) queueMicrotask(resolve);
        return 1;
      }
      calls.push(`wait:${ms}`);
      resolve();
    },
    clearTimeout: () => {},
    interactionInputQueue: new Map(),
    interactionWindows: new Map(managed ? [["17880:default", 21]] : []),
    interactionDebuggers: new Map(),
    lastInteractionScreenshots: new Map(),
    interactionTab: async () => tab,
    requestUrlApproval: async () => ({ allowed: true }),
    removeForeignExtensionFrames: async () => {},
    interactionClickPoint: () => ({ x: 10, y: 10 }),
    interactionNavigationTarget: async () => null,
    interactionDebuggerSession: async () => ({ target: {}, guardTasks: new Set() }),
    executeInteractionAction: async (_tabId, kind) => {
      calls.push(kind);
      if (stalled) await new Promise((resolve) => { finishCommand = resolve; });
    },
    boundedWait: () => 0,
    settleInteractionTabOrDialog: async () => {},
    captureInteractionScreenshot: async () => ({}),
    captureInteractionState: captureState,
    captureVisualClickState: captureState,
    closeInteractionDebugger: async () => {},
    sendJson: (_state, reply) => {
      if (stalled || deadline != null) {
        assert.equal(reply.ok, false);
        assert.match(reply.error, stalled ? /Chrome input timed out/ : /interaction expired/);
      } else {
        assert.equal(reply.ok, true, reply.error);
      }
    },
  });
  await run({ port: 17880 }, { action, args, focus_before_input: optIn, deadline_ms: deadline });
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

test("visual input activates an inactive tab without foreground opt-in", async () => {
  const options = { active: false };
  for (const action of ["click", "scroll", "press", "type"]) {
    assert.deepEqual(await dispatch(action, options), ["activate:11", action]);
  }
});

test("a stalled Chrome command is cancelled once and reported without replay", async () => {
  assert.deepEqual(await dispatch("scroll", { stalled: true }), ["scroll", "detach"]);
});

test("expired input does not activate a tab or dispatch a command", async () => {
  assert.deepEqual(await dispatch("click", { deadline: 0, active: false }), []);
});
