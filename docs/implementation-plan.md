# Browser MCP 分阶段实施计划

> 状态：阶段 1—8 已实现；阶段 9 抖音首版读取能力已实现，等待真实插件验收
> 前置设计：[architecture.md](architecture.md)
> 执行规则：每个阶段完成后停止开发，由用户验收；只有收到确认才进入下一阶段。

## 1. 已确认范围

- 主实现：Python，使用 `uv` 管理环境与锁文件。
- MCP transport：本地 stdio。
- Chrome extension：独立命名，独立端口池 `17880..17889`。
- MVP：优先验证真实 Chrome fetch 主链路。
- 站点顺序：知乎/小红书 → X/Reddit → 搜索引擎 → 抖音/淘宝/微博。
- 页面写操作：不在当前计划中。

## 2. 质量门禁

每个阶段必须同时满足：

- 代码格式化、静态检查和自动化测试通过。
- 新增文件、类和函数有必要的 docstring；复杂协议和异常路径有注释。
- MCP stdout 没有任何日志污染，日志全部进入 stderr。
- 不修改 Robin 工作区，不依赖 Robin 运行时。
- 提供明确的用户验收命令、预期结果和故障日志位置。
- 用户确认后才开始下一阶段。

建议基础工具链：

- Python 3.12+。
- `uv`：环境、依赖、lockfile、console script。
- 官方 `mcp` Python SDK：stdio server 与工具定义。
- `websockets`：extension bridge。
- Pydantic/dataclass：协议与领域模型。
- `pytest` + `pytest-asyncio`：单元和异步集成测试。
- Ruff：格式化与 lint。
- Pyright：类型检查。

具体依赖版本在阶段 1 创建项目时锁定，不在计划文档中写浮动版本。

## 3. 阶段总览

| 阶段 | 目标 | 用户验收重点 |
| --- | --- | --- |
| 0 | 架构与计划确认 | 文档范围、顺序、工具命名 |
| 1 | MCP 空骨架 | Claude/Codex 能列出工具并调用诊断工具 |
| 2 | 独立 extension bridge | extension 能连接 `17880..17889`，状态可探测 |
| 3 | Fetch MVP | 真实 Chrome 能读取静态页、JS 页、登录态页并分页 |
| 4 | 知乎 adapter | 搜索、问题/回答/文章解析 |
| 5 | 小红书 adapter | 搜索、笔记详情、账号笔记及评论流读取 |
| 6 | X/Twitter adapter | 搜索、帖子、回复解析 |
| 7 | Reddit adapter | 帖子、评论、subreddit/search 列表 |
| 8 | 搜索引擎 adapters | Google 先行，其他引擎逐个验收 |
| 9 | 抖音 adapter | 搜索、热门、精选、视频、评论 |
| 10 | 淘宝 adapter | 搜索、商品详情 |
| 11 | 微博 adapter | 搜索、帖子、评论与 Chrome fallback |
| 12 | 本地发布体验 | 安装脚本、客户端配置、打包评估 |

阶段 4 之后，每个站点都是独立 adapter，不允许为了赶进度跨站点共享脆弱 selector 或 JSON path。

## 4. 阶段 1：MCP 空骨架（已通过 Codex 客户端验收）

### 实现

- 创建 `pyproject.toml`、`uv.lock` 和 `src/browser_mcp/` package。
- 建立 `config`、`mcp`、`application`、`bridge`、`snapshot`、`security` 模块边界。
- 配置 stderr tracing/logging。
- 注册最小工具 schema：
  - `browser_status`
  - `browser_read`
  - `browser_read_page`
- `browser_status` 在 bridge 尚未实现时返回明确的 `not_started` 诊断结构。
- `browser_read`/`browser_read_page` 返回“阶段 2/3 尚未启用”的稳定 MCP tool error，而不是进程异常。
- 增加 stdio contract test：initialize、tools/list、tools/call、EOF shutdown。

### 自动化验收

```bash
uv sync
uv run ruff check .
uv run pyright
uv run pytest
```

### 用户验收

- 在一个客户端中注册本地 stdio server。
- 确认可以看到三个工具。
- 调用 `browser_status`，确认返回结构化诊断且 MCP 进程保持运行。

