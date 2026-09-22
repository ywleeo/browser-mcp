/**
 * File-input uploads driven entirely through CDP.
 *
 * Chrome reads the paths itself via DOM.setFileInputFiles, so the OS picker never opens and
 * never has to be automated. Two resolution paths cover how real sites build uploads:
 *
 * 1. Static: the page already holds an <input type="file">, almost always hidden behind a styled
 *    button. Those are resolved without the visibility filter browser_snapshot applies, because
 *    a hidden input never earns a snapshot reference.
 * 2. Intercepted: the control creates its input only on click, so nothing exists to find up
 *    front. Page.setInterceptFileChooserDialog keeps the OS dialog shut and makes Chrome report
 *    the real input as a backendNodeId instead -- even one that was never inserted in the DOM.
 */

const FILE_INPUT_SELECTOR = "input[type='file']";
const FILE_CHOOSER_TIMEOUT_MS = 5000;
const SHADOW_ROOT_WALK = `
  const roots = [document];
  for (let index = 0; index < roots.length; index += 1) {
    for (const element of roots[index].querySelectorAll("*")) {
      if (element.shadowRoot) roots.push(element.shadowRoot);
    }
  }`;

/** Collect every file input in one frame, hidden ones included, as remote object handles. */
const ALL_INPUTS_EXPRESSION = `(() => {${SHADOW_ROOT_WALK}
  return roots.flatMap((root) => [...root.querySelectorAll("${FILE_INPUT_SELECTOR}")]);
})()`;

/** Collect the file inputs a visible upload control owns, widening one step at a time. */
function scopedInputsExpression(elementId) {
  return `(() => {${SHADOW_ROOT_WALK}
  const selector = '[data-browser-mcp-ref="' + CSS.escape(${JSON.stringify(elementId)}) + '"]';
  let anchor = null;
  for (const root of roots) {
    anchor = root.querySelector(selector);
    if (anchor) break;
  }
  if (!anchor) return [];
  if (anchor.matches("${FILE_INPUT_SELECTOR}")) return [anchor];
  const inside = [...anchor.querySelectorAll("${FILE_INPUT_SELECTOR}")];
  if (inside.length) return inside;
  if (anchor.htmlFor) {
    const labelled = anchor.getRootNode().getElementById?.(anchor.htmlFor);
    if (labelled?.matches?.("${FILE_INPUT_SELECTOR}")) return [labelled];
  }
  // Widen to the nearest ancestors: a styled button and its hidden input are usually siblings.
  let node = anchor;
  for (let step = 0; step < 3; step += 1) {
    node = node.parentElement;
    if (!node) break;
    const nearby = [...node.querySelectorAll("${FILE_INPUT_SELECTOR}")];
    if (nearby.length) return nearby;
  }
  return [];
})()`;
}

/** Describe one file input well enough for an agent to pick between several candidates. */
const DESCRIBE_INPUT_FUNCTION = `function () {
  const labelled = [...(this.labels || [])].map((item) => item.innerText || "").join(" ");
  const control = this.closest("label, button, [role='button']");
  const nearby = labelled || control?.innerText || this.parentElement?.innerText || "";
  const rect = this.getBoundingClientRect();
  const style = getComputedStyle(this);
  return {
    accept: this.accept || null,
    multiple: Boolean(this.multiple),
    name: this.name || null,
    id: this.id || null,
    disabled: Boolean(this.disabled),
    hidden: rect.width <= 2 || rect.height <= 2 || style.display === "none"
      || style.visibility === "hidden" || Number(style.opacity || "1") === 0,
    label: String(nearby).replace(/\\s+/g, " ").trim().slice(0, 80),
  };
}`;

/** Return the remote handles an expression produced, one per array element. */
async function evaluateHandles(debuggerTarget, contextId, expression) {
  const evaluation = await chrome.debugger.sendCommand(debuggerTarget, "Runtime.evaluate", {
    expression,
    contextId,
    returnByValue: false,
  });
  const arrayObjectId = evaluation?.result?.objectId;
  if (!arrayObjectId) return [];
  const properties = await chrome.debugger.sendCommand(debuggerTarget, "Runtime.getProperties", {
    objectId: arrayObjectId,
    ownProperties: true,
  });
  return (properties?.result || [])
    .filter((property) => /^\d+$/.test(String(property?.name || "")) && property?.value?.objectId)
    .map((property) => property.value.objectId);
}

