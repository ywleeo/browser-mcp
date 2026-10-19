/**
 * Authenticated Zhihu API reads performed from inside a Zhihu page.
 *
 * Zhihu validates signing prerequisites that a freshly opened tab does not have in place yet:
 * `tab.status === "complete"` only means the document loaded, while the front end is still
 * settling the cookies the signature derives from. A request sent inside that window is
 * rejected with HTTP 403 and code 10003 ("请求参数异常，请升级客户端后重试"), which looks
 * like a broken tool but clears on its own moments later.
 *
 * The retries wait that window out in the tab that is already open, rather than reopening it:
 * the page context is exactly what needs more time, and reloading would only restart the clock.
 */

export const ZHIHU_FETCH_ATTEMPTS = 3;
export const ZHIHU_RETRY_BACKOFF_MS = 400;

/**
 * Read one Zhihu API URL with the page's own credentials, retrying transient rejections.
 *
 * Injected through chrome.scripting, so it must stay self-contained -- no closure reference
 * survives serialization, which is why the limits arrive as arguments.
 *
 * @returns {Promise<{data: unknown, attempts: number} | {error: string, attempts: number}>}
 */
export async function zhihuApiFetch(url, attempts, backoffMs) {
  const isTransient = (status, body) => {
    if (status === 429 || status >= 500) return true;
    // 10003 is Zhihu's parameter/signature rejection, the one a not-yet-settled tab provokes.
    return status === 403 && body.includes("10003");
  };
  let failure = "zhihu request was never attempted";
  let used = 0;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (attempt > 0) {
      await new Promise((resolve) => setTimeout(resolve, backoffMs * attempt));
    }
    used = attempt + 1;
    try {
      const response = await fetch(url, {
        credentials: "include",
        headers: { Accept: "application/json, text/plain, */*" },
      });
      const body = await response.text();
      if (response.ok) return { data: JSON.parse(body), attempts: used };
      failure = `HTTP ${response.status}: ${body.slice(0, 300)}`;
      // A rejection the page cannot outgrow (auth, not-found, bad query) must surface at once.
      if (!isTransient(response.status, body)) return { error: failure, attempts: used };
    } catch (error) {
      failure = String(error?.message || error);
    }
  }
  return { error: failure, attempts: used };
}

/**
 * Decide what the invitations reader should do after one page comes back.
 *
 * Kept pure and outside the page so each page can be fetched by its own injected
 * `zhihuApiFetch` call, which is what gives every page the retry budget.
 *
 * The next URL is rebuilt from `initialUrl` and carries over only a digits-only `offset`
 * taken from the response: host and path always come from the URL that was already approved,
 * never from whatever the page echoed back.
 *
 * @returns {{done: boolean, complete: boolean, url: string | null, offset: string | null}}
 */
export function nextInvitationsPage(payload, initialUrl, requestedStart) {
  const records = Array.isArray(payload?.data) ? payload.data : [];
  const timestamps = records
    .map((record) => Number(record?.create_time) || 0)
    .filter((timestamp) => timestamp > 0);
  // Reached invitations older than the caller asked for: the requested range is covered.
  if (timestamps.length && Math.min(...timestamps) < requestedStart) {
    return { done: true, complete: true, url: null, offset: null };
  }
  if (payload?.paging?.is_end === true) {
    return { done: true, complete: true, url: null, offset: null };
  }
  let offset = null;
  try {
    offset = new URL(String(payload?.paging?.next || ""), initialUrl).searchParams.get("offset");
  } catch {
    offset = null;
  }
  // An unusable cursor stops the walk without claiming the range was covered.
  if (!offset || !/^\d+$/.test(offset)) {
    return { done: true, complete: false, url: null, offset: null };
  }
  const generated = new URL(initialUrl);
  generated.searchParams.set("offset", offset);
  return { done: false, complete: false, url: generated.toString(), offset };
}