### 完成定义

Claude 或 Codex 至少一个客户端真实完成 `tools/list` 和 `browser_status` 调用。

## 5. 阶段 2：独立 extension bridge（已通过真实 Chrome 验收）

### 实现

- 从 Robin 提取 extension，全面改名为 `browser-mcp-extension`。
- storage key、WebSocket path、日志前缀、manifest 描述全部去 Robin 化。
- 使用独立端口池 `17880..17889`。
- 实现 extension bundle 释放、build fingerprint、版本探测和安装目录返回。
- 源码模式固定使用项目根目录 `extension/`；pairing/build 运行时文件不进入 Git，wheel 安装
  保留用户数据目录回退。
- 源码升级通过 `browser-mcp upgrade --check/--apply --json` 暴露给 Agent：拒绝脏工作区和
  分叉分支，只允许 fast-forward，并在 Codex 重启后由 build ID 机制自动重载扩展。
- Python bridge 实现：
  - 端口池抢占。
  - WebSocket path 校验。
  - pairing token hello/auth。
  - ping/pong 与 MV3 保活。
  - pending request/request id。
  - timeout、断连清理、优雅退出。
- `browser_status` 返回：连接状态、监听端口、extension 目录、extension 版本、build id、
  服务版本、安装模式、源码 commit、可执行升级命令和最近握手时间。
- 使用 mock extension 完成协议集成测试。

自动化和真实验收均通过：实时 ping/pong、错误 token、错误 path、端口池回退、断线重连、
生成文件权限和 stdio 生命周期通过；真实 Chrome 返回 `connected=true`、Extension 0.2.0。

### 用户验收

1. 调用 `browser_status` 获取 unpacked extension 目录。
2. 在 `chrome://extensions` 开启开发者模式并加载该目录。
3. 再次调用 `browser_status`，预期 `connected=true`，端口位于 `17880..17889`。
4. 同时运行 Robin，确认两套 extension/端口互不抢占。
5. 重启 MCP、重启 Chrome各一次，确认能够自动重连。

### 完成定义

真实 Chrome extension 可稳定连接；错误 token、错误 path 和模拟断连测试通过。

## 6. 阶段 3：Fetch MVP（已通过真实 Chrome 与 stdio MCP 验收）

### 实现

- 只保留 extension 中 `browser.fetch` 所需代码，站点 action 暂不注册。
- 支持四种读取模式：
  - `readability`：正文。
  - `text`：可见文本。
  - `raw`：rendered HTML。
  - `xhr`：页面加载期间 fetch/XHR response。
- 保留 30 秒未达到 `complete` 时返回已渲染 DOM 的容错，并在结果中标记 `load_timed_out`。
- 实现 URL policy：scheme、hostname、IP、DNS 和 redirect 校验。
- 实现不可变 snapshot store：
  - 最多 32 个快照。
  - TTL 2 小时。
  - 单页最多 24,000 UTF-8 bytes。
  - 增加总内存预算与 LRU/最旧淘汰。
- `browser_read` 返回首屏与续页元数据。
- `browser_read_page` 只读取已有快照，不重新访问网页。
- 为 Readability、Unicode 分页、XHR 格式和超时快照增加 fixtures/tests。

实现补强：Extension 对初始导航及每次 Document 重定向执行 Python DNS/IP policy 回问；最终
URL 再次校验。Extension 对单个 rendered DOM/XHR body 设置有诊断信息的桥接上限，Python
快照总内存预算为 32 MiB。为兼容本机代理的 RFC 2544 fake-IP，只对 `198.18.0.0/15` 启用公共
DoH 复核；普通私网结果仍直接拒绝。

最终结果：35 项自动化测试、Ruff、Pyright、Extension JS/manifest 和 wheel 资源检查通过。
真实 Chrome 验收通过 Example Domain text、Wikipedia Readability 和连续分页、React rendered
HTML、Reddit 12 条 Fetch/XHR，以及未跳转登录页的 Reddit Settings 登录态读取。官方 stdio
MCP client 完成 `initialize → browser_read → browser_read_page` 全链路。

### 用户验收矩阵