/** Find every candidate file input across injectable frames, in document order. */
async function collectFileInputs(debuggerTarget, frames, elementId) {
  const expression = elementId ? scopedInputsExpression(elementId) : ALL_INPUTS_EXPRESSION;
  const candidates = [];
  for (const frame of frames) {
    let contextId;
    try {
      const world = await chrome.debugger.sendCommand(debuggerTarget, "Page.createIsolatedWorld", {
        frameId: frame.id,
        worldName: "browser-mcp-upload",
      });
      contextId = world?.executionContextId;
    } catch {
      continue; // Sandboxed and browser-owned frames cannot host an isolated world.
    }
    if (!contextId) continue;
    let objectIds = [];
    try {
      objectIds = await evaluateHandles(debuggerTarget, contextId, expression);
    } catch {
      continue;
    }
    for (const objectId of objectIds) {
      let description = {};
      try {
        const described = await chrome.debugger.sendCommand(
          debuggerTarget,
          "Runtime.callFunctionOn",
          { objectId, functionDeclaration: DESCRIBE_INPUT_FUNCTION, returnByValue: true },
        );
        description = described?.result?.value || {};
      } catch {
        // An input that cannot describe itself is still usable as an upload target.
      }
      candidates.push({ objectId, frameId: frame.id, ...description });
    }
  }
  return candidates;
}

/** Render the candidate list an agent needs to retry with an explicit index. */
function describeCandidates(candidates) {
  return candidates
    .map((candidate, index) => {
      const facts = [
        candidate.label ? `label "${candidate.label}"` : null,
        candidate.accept ? `accept ${candidate.accept}` : null,
        candidate.name ? `name ${candidate.name}` : null,
        candidate.id ? `id ${candidate.id}` : null,
        candidate.multiple ? "multiple" : null,
        candidate.hidden ? "hidden" : "visible",
        candidate.disabled ? "disabled" : null,
      ].filter(Boolean);
      return `  index ${index}: ${facts.join(", ")}`;
    })
    .join("\n");
}

/** Choose the single input the request identifies, or explain how to disambiguate. */
function selectCandidate(candidates, args) {
  const scope = args.element_id ? ` near element ${args.element_id}` : "";
  if (!candidates.length) {
    throw new Error(
      `no file input found${scope}. Upload targets are usually hidden, so they never appear in `
      + "browser_snapshot. If the control creates its input only when clicked, pass element_id "
      + "pointing at that control: the click then runs with Chrome's file chooser intercepted, "
      + "so the OS dialog stays shut. Never click an upload control directly -- that opens the "
      + "native picker, which no browser tool can drive.",
    );
  }
  if (args.index != null) {
    const chosen = candidates[args.index];
    if (!chosen) {
      throw new Error(
        `file input index ${args.index} is out of range; ${candidates.length} found:\n`
        + describeCandidates(candidates),
      );
    }
    return chosen;
  }
  if (candidates.length > 1) {
    throw new Error(
      `${candidates.length} file inputs found${scope}; retry with index, or pass element_id to `
      + `scope the search:\n${describeCandidates(candidates)}`,
    );
  }
  return candidates[0];
}

/** Locate one snapshot reference as a remote handle, whichever injectable frame owns it. */
function anchorExpression(elementId) {
  return `(() => {${SHADOW_ROOT_WALK}
  const selector = '[data-browser-mcp-ref="' + CSS.escape(${JSON.stringify(elementId)}) + '"]';
  for (const root of roots) {
    const found = root.querySelector(selector);
    if (found) return [found];
  }
  return [];
})()`;
}

/** Resolve the upload control an agent pointed at, so its click can be intercepted. */
async function resolveAnchorHandle(debuggerTarget, frames, elementId) {
  const expression = anchorExpression(elementId);
  for (const frame of frames) {
    let contextId;
    try {
      const world = await chrome.debugger.sendCommand(debuggerTarget, "Page.createIsolatedWorld", {
        frameId: frame.id,
        worldName: "browser-mcp-upload",
      });
      contextId = world?.executionContextId;
    } catch {
      continue;
    }
    if (!contextId) continue;
    try {
      const [objectId] = await evaluateHandles(debuggerTarget, contextId, expression);
      if (objectId) return objectId;
    } catch {
      continue;
    }
  }
  return null;
}

/** Return the viewport centre CDP reports for one element, scrolled into view first. */
async function anchorClickPoint(debuggerTarget, objectId) {
  try {
    await chrome.debugger.sendCommand(debuggerTarget, "DOM.scrollIntoViewIfNeeded", { objectId });
  } catch {
    // The helper is experimental; a control already in view still clicks correctly without it.
  }
  const { model } = await chrome.debugger.sendCommand(debuggerTarget, "DOM.getBoxModel", {
    objectId,
  });
  const quad = model?.border || model?.content;
  if (!Array.isArray(quad) || quad.length < 8) return null;
  const xs = [quad[0], quad[2], quad[4], quad[6]].map(Number);
  const ys = [quad[1], quad[3], quad[5], quad[7]].map(Number);
  const x = (Math.min(...xs) + Math.max(...xs)) / 2;
  const y = (Math.min(...ys) + Math.max(...ys)) / 2;
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
}

/** Resolve on the next intercepted file chooser for this tab, or reject on timeout. */
function awaitFileChooser(debuggerTarget, timeoutMs) {
  return new Promise((resolve, reject) => {
    const listener = (source, method, params) => {
      if (method !== "Page.fileChooserOpened") return;
      if (source?.tabId != null && source.tabId !== debuggerTarget.tabId) return;
      clearTimeout(timer);
      chrome.debugger.onEvent.removeListener(listener);
      resolve(params || {});
    };
    const timer = setTimeout(() => {
      chrome.debugger.onEvent.removeListener(listener);
      reject(new Error("the control did not open a file chooser within 5s"));
    }, timeoutMs);
    chrome.debugger.onEvent.addListener(listener);
  });
}

