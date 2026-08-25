# Browser MCP

[![M8ven Verified](https://m8ven.ai/badge/mcp/ywleeo-browser-mcp-1fpz06)](https://m8ven.ai/mcp/ywleeo-browser-mcp-1fpz06)

[English](README.en.md) · [中文](README.md)

> **Let your AI really search the whole web.**
> Any site you can reach with a login — Xiaohongshu, Zhihu, X, Douyin, Bilibili, Reddit, or any
> logged-in site — its content can be captured, and it can be **automated** in the background: no
> headless browser, no reverse-engineering, no cookie theft.

Browser MCP is a local MCP server. It lets **any MCP-capable AI assistant**, inside the session state of
your **real Chrome**:

- **Search and capture the content of any site on the web** — public pages, JavaScript-rendered pages,
  and pages that are **only visible after login**;
- **Automate interactions in a background Chrome window** — click, scroll, type, press keys, select —
  **without switching you away from the page you're on**.

| Built-in search / scraper | **Browser MCP** |
| --- | --- |
| Only gets what a search engine has or what's publicly crawlable | **The whole web — if you can log in and reach it, it can be captured** |
| Can't reach logged-in content | **Uses your logged-in state and takes it directly** |
| Mostly read-only | **Can click, scroll, type, press keys, select** |
| Breaks because it depends on reverse-engineered APIs the moment the site changes | **Drives the real UI and reads the real rendered DOM — resilient to redesigns** |
| Easily blocked by anti-bot, leaks cookies | **No reverse-engineering, extension never requests `cookies`, downloads verified by SHA-256, side-effect actions need confirmation** |
| Only works with a couple of agents | **Standard MCP: Codex / Claude Desktop / Cursor / Claude Code… all work** |

**Runs locally; your data stays on your machine**: the MCP server and the Chrome extension run on your own
machine, over localhost (`127.0.0.1`) only — there's no extra remote server, and your session is never
uploaded.

- **Login state never leaks**: the extension never requests the `cookies` permission and never returns or
  persists cookies — your login state is only used normally by the page inside your own Chrome.
- **Records & artifacts stay local**: connection, tool-call, and media-download state are kept locally;
  downloads land on disk with a SHA-256 and aren't reported anywhere else.
- **Fully compliant**: it doesn't reverse-engineer internal APIs, bypass CAPTCHAs, steal cookies, or
  bulk-scrape — it behaves exactly like a normal logged-in user browsing.
- **The one boundary**: the page **content** you ask the AI to read is returned to your assistant as a tool
  result (that's its whole purpose). If your assistant is wired to a model API, that content goes to the
  model service. But **login state and cookies never** go with it — this boundary is separate from "you
  hand content to the AI."

## What you can do

In one line: **if you can reach it with a login, it can be captured.** Your AI can search, collect, and
operate on any site — Zhihu, Xiaohongshu, X, Douyin, Bilibili, Reddit and beyond — using your logged-in
state to get the platform's own data.

- **Search directly inside the real platforms**: Zhihu, Xiaohongshu, X, Douyin, Bilibili, Reddit — with your logged-in state, getting the platforms' own data, not your agent's built-in search.
- **Read any page**: article body, visible text, JS-rendered content, data returned by page requests, and content that's **only visible after login**.
- **Drive a page in the background**: get a screenshot plus numbered actionable elements in a background Chrome window, then keep clicking, scrolling, typing, pressing keys, and selecting options — without leaving the tab you're on.
- **Zhihu**: search, questions, answers, articles, answer invitations.
- **Xiaohongshu**: search, an account's posts, note details, **full comments (resumable)**, like/collect, image & video download.
- **Douyin**: search, video/image-post details, **full comments (resumable)**, like/collect, image & video download.
- **Bilibili**: video search, content metadata, multi-part info, video or audio-only download.
- **X (Twitter)**: post search, post details.
- **Reddit**: post search, post details, comments.
- **Search**: Google, Bing, Sogou.

Current version is `0.12.1`. See [CHANGELOG.md](CHANGELOG.md) for release notes.

Final actions that affect external state — like, collect, publish, send, buy, delete — should be
confirmed with the user before executing. The extension uses the current Chrome Profile's login
state but **never** returns or persists cookies over MCP.

---

## Quick start

### 1. Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Google Chrome

### 2. Install

**Option 1: pip (published, recommended)**

```bash
pip install ai-browser-mcp
```

Then run `browser-mcp` directly (the Chrome extension is bundled in the install; take its directory from
the `extension_dir` returned by `browser_status` — loading it works the same way). To run a specific
version on the fly, use `uvx ai-browser-mcp`.

**Option 2: from source**

```bash
git clone https://github.com/ywleeo/browser-mcp.git
cd browser-mcp
uv sync
```

(`/path/to/browser-mcp` below is the absolute path to this repo in the source setup. With a pip install
you don't need a path — just run the `browser-mcp` command.)

### 3. Load the Chrome extension

1. Call `browser_status` and take the `extension_dir` from the result.
2. Open `chrome://extensions`.
3. Turn on **Developer mode**.
4. Click **Load unpacked**.
5. Pick the directory returned by `extension_dir`.
6. Call `browser_status` again.

A successful connection returns:

```json
{
  "state": "connected",
  "connected": true,
  "bridge_port": 17880,
  "server_version": "0.12.1",
  "install_mode": "source",
  "project_root": "/path/to/browser-mcp",
  "source_commit": "<git-commit>",
  "upgrade_check_command": "uv --directory /path/to/browser-mcp run browser-mcp upgrade --check --json",
  "upgrade_apply_command": "uv --directory /path/to/browser-mcp run browser-mcp upgrade --apply --json"
}
```

`bridge_port` may also fall in `17880..17889`. The extension normally only needs to be loaded once
and reconnects automatically when the MCP server starts. After updating the extension, click
**Reload** in `chrome://extensions` if it doesn't pick up automatically.

### 4. Connect your AI client

**Codex**

```bash
codex mcp add browser_mcp -- \
  uv --directory /path/to/browser-mcp run browser-mcp
```

Or write the Codex MCP config directly:

```toml
[mcp_servers.browser_mcp]
command = "uv"
args = [
  "--directory",
  "/path/to/browser-mcp",
  "run",
  "browser-mcp",
]
```

Restart Codex after adding or editing the config. Once connected, use the [MCP tools](#mcp-tools).
To remove it: `codex mcp remove browser_mcp`.

**Claude Desktop**

Add this to your Claude Desktop config file:

```json
{
  "mcpServers": {
    "browser-mcp": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/browser-mcp",
        "run",
        "browser-mcp"
      ]
    }
  }
}
```

Save and restart Claude Desktop.

**DeepSeek Harness (dsh)**

Register the server as native dsh MCP tools with one command:

```bash
dsh plugin --profile web add "github:ywleeo/browser-mcp#main"
```

Restart `dsh web`. The tools appear as `mcp__browser__*` (for example
`mcp__browser__browser_read`, `mcp__browser__browser_snapshot`). This is a
configuration-only dsh bundle: it wires `@deepseek-ai/dsh-mcp-client` to the
server, and the server itself is fetched from PyPI (`ai-browser-mcp`) via `uvx`,
so `uv` must be on `PATH` but no local checkout is required. Remove it with
`dsh plugin --profile web remove "@ywleeo/dsh-browser-mcp"`.

### 5. Start using it

Once connected, just describe what you want in plain language:

> “Search Zhihu for answers about MCP.”
> “Download all images from this Xiaohongshu note to `/abs/path`.”
> “Read this Douyin video's content and comments.”
> “Open this page, fill the search box from the screenshot, and click search.”

More copy-paste examples are in [Examples](#examples).

---

## Reference

### Upgrading

Source installs provide a safe upgrade command an agent can run directly. First check the version
and repo state:

```bash
uv --directory /path/to/browser-mcp run browser-mcp upgrade --check --json
```

Then apply if it's safe:

```bash
uv --directory /path/to/browser-mcp run browser-mcp upgrade --apply --json
```

The updater only accepts Git branches that have an `upstream`, and follows these safety rules:

- Refuses to upgrade if the worktree has untracked or uncommitted files.
- Refuses to upgrade if local and remote have diverged; no implicit merge commit is created.
- Only updates source with `git pull --ff-only`.
- Syncs locked dependencies with `uv sync --frozen`, never touching `uv.lock`.

After `--apply` succeeds and returns `restart_required: true`, have the client reconnect the MCP
server — start a new task or reconnect it in Codex; only restart the client if it can't reconnect on
its own. A fresh MCP server refreshes the extension bundle, and the already-loaded Chrome extension
auto-reloads by build ID.

Agents don't need to guess the project path: call `browser_status` and use the returned
`upgrade_check_command` / `upgrade_apply_command`. If installed from a wheel or another package
manager, `install_mode` is `"package"`, so upgrade with your original installer instead of mutating
an arbitrary Git repo.

### Extension permissions

- `<all_urls>`: to open public HTTP(S) pages explicitly requested by the caller, and to support the
  per-site adapters. It never crawls browsing history on its own.
- `debugger`: to capture page request responses and to send trusted browser input events in comment
  streams and visual interaction.
- `tabs`, `scripting`: to manage isolated background tabs and run the bundled, fixed extraction scripts.
- `storage`, `alarms`: for local pairing config and MV3 service-worker keep-alive.

The extension does **not** request the `cookies` permission. Login state is only used normally by
the target page inside the current Chrome Profile; cookies are never returned through MCP tool
results.

Zhihu, Xiaohongshu, Douyin, X and Reddit tools check the platform login state of the current Chrome
Profile before doing anything:

- Logged in: proceed.
- Not logged in: stop and return the platform's login URL, and the client prompts the user to log in.
- Can't tell: stop, to avoid touching protected content when the login state is ambiguous.

Login state isn't cached. Once you finish logging in in Chrome, just retry the original request.

### Media download

`xhs_download` and `douyin_download` accept these common parameters:

- `media`: choose `images`, `video`, or `all`.
- `output_dir`: optional absolute dir; defaults to `downloads` under the Browser MCP data dir.
- `overwrite`: default `false`; a same-named file gets a new name unless explicitly set.
- `max_file_mb`: per-file size cap, default 1024 MiB.

Before downloading, the tool reads the post detail through the current Chrome login state, then
validates the page-derived media URL against platform CDN allowlists, public addresses, hop-by-hop
redirects, and the response media type. Files stream to a `.part` temp file and atomically land on
disk; the result includes the final path, byte count, Content-Type, and SHA-256.

`bilibili_download_video` and `bilibili_download_audio` share the same absolute-dir, overwrite, and
size rules. Bilibili usually returns separate DASH video/audio tracks: the video tool losslessly
muxes them into MP4 via `ffmpeg` when available, and returns the two tracks as separate files when
it isn't, rather than pretending to have a complete video. The audio-only tool keeps the most
compatible track. For multipart videos, pass `?p=N` in the URL.

### Comment completeness & resumable collection

`xhs_comments` and `douyin_comments` scroll the comment stream, expand replies, and watch the page's
own signed pagination requests. In the result, `complete` means every discovered comment stream
reached its terminal page; `limit_reached` means `max_comments` truncated the collection;
`pages_fetched` and `scrolls` help diagnose the run.

A popular post's comment stream can take minutes to scroll — longer than any MCP client is willing
to wait in one call — so a single call doesn't try to finish: when `time_budget_seconds` (default
40s) elapses the collection **suspends instead of failing**, returning the newly gathered comments and
a `session_id`. Call again with the same `url` plus that `session_id` to resume from where it
stopped, without re-gathering already-collected comments:

- `budget_exhausted` means the run wrapped up at the budget; the data is complete and usable, just not
  fully collected yet.
- A non-empty `session_id` means you can resume; an empty one means it's done (finished, hit the cap,
  or the stream bottomed out).
- `collected_total` is the session's cumulative count; compare with `total` to gauge progress.
- Each call's `items` holds only the **newly** collected comments; merge them yourself.

A suspended session keeps a background collection window that closes after 5 minutes idle, after
which an old `session_id` is invalid and you must start over. Raise `time_budget_seconds` to resume
fewer times, but make sure the MCP client's single-call timeout (commonly 60s) leaves room.

### Like & collect

`xhs_like`, `xhs_collect`, `douyin_like`, and `douyin_collect` take a post `url` and the desired
`enabled` state (default `true`). The tool reads the current state, clicks once only if it's out of
sync, then polls to verify; passing the same state again doesn't toggle it back. Pass `enabled=false`
to unlike or uncollect.

These four tools modify external account state for the current Chrome Profile. The MCP client must
get the user's explicit confirmation immediately before each call; the tool won't auto-retry a click
that didn't verify.

### Extension troubleshooting

- `state: disconnected`: confirm the extension is enabled, then click **Reload** on its detail page.
- Port in use: the service auto-tries `17880..17889`; trust the `bridge_port` in the status result.
  It monitors the real MCP host behind `uv` and frees the listening port automatically when the host
  exits, so the agent doesn't have to guess at and kill other processes.
- Changed extension dir: rely on the latest `extension_dir` from `browser_status`.
- Never share `pairing.json` or `pairing-token`; they contain local connection credentials.

A special process supervisor can take the host PID via `BROWSER_MCP_OWNER_PID`; set it to `0` only to
disable host-liveness monitoring. Normal Codex, Claude, or CLI configs don't need this variable.

### MCP tools

These tools are called automatically by any MCP-capable client. In daily use just describe your goal;
you don't need to fill in parameters by hand.

| Tool | Scope | What it does |
| --- | --- | --- |
| `browser_status` | connection | Checks whether the MCP server and Chrome extension are connected, and returns server version, install mode, source commit, and runnable upgrade commands. |
| `browser_read` | general | Opens a page in real Chrome and reads article body, visible text, JS-rendered content, and data returned by page requests; can use the current Chrome login state. |
| `browser_read_page` | general | Continues reading paginated content while keeping the same page snapshot as the first read. |
| `browser_snapshot` | interact | Opens a page in a background Chrome window that shares the current login state, without leaving the user's page; returns a viewport screenshot, visible text, and numbered actionable elements (buttons, links, inputs). With no URL, observes the current page. |
| `browser_click` | interact | Moves the trusted pointer and clicks directly at pixels chosen from the current screenshot. The click path traverses neither DOM nor iframes and samples at most the first topmost hover node. `element_id` is only shorthand for a center saved with that screenshot. Screenshot coords map to the CSS viewport; pass `coordinate_space=viewport` explicitly if needed. The action returns a fresh screenshot only; call `browser_snapshot` before another semantic action. |
| `browser_dialog` | interact | Handles Chrome-native `alert`, `confirm`, `prompt`, and leave-page dialogs. `accept` confirms (and leaves for `beforeunload`); `dismiss` cancels and stays. It always returns a fresh screenshot and element map. If the user already pressed Escape, it safely refreshes visual state. |
| `browser_scroll` | interact | Scrolls the page up/down/left/right, or brings a given element into view. |
| `browser_type` | interact | Fills, appends, or replaces text in an input or editable area and returns the resulting state; passwords never appear in the element info. |
| `browser_press` | interact | Sends common keyboard actions: Enter, Escape, Tab, arrow keys, PageUp/Down, Home, End. |
| `browser_select` | interact | Picks an option in a native dropdown and returns the resulting state. |
| `site_login_status` | login | Checks whether the current Chrome Profile is logged into Zhihu, Xiaohongshu, Douyin, X, or Reddit; only checks session state, runs no platform task, and returns no cookies. |
| `zhihu_search` | Zhihu | Searches Zhihu's combined content, answers, articles, or questions; gets titles, authors, summaries, engagement data, and original links. |
| `zhihu_content` | Zhihu | Reads the body of a Zhihu question, answer, or article — good for summarizing, extracting opinions, or deeper analysis. |
| `zhihu_invitations` | Zhihu | Lists answer invitations for the logged-in account, including inviter, related question, time, and source. |
| `xhs_search` | Xiaohongshu | Searches Xiaohongshu notes, sorted by general / newest / hottest; returns titles, authors, covers, and engagement info. |
| `xhs_note` | Xiaohongshu | Reads a single image/video note's title, body, author, publish time, engagement data, and image/video URLs. |
| `xhs_like` | Xiaohongshu | Sets a note to the desired liked/unliked state; confirm before calling; re-sending the same state doesn't toggle it back. |
| `xhs_collect` | Xiaohongshu | Sets a note to the desired collected/uncollected state; confirm before calling; re-sending the same state doesn't toggle it back. |
| `xhs_download` | Xiaohongshu | Streams a note's images, video, or all media to a local dir; defaults to `downloads` under the Browser MCP data dir, or takes an absolute path. |
| `xhs_comments` | Xiaohongshu | Scrolls the note's own comment stream and expands replies, deduping by comment ID; returns incremental comments within a time budget, with a resumable `session_id` if incomplete. |
| `xhs_user_notes` | Xiaohongshu | Lists notes published by the logged-in account or a given account; can page through and dedupe, showing title, publish time, cover, likes, pinned status, and note link. |
| `douyin_search` | Douyin | Searches Douyin video/image posts; gets ID, description, author, publish time, cover, and engagement counts. |
| `douyin_video` | Douyin | Reads a single Douyin video/image post's author, body, publish time, engagement data, media URLs, and music. |
| `douyin_like` | Douyin | Sets a post to the desired liked/unliked state; confirm before calling; re-sending the same state doesn't toggle it back. |
| `douyin_collect` | Douyin | Sets a post to the desired collected/uncollected state; confirm before calling; re-sending the same state doesn't toggle it back. |
| `douyin_download` | Douyin | Streams a video/image post's media to a local dir; supports images only, video only, or all. |
| `douyin_comments` | Douyin | Scrolls the post's actual comment stream and expands replies, deduping by comment ID; returns incremental comments within a time budget, with a resumable `session_id` if incomplete. |
| `bilibili_search` | Bilibili | Searches videos sorted by general, plays, newest, danmaku, or favorites; returns title, author, duration, tags, stats, and canonical BV links. |
| `bilibili_video` | Bilibili | Reads a BV/AV video's title, intro, author, publish time, engagement stats, tags, and all multipart info; supports `?p=N`. |
| `bilibili_download_video` | Bilibili | Downloads the best compatible video+audio for a video or part; muxes losslessly to MP4 with FFmpeg, otherwise returns two tracks explicitly. |
| `bilibili_download_audio` | Bilibili | Downloads only the best compatible audio track of a video or part, saved as a directly playable audio file. |
| `x_search` | X | Searches X posts (top or latest) and gets author, body, publish time, engagement, media, and links; uses the current Chrome X login state. |
| `x_post` | X | Reads a single X post's body, author, publish time, replies, retweets, likes, views, media, and external links. |
| `reddit_search` | Reddit | Searches Reddit posts by relevance, hot, top, new, or comments; gets subreddit, author, score, comment count, and post link. |
| `reddit_post` | Reddit | Reads a Reddit post's body/media and the comments already loaded on the page, with author, time, score, and depth. |
| `google_search` | Google | Searches the web with Google; gets titles, URLs, sites, and snippets. |
| `bing_search` | Bing | Searches the web with Bing; gets titles, target URLs, sites, and snippets. |
| `sogou_search` | Sogou | Searches the web with Sogou; returns original site links, titles, sites, and snippets; excludes Sogou's own site nav and clearly-advertised results. |
| `site_read_page` | Zhihu etc. | Continues reading paginated content for long answers/articles without revisiting the page. |

### Examples

Just ask an MCP-capable client in plain language:

- “Read this page and summarize the key points.”
- “Open this page, fill the search box from the screenshot, and click search.”
- “Scroll down and click the contact-us button.”
- “Download all images from this Xiaohongshu note to `/abs/path`.”
- “Download this Douyin video and return the path and SHA-256.”
- “Check whether I'm logged into Xiaohongshu.”
- “Search Zhihu for answers about MCP.”
- “Read today's Zhihu answer invitations.”
- “Search Xiaohongshu for recent camping notes.”
- “Read this Xiaohongshu note's body and images.”
- “Get all comments and replies on this Xiaohongshu note.”
- “Like and collect this Xiaohongshu note.” (the client confirms before calling)
- “List every post my Xiaohongshu account has published.”
- “Search Douyin for posts about '牵手 APP'.”
- “Read this Douyin video's content and comments.”
- “Unlike this Douyin post.” (the client confirms before calling)
- “Search Bilibili for videos about OpenAI, and read the first video's meta.”
- “Download this Bilibili video, and also extract an audio-only track.”
- “Search X for the latest posts about OpenAI.”
- “Read this X post's body and engagement data.”
- “Search Reddit for high-scoring posts about MCP.”
- “Read this Reddit post and the first 20 comments.”
- “Search for Browser MCP on Google, Bing, and Sogou.”

Each page change produces a fresh set of element numbers; the agent should use the numbers from the
latest screenshot.

### Running directly

To start the stdio server manually:

```bash
uv run browser-mcp
```

The process waits for MCP JSON-RPC on stdin; no output in the terminal is normal.

## License

[MIT](LICENSE)