| 场景 | 模式 | 预期 |
| --- | --- | --- |
| 普通文章 | readability | 标题、最终 URL、正文存在，导航噪声被过滤 |
| SPA 页面 | text | 能读取 JS 渲染后的可见文本 |
| 结构化页面 | raw | 返回 rendered HTML，而非初始空壳 |
| 有 API 请求的页面 | xhr | 至少返回匹配的 request metadata/body |
| 登录后页面 | text 或 xhr | 使用当前 Chrome profile 登录态读取 |
| 长页面 | 任意 | `complete=false`，连续续页可无损重建全文 |
| 慢页面 | 任意 | 超时后仍返回可用快照并带 warning |
| 本地/内网 URL | 任意 | 在导航前拒绝 |

### 完成定义

用户在 Claude/Codex 中至少完成：一个普通文章、一个 JS 页面、一个登录态页面、一次续页读取。MVP 到此结束并暂停，根据测试反馈修复后才进入站点迁移。

## 7. 阶段 4：知乎（实现完成，等待用户验收）

### 迁移范围

- `zhihu_search`：关键词与内容类型搜索。
- `zhihu_content`：问题、回答、文章。
- 优先复用 Robin 的 URL 分类、SSR `js-initialData` 解析、format fixtures。
- 获取策略由 adapter 决定：公开 HTTP 优先，遇到 challenge/登录墙时允许回退真实 Chrome fetch。

### 实现结果

- `zhihu_search` 在本地 Chrome 同源页面中执行，自动使用当前 Profile Cookie。
- `zhihu_content` 严格支持 question/answer/article 数字 URL，并解析 SSR `js-initialData`。
- `zhihu_invitations` 使用登录态通知 API，按中国时区日期过滤并提供完整性标记。
- 站点正文进入独立不可变快照，由 `site_read_page` 续页。
- 搜索 URL、SSR question/answer/article、application 路由和续页均有离线回归测试。
- 真实 Chrome 已完成关键词搜索与长专栏文章读取。

### 用户验收

- 搜索一个关键词。
- 读取一个问题、一个回答、一个专栏文章。
- 验证长内容续页。
- 验证遇到反爬页面时返回可诊断错误或成功走 Chrome fallback。

## 8. 阶段 5：小红书（5A 实现完成，等待用户验收）

### 迁移范围

- `xhs_search`：页面触发 signed XHR 并整形结果。
- `xhs_note`：读取 SSR initial state。
- `xhs_user_notes`：合并账号页 SSR 首屏和 signed `user_posted` 分页响应。
- `xhs_comments`：滚动详情弹层自己的 `.note-scroller`，捕获页面签名的评论分页响应，展开子评论并按评论 ID 去重。
- `xhs_note`：从已执行的 `window.__INITIAL_STATE__.note.noteDetailMap` 读取最小字段快照，
  不再把包含 `Map` 等 JavaScript 值的 SSR 源码误当作严格 JSON。
- 迁移 URL、shape、format fixtures；协议保留原始 JSON 与 typed output 的诊断边界。

### 5A 实现结果

- `xhs_search` 使用 `document_start` MAIN-world observer 旁路站点自身 signed fetch/XHR；不读取、
  复制或输出 Cookie。
- 搜索响应兼容 `/v1/search/notes` 与 `/v2/search/notes` 灰度路径。
- `xhs_note` 保留 `xsec_token/xsec_source`，在同源登录态页面读取 SSR initial state。
- `xhs_user_notes` 默认发现当前登录账号，也支持显式 `user_id`；滚动仅用于触发页面自身的
  signed 分页请求，返回去重后的内容列表和明确完整性标记。
- 搜索/详情 URL、图文、视频、互动数和 service 路由均有离线回归测试。
- 真实本地 Chrome 已完成搜索 26 条和首条图文详情读取（16 张图片）。
- 评论工具返回完整性、数量上限、分页数和滚动次数，达到终止条件或预算时明确停止。

### 用户验收

- 在已登录 Chrome 中搜索关键词。
- 读取图文笔记和视频笔记各一条。
- 验证标题、作者、正文、互动数、图片/视频 URL。
- 持续用真实登录态回归评论分页、子评论展开、去重和停止条件。

