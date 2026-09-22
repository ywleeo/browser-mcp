/** Tests for the CDP upload paths, with chrome.debugger mocked at the command boundary. */

import assert from "node:assert/strict";
import test from "node:test";

import { executeUpload } from "../../extension/interaction_upload.js";

const TARGET = { tabId: 1 };
const FRAMES = [{ id: "frame-1", url: "https://example.com/" }];
const FILES = ["/tmp/poster.png"];

/** Install a chrome.debugger double that records commands and can emit protocol events. */
function mockChrome(handlers) {
  const calls = [];
  const listeners = new Set();
  const emit = (method, params, source = TARGET) => {
    for (const listener of [...listeners]) listener(source, method, params);
  };
  globalThis.chrome = {
    debugger: {
      async sendCommand(_target, method, params) {
        calls.push({ method, params });
        const handler = handlers[method];
        const value = typeof handler === "function" ? await handler(params, emit) : handler;
        return value ?? {};
      },
      onEvent: {
        addListener: (fn) => listeners.add(fn),
        removeListener: (fn) => listeners.delete(fn),
      },
    },
  };
  return { calls, emit, methods: () => calls.map((call) => call.method) };
}

/** Build the evaluate/getProperties/callFunctionOn replies for a list of file inputs. */
function staticInputs(descriptions) {
  return {
    "Page.createIsolatedWorld": { executionContextId: 7 },
    "Runtime.evaluate": { result: { objectId: "array-1" } },
    "Runtime.getProperties": {
      result: descriptions.map((_item, index) => ({
        name: String(index),
        value: { objectId: `input-${index}` },
      })),
    },
    "Runtime.callFunctionOn": ({ objectId }) => ({
      result: { value: descriptions[Number(objectId.split("-")[1])] },
    }),
    "DOM.setFileInputFiles": {},
  };
}

test("a single hidden input is filled directly, without opening a chooser", async () => {
  const chrome = mockChrome(
    staticInputs([{ accept: "image/*", multiple: false, hidden: true, label: "本地上传" }]),
  );

  await executeUpload(TARGET, { paths: FILES }, FRAMES);

  const write = chrome.calls.find((call) => call.method === "DOM.setFileInputFiles");
  assert.deepEqual(write.params, { objectId: "input-0", files: FILES });
  assert.ok(!chrome.methods().includes("Page.setInterceptFileChooserDialog"));
});

test("several inputs are reported with indexes instead of guessed between", async () => {
  mockChrome(
    staticInputs([
      { accept: "image/*", multiple: false, hidden: true, label: "背景图" },
      { accept: "video/*", multiple: true, hidden: false, label: "视频" },
    ]),
  );

  await assert.rejects(
    executeUpload(TARGET, { paths: FILES }, FRAMES),
    (error) => {
      assert.match(error.message, /2 file inputs found/);
      assert.match(error.message, /index 0: label "背景图", accept image\/\*/);
      assert.match(error.message, /index 1: .*accept video\/\*.*multiple, visible/);
      return true;
    },
  );
});

test("an explicit index selects among several inputs", async () => {
  const chrome = mockChrome(
    staticInputs([
      { multiple: false, hidden: true, label: "背景图" },
      { multiple: false, hidden: true, label: "视频" },
    ]),
  );

  await executeUpload(TARGET, { paths: FILES, index: 1 }, FRAMES);

  const write = chrome.calls.find((call) => call.method === "DOM.setFileInputFiles");
  assert.equal(write.params.objectId, "input-1");
});

