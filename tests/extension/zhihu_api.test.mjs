/** Tests for the retry behaviour that absorbs Zhihu's not-yet-settled-tab rejections. */

import assert from "node:assert/strict";
import test from "node:test";

import {
  ZHIHU_FETCH_ATTEMPTS,
  ZHIHU_RETRY_BACKOFF_MS,
  nextInvitationsPage,
  zhihuApiFetch,
} from "../../extension/zhihu_api.js";

const URL_UNDER_TEST = "https://www.zhihu.com/api/v4/search_v3?q=test";
const CODE_10003 = JSON.stringify({
  error: { message: "请求参数异常，请升级客户端后重试。", code: 10003 },
});

/** Queue scripted responses, recording every call the function makes. */
function mockFetch(responses) {
  const calls = [];
  globalThis.fetch = async (url) => {
    const next = responses[Math.min(calls.length, responses.length - 1)];
    calls.push(url);
    if (next instanceof Error) throw next;
    return {
      ok: next.status >= 200 && next.status < 300,
      status: next.status,
      text: async () => next.body,
    };
  };
  return calls;
}

/** Record backoff delays while running them out immediately, so tests stay fast. */
function mockTimers() {
  const delays = [];
  const original = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms) => {
    delays.push(ms);
    return original(fn, 0);
  };
  return { delays, restore: () => { globalThis.setTimeout = original; } };
}

test("a first-try success reports a single attempt", async () => {
  const calls = mockFetch([{ status: 200, body: '{"data":[]}' }]);

  const result = await zhihuApiFetch(URL_UNDER_TEST, 3, 10);

  assert.deepEqual(result, { data: { data: [] }, attempts: 1 });
  assert.equal(calls.length, 1);
});

test("a 10003 rejection is retried and the recovery is reported", async () => {
  mockFetch([
    { status: 403, body: CODE_10003 },
    { status: 200, body: '{"data":[{"id":1}]}' },
  ]);

  const result = await zhihuApiFetch(URL_UNDER_TEST, 3, 10);

  assert.deepEqual(result, { data: { data: [{ id: 1 }] }, attempts: 2 });
});

test("backoff grows between attempts rather than hammering Zhihu", async () => {
  const timers = mockTimers();
  mockFetch([{ status: 403, body: CODE_10003 }]);

  try {
    await zhihuApiFetch(URL_UNDER_TEST, 3, 400);
  } finally {
    timers.restore();
  }

  assert.deepEqual(timers.delays, [400, 800]);
});

test("a persistent rejection surfaces the last failure after every attempt", async () => {
  const calls = mockFetch([{ status: 403, body: CODE_10003 }]);

  const result = await zhihuApiFetch(URL_UNDER_TEST, 3, 10);

  assert.equal(calls.length, 3);
  assert.equal(result.attempts, 3);
  assert.match(result.error, /HTTP 403/);
  assert.match(result.error, /10003/);
});

test("an error the page cannot outgrow fails immediately", async () => {
  const calls = mockFetch([{ status: 401, body: '{"error":{"code":100}}' }]);

  const result = await zhihuApiFetch(URL_UNDER_TEST, 3, 10);

  assert.equal(calls.length, 1, "auth failures must not be retried");
  assert.equal(result.attempts, 1);
  assert.match(result.error, /HTTP 401/);
});

test("a plain 403 without 10003 is treated as final", async () => {
  const calls = mockFetch([{ status: 403, body: '{"error":{"code":40352}}' }]);

  await zhihuApiFetch(URL_UNDER_TEST, 3, 10);

  assert.equal(calls.length, 1);
});

test("rate limiting and server faults are retried", async () => {
  for (const status of [429, 500, 503]) {
    const calls = mockFetch([{ status, body: "busy" }]);
    await zhihuApiFetch(URL_UNDER_TEST, 2, 10);
    assert.equal(calls.length, 2, `HTTP ${status} should be retried`);
  }
});

test("a dropped connection is retried and can still succeed", async () => {
  mockFetch([new Error("Failed to fetch"), { status: 200, body: '{"ok":true}' }]);

  const result = await zhihuApiFetch(URL_UNDER_TEST, 3, 10);

  assert.deepEqual(result, { data: { ok: true }, attempts: 2 });
});

test("the shipped limits stay bounded enough for an interactive call", () => {
  assert.equal(ZHIHU_FETCH_ATTEMPTS, 3);
  assert.equal(ZHIHU_RETRY_BACKOFF_MS, 400);
  // Worst case waits 400 + 800 ms, well inside the bridge's request timeout.
  assert.ok(ZHIHU_RETRY_BACKOFF_MS * 3 < 2000);
});

const INVITES_URL = "https://www.zhihu.com/api/v4/notifications/v2/recent?entry_name=invite&limit=20";

/** Build one invitations payload with the given record timestamps and paging cursor. */
function invitesPayload(timestamps, paging) {
  return { data: timestamps.map((create_time) => ({ create_time })), paging };
}

test("a normal page hands back the next cursor and keeps walking", () => {
  const step = nextInvitationsPage(
    invitesPayload([1800, 1700], { is_end: false, next: "?entry_name=invite&offset=20" }),
    INVITES_URL,
    1000,
  );

  assert.equal(step.done, false);
  assert.equal(step.offset, "20");
  assert.equal(new URL(step.url).searchParams.get("offset"), "20");
});

test("reaching records older than requested completes the range", () => {
  const step = nextInvitationsPage(
    invitesPayload([1200, 900], { is_end: false, next: "?offset=20" }),
    INVITES_URL,
    1000,
  );

  assert.deepEqual(step, { done: true, complete: true, url: null, offset: null });
});

test("is_end completes the range", () => {
  const step = nextInvitationsPage(
    invitesPayload([1800], { is_end: true, next: "?offset=20" }),
    INVITES_URL,
    1000,
  );

  assert.deepEqual(step, { done: true, complete: true, url: null, offset: null });
});

test("an unusable cursor stops without claiming the range was covered", () => {
  for (const paging of [{}, { next: "" }, { next: "?offset=abc" }, { next: "?limit=20" }]) {
    const step = nextInvitationsPage(invitesPayload([1800], paging), INVITES_URL, 1000);
    assert.equal(step.done, true, `paging ${JSON.stringify(paging)} should stop`);
    assert.equal(step.complete, false, "an unusable cursor is not a covered range");
  }
});

test("records without usable timestamps do not end the walk early", () => {
  const step = nextInvitationsPage(
    invitesPayload([0, Number.NaN], { is_end: false, next: "?offset=20" }),
    INVITES_URL,
    1000,
  );

  assert.equal(step.done, false);
});

test("an empty page still follows its cursor", () => {
  const step = nextInvitationsPage(
    { data: [], paging: { is_end: false, next: "?offset=40" } },
    INVITES_URL,
    1000,
  );

  assert.equal(step.done, false);
  assert.equal(step.offset, "40");
});

test("a cursor pointing elsewhere cannot redirect the walk off Zhihu", () => {
  const step = nextInvitationsPage(
    invitesPayload([1800], { is_end: false, next: "https://evil.example.com/api?offset=20" }),
    INVITES_URL,
    1000,
  );

  const target = new URL(step.url);
  assert.equal(target.origin, "https://www.zhihu.com", "host must come from the approved URL");
  assert.equal(target.pathname, "/api/v4/notifications/v2/recent");
  assert.equal(target.searchParams.get("offset"), "20");
});