## 9. 阶段 6：X/Twitter

### 迁移范围

- 搜索页面拦截 `SearchTimeline`。
- 帖子和回复拦截 `TweetDetail`。
- 保留 rotating GraphQL hash 无关的 operation-name 匹配。
- 迁移 tweet unwrap、note tweet、媒体最佳码率、tombstone 过滤 fixtures。

### 用户验收

- 登录态关键词搜索。
- 读取普通帖子、长文帖子、带图片/视频帖子。
- 读取回复并确认不把原帖重复算作回复。

## 10. 阶段 7：Reddit

### 迁移范围

- 帖子与评论树。
- subreddit listing。
- Reddit search。
- `.json` 直连优先；被匿名限制时使用 Chrome/cookie 或 rendered fallback。

### 用户验收

- 读取一个帖子及嵌套评论。
- 读取一个 subreddit 列表。
- 执行 subreddit/global search。
- 验证删除内容、more-comments 和限流错误的可诊断性。

## 11. 阶段 8：搜索引擎

每个搜索引擎单独验收，不一次性混做：

1. Google：先迁移 Robin 已有 rendered result parser。
2. 其余引擎：在进入阶段 8 时根据用户实际需求确定顺序；候选为 Bing、百度、DuckDuckGo。

统一内部 SearchResult model，但每个 engine 保持自己的 acquire/parser adapter。对外既可提供明确的 `google_search` 等窄工具，也可在各 adapter 稳定后增加一个只做路由的 `web_search`。

## 12. 阶段 9：抖音

- 搜索、热门、精选、视频详情、评论。
- 页面自身发起签名请求，extension 只导航/观察，不复制签名算法。
- 迁移 URL、shape、format 和日期转换 fixtures。

### 首版实现结果

- 新增 `douyin_search`、`douyin_video`、`douyin_download`、`douyin_comments` 四个窄工具，并把抖音纳入
  `site_login_status` 的 fail-closed 登录检查。
- 搜索捕获页面自己的 `/general/search/stream/` 流式响应；服务端兼容普通 JSON、连续 JSON
  对象和带 HTTP chunk framing 的响应，不实现或保存抖音签名算法。
- 作品详情捕获 `/aweme/detail/`，统一输出视频/图文类型、作者、发布时间、互动数据、封面、
  媒体地址和音乐信息。
- 小红书和抖音分别提供窄下载工具：先复用各自详情 adapter 获取页面实际媒体 URL，再由
  服务端对平台 CDN、所有重定向、响应类型和单文件大小进行校验，使用 `.part` 临时文件原子
  落盘；默认不覆盖已有文件。
- 评论采集在隔离的非聚焦窗口中识别包含“全部评论”的实际滚动容器，发送原生滚轮事件、
  展开回复并捕获 root/reply signed responses；桥接前裁剪为公开合同所需字段，避免原始响应
  膨胀污染全局协议。
- 热门和精选列表留在阶段 9 后续增量，不复用搜索或评论的脆弱 selector。

## 13. 阶段 10：淘宝

- 搜索与商品详情。
- 使用真实登录态页面和 rendered DOM extractor。
- selector 失败时输出诊断摘要，不把整页 HTML写入日志。

## 14. 阶段 11：微博

- 搜索 HTML parser。
- 帖子与评论 API。
- 直连优先，Chrome rendered fallback。
- Cookie/CSRF 不进入日志或持久化快照元数据。

## 15. 阶段 12：本地发布体验

- README：Chrome extension 安装、Claude/Codex 配置、升级、排错。
- 提供 `uv tool install`/`uvx` 运行方式。
- 验证 macOS、Windows；Linux 保持 best-effort。
- 评估 PyInstaller/standalone binary，但 extension 仍需释放到稳定目录。
- 最终 smoke test 覆盖两个客户端同时运行和端口池分配。

## 16. 明确延期项

- page click/type/upload。
- 任意 JavaScript eval。
- 点赞、评论、发帖、购买等网站写操作。
- 公网 Streamable HTTP 部署。
- Chrome Web Store 发布。

这些能力不预留空工具，也不污染 MVP schema；需要时重新做安全与产品评审。