test("a control that builds its input on click is driven with the chooser intercepted", async () => {
  const chrome = mockChrome({
    "Page.createIsolatedWorld": { executionContextId: 7 },
    // The scoped search finds nothing; the anchor lookup then resolves the button itself.
    "Runtime.evaluate": ({ expression }) =>
      (expression.includes("input[type='file']")
        ? { result: {} }
        : { result: { objectId: "array-anchor" } }),
    "Runtime.getProperties": { result: [{ name: "0", value: { objectId: "button-1" } }] },
    "DOM.scrollIntoViewIfNeeded": {},
    "DOM.getBoxModel": { model: { border: [10, 10, 110, 10, 110, 40, 10, 40] } },
    "Page.setInterceptFileChooserDialog": {},
    "Input.dispatchMouseEvent": (params, emit) => {
      if (params.type === "mouseReleased") {
        emit("Page.fileChooserOpened", { backendNodeId: 42, mode: "selectSingle" });
      }
      return {};
    },
    "DOM.setFileInputFiles": {},
  });

  await executeUpload(TARGET, { paths: FILES, element_id: "abc-e12" }, FRAMES);

  const write = chrome.calls.find((call) => call.method === "DOM.setFileInputFiles");
  assert.deepEqual(write.params, { backendNodeId: 42, files: FILES });

  const clicks = chrome.calls.filter((call) => call.method === "Input.dispatchMouseEvent");
  assert.deepEqual(clicks.map((call) => call.params.type), [
    "mouseMoved",
    "mousePressed",
    "mouseReleased",
  ]);
  assert.equal(clicks[1].params.x, 60, "clicks the centre of the control's box");
  assert.equal(clicks[1].params.y, 25);

  const interception = chrome.calls.filter(
    (call) => call.method === "Page.setInterceptFileChooserDialog",
  );
  assert.deepEqual(interception.map((call) => call.params.enabled), [true, false]);
});

test("interception is lifted even when the upload itself fails", async () => {
  const chrome = mockChrome({
    "Page.createIsolatedWorld": { executionContextId: 7 },
    "Runtime.evaluate": ({ expression }) =>
      (expression.includes("input[type='file']")
        ? { result: {} }
        : { result: { objectId: "array-anchor" } }),
    "Runtime.getProperties": { result: [{ name: "0", value: { objectId: "button-1" } }] },
    "DOM.scrollIntoViewIfNeeded": {},
    "DOM.getBoxModel": { model: { border: [0, 0, 20, 0, 20, 20, 0, 20] } },
    "Page.setInterceptFileChooserDialog": {},
    "Input.dispatchMouseEvent": (params, emit) => {
      if (params.type === "mouseReleased") {
        emit("Page.fileChooserOpened", { backendNodeId: 42, mode: "selectSingle" });
      }
      return {};
    },
    "DOM.setFileInputFiles": () => {
      throw new Error("node is detached");
    },
  });

  await assert.rejects(executeUpload(TARGET, { paths: FILES, element_id: "e1" }, FRAMES));

  const interception = chrome.calls.filter(
    (call) => call.method === "Page.setInterceptFileChooserDialog",
  );
  assert.deepEqual(
    interception.map((call) => call.params.enabled),
    [true, false],
    "a leaked interception would silently break the user's own upload buttons",
  );
});

test("a chooser with no backing input is reported as File System Access", async () => {
  mockChrome({
    "Page.createIsolatedWorld": { executionContextId: 7 },
    "Runtime.evaluate": ({ expression }) =>
      (expression.includes("input[type='file']")
        ? { result: {} }
        : { result: { objectId: "array-anchor" } }),
    "Runtime.getProperties": { result: [{ name: "0", value: { objectId: "button-1" } }] },
    "DOM.scrollIntoViewIfNeeded": {},
    "DOM.getBoxModel": { model: { border: [0, 0, 20, 0, 20, 20, 0, 20] } },
    "Page.setInterceptFileChooserDialog": {},
    "Input.dispatchMouseEvent": (params, emit) => {
      if (params.type === "mouseReleased") emit("Page.fileChooserOpened", { mode: "selectSingle" });
      return {};
    },
  });

  await assert.rejects(
    executeUpload(TARGET, { paths: FILES, element_id: "e1" }, FRAMES),
    /File System Access API/,
  );
});

test("multiple files are refused by a single-file input before any write", async () => {
  const chrome = mockChrome(
    staticInputs([{ multiple: false, hidden: true, label: "背景图" }]),
  );

  await assert.rejects(
    executeUpload(TARGET, { paths: ["/tmp/a.png", "/tmp/b.png"] }, FRAMES),
    /accepts one file but 2 were given/,
  );
  assert.ok(!chrome.methods().includes("DOM.setFileInputFiles"));
});

test("a page with no file input explains the intercepted alternative", async () => {
  mockChrome({
    "Page.createIsolatedWorld": { executionContextId: 7 },
    "Runtime.evaluate": { result: {} },
  });

  await assert.rejects(executeUpload(TARGET, { paths: FILES }, FRAMES), (error) => {
    assert.match(error.message, /no file input found/);
    assert.match(error.message, /Never click an upload control directly/);
    return true;
  });
});