/** Send one trusted click, matching the pointer sequence the visual click path uses. */
async function dispatchTrustedClick(debuggerTarget, point) {
  const position = { x: Number(point.x), y: Number(point.y) };
  await chrome.debugger.sendCommand(debuggerTarget, "Input.dispatchMouseEvent", {
    type: "mouseMoved",
    button: "none",
    buttons: 0,
    ...position,
  });
  await chrome.debugger.sendCommand(debuggerTarget, "Input.dispatchMouseEvent", {
    type: "mousePressed",
    button: "left",
    buttons: 1,
    clickCount: 1,
    ...position,
  });
  await chrome.debugger.sendCommand(debuggerTarget, "Input.dispatchMouseEvent", {
    type: "mouseReleased",
    button: "left",
    buttons: 0,
    clickCount: 1,
    ...position,
  });
}

/**
 * Upload through a control that builds its file input only when clicked.
 *
 * The interception has to be lifted again in every outcome: while it is on, Chrome suppresses
 * the picker for the user too, so leaking it would silently break their own upload buttons.
 */
async function uploadViaFileChooser(debuggerTarget, frames, elementId, files) {
  const objectId = await resolveAnchorHandle(debuggerTarget, frames, elementId);
  if (!objectId) {
    throw new Error(
      `element ${elementId} is not on the page; take a fresh browser_snapshot and retry with a `
      + "reference from it",
    );
  }
  const point = await anchorClickPoint(debuggerTarget, objectId);
  if (!point) {
    throw new Error(`element ${elementId} has no clickable box, so its chooser cannot be opened`);
  }
  await chrome.debugger.sendCommand(debuggerTarget, "Page.setInterceptFileChooserDialog", {
    enabled: true,
  });
  try {
    // Listen before clicking: the chooser can open before the click command even resolves.
    const chooser = awaitFileChooser(debuggerTarget, FILE_CHOOSER_TIMEOUT_MS);
    await dispatchTrustedClick(debuggerTarget, point);
    const opened = await chooser;
    const backendNodeId = Number(opened?.backendNodeId);
    if (!Number.isInteger(backendNodeId) || backendNodeId <= 0) {
      throw new Error(
        "the page opened a file chooser that is not backed by a file input, which means it uses "
        + "the File System Access API; that API cannot be filled from outside the page, so the "
        + "user has to pick the file themselves",
      );
    }
    if (files.length > 1 && opened?.mode === "selectSingle") {
      throw new Error(`the control accepts one file but ${files.length} were given`);
    }
    await chrome.debugger.sendCommand(debuggerTarget, "DOM.setFileInputFiles", {
      backendNodeId,
      files,
    });
  } finally {
    try {
      await chrome.debugger.sendCommand(debuggerTarget, "Page.setInterceptFileChooserDialog", {
        enabled: false,
      });
    } catch {
      // A detached debugger already dropped the interception with the session.
    }
  }
}

/**
 * Attach local files to one file input on the managed tab.
 *
 * Falls back to an intercepted file chooser when element_id names a control that has no file
 * input yet, which is how JS upload widgets that create theirs on click are handled.
 *
 * `frames` is supplied by the caller so this module stays independent of the frame-tree
 * traversal and foreign-extension filtering that the interaction pipeline already owns.
 */
export async function executeUpload(debuggerTarget, args, frames) {
  if (!debuggerTarget) throw new Error("file upload requires an attached Chrome debugger");
  const files = Array.isArray(args?.paths) ? args.paths.map(String) : [];
  if (!files.length) throw new Error("file upload requires at least one path");
  const elementId = args?.element_id || null;
  const candidates = await collectFileInputs(debuggerTarget, frames, elementId);
  if (!candidates.length && elementId) {
    // Nothing to find yet: this control builds its input on click, so drive it with the
    // chooser intercepted rather than reporting a page that simply has no upload.
    await uploadViaFileChooser(debuggerTarget, frames, elementId, files);
    return;
  }
  const target = selectCandidate(candidates, args || {});
  if (files.length > 1 && target.multiple === false) {
    throw new Error(
      `file input accepts one file but ${files.length} were given`
      + `${target.label ? ` (label "${target.label}")` : ""}`,
    );
  }
  if (target.disabled) {
    throw new Error(
      `file input is disabled${target.label ? ` (label "${target.label}")` : ""}; `
      + "the page may enable it only after another step",
    );
  }
  // setFileInputFiles fires input and change itself, so the page's own upload handler runs
  // exactly once -- dispatching those events again here would upload every file twice.
  await chrome.debugger.sendCommand(debuggerTarget, "DOM.setFileInputFiles", {
    objectId: target.objectId,
    files,
  });
}
