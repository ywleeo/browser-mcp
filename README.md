# Browser MCP

[![M8ven Verified](https://m8ven.ai/badge/mcp/ywleeo-browser-mcp-1fpz06)](https://m8ven.ai/mcp/ywleeo-browser-mcp-1fpz06)
<!-- m8ven-verify: 95f859e641d62950630af153ffb997b7 -->

Browser MCP 是一个本地 MCP server，通过用户真实的 Chrome 会话读取和操作公开页面、
JavaScript 渲染页面及登录态页面。

支持以下能力：

- 通用网页内容抽取和长内容分页。
- 在不切走当前页面的后台 Chrome 窗口中，向 Agent 提供网页截图和可操作元素，并继续点击、滚动、输入、按键或选择选项。
- 知乎搜索、问题、回答、文章及邀请回答。
- 小红书搜索、账号发布列表、笔记详情、完整评论、点赞/收藏与图片/视频下载。
- 抖音搜索、视频/图文详情、完整评论、点赞/收藏与图片/视频下载。
- X 帖子搜索及帖子详情。
- Reddit 帖子搜索、帖子详情及评论。
- Google、必应和搜狗网页搜索。

当前版本为 `0.10.0`，版本变更见 [CHANGELOG.md](CHANGELOG.md)。

点赞、收藏、发布、发送、购买、删除等会产生外部影响的最终操作，应在执行前向用户确认。

## 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Google Chrome

## 安装

```bash
git clone https://github.com/ywleeo/browser-mcp.git
cd browser-mcp
uv sync
```

下文中的 `/path/to/browser-mcp` 表示本项目在本机的绝对路径。

## 升级

源码安装提供 Agent 可直接执行的安全升级命令。先检查版本与仓库状态：

```bash
uv --directory /path/to/browser-mcp run browser-mcp upgrade --check --json
```

确认可以升级后执行：

```bash
uv --directory /path/to/browser-mcp run browser-mcp upgrade --apply --json
```

升级器只接受设置了 upstream 的 Git 分支，并遵循以下保护规则：

- 工作区有未提交或未跟踪文件时拒绝升级。
- 本地与远端分叉时拒绝升级，不创建隐式 merge commit。
- 只使用 `git pull --ff-only` 更新源码。
- 使用 `uv sync --frozen` 同步锁定依赖，不修改 `uv.lock`。

`--apply` 成功并返回 `restart_required: true` 后，需要让客户端重新连接 MCP server。Codex
中可开启新任务或重连该 MCP；只有客户端无法单独重连时才需要重启客户端。新 MCP server
启动时会刷新扩展 bundle；已加载的 Chrome 扩展根据 build ID 自动重载，无需重新选择扩展目录。

Agent 不需要猜测项目路径。调用 `browser_status` 后，直接使用返回的
`upgrade_check_command` 和 `upgrade_apply_command` 即可。wheel 或其他包管理器安装会返回
`install_mode: "package"`，此时应使用原安装工具升级，而不是修改任意 Git 仓库。

## 配置 Codex

使用 Codex CLI 添加 MCP server：

```bash
codex mcp add browser_mcp -- \
  uv --directory /path/to/browser-mcp run browser-mcp
```

对应的 Codex MCP 配置为：

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

首次添加或修改配置后重启 Codex。连接成功后，即可使用下方列出的
[MCP 能力](#mcp-能力)。

如需移除配置：

```bash
codex mcp remove browser_mcp
```

## 配置 Claude Desktop

在 Claude Desktop 配置文件中加入：

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

保存配置后重启 Claude Desktop。

## 加载 Chrome 扩展

1. 调用 `browser_status`，获取返回结果中的 `extension_dir`。
2. 打开 `chrome://extensions`。
3. 开启“开发者模式”。
4. 点击“加载已解压的扩展程序”。
5. 选择 `extension_dir` 对应的目录。
6. 再次调用 `browser_status`。

连接成功时会返回：

```json
{
  "state": "connected",
  "connected": true,
  "bridge_port": 17880,
  "server_version": "0.10.0",
  "install_mode": "source",
  "project_root": "/path/to/browser-mcp",
  "source_commit": "<git-commit>",
  "upgrade_check_command": "uv --directory /path/to/browser-mcp run browser-mcp upgrade --check --json",
  "upgrade_apply_command": "uv --directory /path/to/browser-mcp run browser-mcp upgrade --apply --json"
}
```

`bridge_port` 也可能是 `17880..17889` 中的其他端口。扩展通常只需加载一次，之后会在 MCP
server 启动时自动重新连接。更新扩展后若没有自动生效，请在 `chrome://extensions` 中点击
“重新加载”。

扩展会使用当前 Chrome Profile 的登录状态访问页面，但不会向 MCP 返回或持久化 Cookie。

### 扩展权限

- `<all_urls>`：用于打开调用方明确请求的公开 HTTP(S) 页面，并支持多个站点 adapter；不会
  主动遍历浏览历史。
- `debugger`：用于捕获页面请求响应，以及在评论流和视觉交互中发送可信浏览器输入事件。
- `tabs`、`scripting`：用于管理隔离的后台标签页并执行项目内置的固定提取脚本。
- `storage`、`alarms`：用于本地配对配置和 MV3 service worker 保活。

扩展没有申请 `cookies` 权限。登录态只由目标页面在当前 Chrome Profile 内正常使用，Cookie
不会通过 MCP 工具结果返回。

知乎、小红书、抖音、X 和 Reddit 工具会在执行任务前检查当前 Chrome Profile 的平台登录状态：

- 已登录：继续执行请求。
- 未登录：停止任务并返回对应平台的登录地址，客户端会提示用户先登录。
- 无法确认：停止任务，避免在登录状态不明确时继续访问目标内容。

登录状态不做缓存。用户在 Chrome 中完成登录后，可以直接重试原来的请求。

### 媒体下载

`xhs_download` 和 `douyin_download` 支持以下通用参数：

- `media`：选择 `images`、`video` 或 `all`。
- `output_dir`：可选的绝对目录；省略时保存到 Browser MCP 数据目录下的 `downloads`。
- `overwrite`：默认 `false`，同名文件会自动分配新文件名；只有显式设置后才覆盖。
- `max_file_mb`：单文件大小上限，默认 1024 MiB。

下载前会先通过当前 Chrome 登录态读取作品详情，再对页面派生的媒体 URL 执行平台 CDN
白名单、公共地址、逐跳重定向和响应媒体类型校验。文件使用 `.part` 临时文件流式写入，
完成后原子落盘；结果包含最终路径、字节数、Content-Type 和 SHA-256。

### 评论完整性

`xhs_comments` 与 `douyin_comments` 会滚动作评论流、展开回复，并观察页面自身发起的签名分页
请求。结果中的 `complete` 表示已观察到所有已发现评论流的终止页；`limit_reached` 表示因
`max_comments` 截断；`pages_fetched` 和 `scrolls` 可用于诊断采集过程。

### 点赞与收藏

`xhs_like`、`xhs_collect`、`douyin_like`、`douyin_collect` 接受作品 `url` 和期望状态
`enabled`（默认 `true`）。工具先读取当前页面状态，只有状态不一致时才点击一次，随后只轮询
验证结果；重复传入同一状态不会反向取消。传入 `enabled=false` 可取消点赞或收藏。

这四个工具会修改当前 Chrome Profile 对应账号的外部状态。MCP 客户端必须在每次调用前立即
取得用户明确确认；工具不会把一次未能验证的点击自动重试。

### 扩展排错

- `state: disconnected`：确认扩展已启用，然后在扩展详情页点击“重新加载”。
- 端口被占用：服务会自动尝试 `17880..17889`，以状态结果中的 `bridge_port` 为准。
- 扩展目录变化：以最新一次 `browser_status` 返回的 `extension_dir` 为准。
- 不要分享 `pairing.json` 或 `pairing-token`，它们包含本地连接凭据。

## MCP 能力

这些工具由支持 MCP 的客户端自动调用。日常使用时直接描述目标即可，不需要手动填写接口参数。

| 工具 | 适用范围 | 能做什么 |
| --- | --- | --- |
| `browser_status` | 连接检查 | 检查 MCP server 与 Chrome 扩展是否连接，并返回服务版本、安装模式、源码 commit 及可直接执行的升级命令。 |
| `browser_read` | 通用网页 | 使用真实 Chrome 打开网页，读取文章正文、页面可见文本、JavaScript 渲染内容及页面请求返回的数据；也能利用当前 Chrome 的网站登录状态。 |
| `browser_read_page` | 通用网页 | 当网页内容较长时继续读取后续内容，并保持与首次读取相同的页面快照。 |
| `browser_snapshot` | 网页操作 | 在共享当前登录态的后台 Chrome 窗口中打开网页，不切走用户当前页面；向 Agent 返回当前视口截图、可见文字以及带编号的按钮、链接、输入框等可操作元素。未提供网址时，可以观察当前页面。 |
| `browser_click` | 网页操作 | 优先按当前截图中的元素编号点击；没有可识别元素时，也能按截图坐标点击。操作后返回新的截图和元素编号。 |
| `browser_scroll` | 网页操作 | 向上、向下、向左或向右滚动网页，也可以把指定元素滚动到视口中。 |
| `browser_type` | 网页操作 | 在输入框或可编辑区域填写、追加或替换文字，并返回填写后的页面状态；密码内容不会出现在元素信息中。 |
| `browser_press` | 网页操作 | 执行 Enter、Escape、Tab、方向键、翻页键、Home、End 等常用键盘操作。 |
| `browser_select` | 网页操作 | 在网页原生下拉选择框中选择选项，并返回选择后的页面状态。 |
| `site_login_status` | 登录检查 | 查看当前 Chrome Profile 是否已登录知乎、小红书、抖音、X 或 Reddit；只检查会话状态，不执行平台任务，也不会返回 Cookie。 |
| `zhihu_search` | 知乎 | 搜索知乎的综合内容、回答、文章或问题，获取标题、作者、摘要、互动数据和原始链接。 |
| `zhihu_content` | 知乎 | 读取知乎问题、回答或专栏文章的正文，适合总结内容、提取观点或继续分析。 |
| `zhihu_invitations` | 知乎 | 查看当前登录账号收到的邀请回答，了解邀请人、相关问题、邀请时间和来源。 |
| `xhs_search` | 小红书 | 搜索小红书笔记，并按综合、最新或最热查看标题、作者、封面及互动信息。 |
| `xhs_note` | 小红书 | 读取单篇图文或视频笔记的标题、正文、作者、发布时间、互动数据及图片或视频地址。 |
| `xhs_like` | 小红书 | 将单篇笔记设置为期望的点赞/未点赞状态；调用前必须确认，重复调用同一状态不会反向取消。 |
| `xhs_collect` | 小红书 | 将单篇笔记设置为期望的收藏/未收藏状态；调用前必须确认，重复调用同一状态不会反向取消。 |
| `xhs_download` | 小红书 | 将单篇笔记的图片、视频或全部媒体流式下载到本地目录；默认使用 Browser MCP 数据目录下的 `downloads`，也可指定绝对路径。 |
| `xhs_comments` | 小红书 | 滚动笔记自身的评论流并展开回复，按评论 ID 去重获取评论与子评论，同时返回完整性和数量上限信息。 |
| `xhs_user_notes` | 小红书 | 获取当前登录账号或指定账号发布的笔记列表；可连续收集多页并去重，查看标题、发布时间、封面、点赞数、置顶状态和笔记链接。 |
| `douyin_search` | 抖音 | 搜索抖音视频或图文作品，获取作品 ID、描述、作者、发布时间、封面及点赞、评论、收藏和分享数据。 |
| `douyin_video` | 抖音 | 读取单个抖音视频或图文作品的作者、正文、发布时间、互动数据、媒体地址和音乐信息。 |
| `douyin_like` | 抖音 | 将单个作品设置为期望的点赞/未点赞状态；调用前必须确认，重复调用同一状态不会反向取消。 |
| `douyin_collect` | 抖音 | 将单个作品设置为期望的收藏/未收藏状态；调用前必须确认，重复调用同一状态不会反向取消。 |
| `douyin_download` | 抖音 | 将单个视频或图文作品的媒体流式下载到本地目录；支持只选图片、只选视频或全部媒体。 |
| `douyin_comments` | 抖音 | 滚动作品实际评论流并展开回复，按评论 ID 去重获取评论与子评论，同时返回完整性和数量上限信息。 |
| `x_search` | X | 搜索 X 上的帖子，可查看热门或最新结果，并获取作者、正文、发布时间、互动数据、媒体和链接。使用当前 Chrome 的 X 登录状态。 |
| `x_post` | X | 读取单条 X 帖子的正文、作者、发布时间、回复数、转发数、点赞数、浏览数、媒体和外部链接。 |
| `reddit_search` | Reddit | 搜索 Reddit 帖子，可按相关性、热门、最高票、最新或评论数排序，查看社区、作者、票数、评论数和帖子链接。 |
| `reddit_post` | Reddit | 读取 Reddit 帖子的正文或媒体信息，并获取页面中已经加载的评论及其作者、时间、得分和层级。 |
| `google_search` | Google | 使用 Google 搜索网页，获取标题、目标网址、站点和内容摘要。 |
| `bing_search` | 必应 | 使用必应搜索网页，获取标题、目标网址、站点和内容摘要。 |
| `sogou_search` | 搜狗 | 使用搜狗搜索网页，返回原始网站链接、标题、站点和摘要；排除搜狗站内导航及带有明确广告标识的结果。 |
| `site_read_page` | 知乎等平台 | 当平台内容较长时继续读取后续内容，不重新访问目标页面，适合完整获取长回答或长文章。 |

## 使用示例

可以直接向支持 MCP 的客户端提出自然语言请求，例如：

- “读取这个网页并总结重点。”
- “打开这个网页，根据截图填写搜索框并点击搜索。”
- “向下滚动页面，找到联系我们按钮并点击。”
- “把这篇小红书笔记的全部图片下载到 `/绝对路径/素材`。”
- “下载这个抖音作品的视频，返回保存路径和 SHA-256。”
- “检查我是否已经登录小红书。”
- “搜索知乎里关于 MCP 的回答。”
- “读取今天收到的知乎邀请回答。”
- “搜索小红书最近的露营笔记。”
- “读取这条小红书笔记的正文和图片。”
- “获取这条小红书笔记的全部评论和回复。”
- “点赞并收藏这条小红书笔记。”（客户端会在实际调用前确认）
- “列出我小红书账号发布的全部帖子。”
- “搜索抖音里关于牵手 APP 的作品。”
- “读取这条抖音视频的内容和评论。”
- “取消点赞这条抖音作品。”（客户端会在实际调用前确认）
- “搜索 X 上关于 OpenAI 的最新帖子。”
- “读取这条 X 帖子的正文和互动数据。”
- “搜索 Reddit 上关于 MCP 的高票帖子。”
- “读取这个 Reddit 帖子以及前 20 条评论。”
- “分别用 Google、必应和搜狗搜索 Browser MCP。”

网页每次变化后都会生成一组新的元素编号，Agent 应使用最新截图中的编号继续操作。

## 直接运行

如需手动启动 stdio server：

```bash
uv run browser-mcp
```

进程会在 stdin 等待 MCP JSON-RPC，直接在终端运行时没有输出属于正常现象。

## License

[MIT](LICENSE)
