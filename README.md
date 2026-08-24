# Browser MCP

[![M8ven Verified](https://m8ven.ai/badge/mcp/ywleeo-browser-mcp-1fpz06)](https://m8ven.ai/mcp/ywleeo-browser-mcp-1fpz06)
<!-- m8ven-verify: 95f859e641d62950630af153ffb997b7 -->

[English](README.en.md) · [中文](README.md)

> **让你的 AI 真正能搜遍全网。**
> 所有你能登录访问的站点——小红书、知乎、X、抖音、B 站、Reddit 乃至任意登录态站点——内容都能抓下来，还能在后台替你**自动化操作**：不无头、不逆向、不偷 Cookie。

Browser MCP 是一个本地 MCP server。它让**任何支持 MCP 的 AI 助手**，在你**真实 Chrome** 的登录态里：

- **搜遍并抓取全网任意站点的内容**——公开页面、JavaScript 渲染页面，以及**登录后才看得到**的页面；
- **在后台 Chrome 窗口里自动化操作**——点击、滚动、输入、按键、下拉，**全程不切走你当前正在看的页面**。

| 内置搜索 / 爬虫 | **Browser MCP** |
| --- | --- |
| 搜索引擎有的、能公开爬到的才拿得到 | **全网——只要能登录访问，内容都抓得到** |
| 登录态内容搜不到、进不去 | **用你已登录的状态，直接取** |
| 大多只能读 | **能点击、滚动、输入、按键、下拉** |
| 逆向接口，**平台一改版就崩** | **驱动真实 UI、读真实渲染 DOM，改版也不怕** |
| 易被反爬拦截、易泄漏 Cookie | **不逆向、扩展零 `cookies` 权限、下载带 SHA-256、副作用操作需确认** |
| 只适配个别 Agent | **标准 MCP：Codex / Claude Desktop / Cursor / Claude Code…都能接** |

**完全合规**：不逆向内部接口、不绕过验证码、不窃取 Cookie、不批量抓取——行为就是一个普通登录用户
在浏览。登录态只在你自己的 Chrome 里被正常使用，从不回传，也不通过 MCP 结果返回。

## 你能用它做什么

一句话：**只要能登录访问，就都能抓。** 你的 AI 可以直接在知乎、小红书、X、抖音、B 站、Reddit 乃至
任意站点里搜索、抓内容、做操作——用你已登录的状态拿到平台本身的数据。

- **直接在真实平台里搜**：知乎、小红书、X、抖音、B 站、Reddit —— 用你已登录的状态，取到平台本身的数据，不靠 agent 内置搜索。
- **读任意网页**：正文、页面可见文本、JavaScript 渲染内容、页面请求返回的数据，以及**登录后才看得到**的内容。
- **后台操作网页**：在共享登录态的后台 Chrome 窗口里返回截图 + 编号可操作元素，继续点击、滚动、输入、按键、下拉；不切走你当前的页面。
- **知乎**：搜索、问题、回答、文章、邀请回答。
- **小红书**：搜索、账号发布列表、笔记详情、**完整评论（断点续抓）**、点赞/收藏、图片/视频下载。
- **抖音**：搜索、视频/图文详情、**完整评论（断点续抓）**、点赞/收藏、图片/视频下载。
- **B 站**：视频搜索、内容 meta、分 P 信息、视频或纯音频下载。
- **X**：帖子搜索、帖子详情。
- **Reddit**：帖子搜索、帖子详情、评论。
- **搜索**：Google、必应、搜狗网页搜索。

当前版本为 `0.12.0`，版本变更见 [CHANGELOG.md](CHANGELOG.md)。

点赞、收藏、发布、发送、购买、删除等会产生外部影响的最终操作，应在执行前向用户确认。
扩展会使用当前 Chrome Profile 的登录状态访问页面，但**不会**向 MCP 返回或持久化 Cookie。

---

## 快速开始

### 1. 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Google Chrome

### 2. 安装

```bash
git clone https://github.com/ywleeo/browser-mcp.git
cd browser-mcp
uv sync
```

下文中的 `/path/to/browser-mcp` 表示本项目在本机的绝对路径。

### 3. 加载 Chrome 扩展

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
  "server_version": "0.12.0",
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

### 4. 接入你的 AI 客户端

**Codex**

```bash
codex mcp add browser_mcp -- \
  uv --directory /path/to/browser-mcp run browser-mcp
```

或直接写入 Codex MCP 配置：

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

首次添加或修改配置后重启 Codex。连接成功后，即可使用下方列出的 [MCP 能力](#mcp-能力)。
如需移除配置：`codex mcp remove browser_mcp`。

**Claude Desktop**

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

### 5. 直接开用

连接成功后，直接用自然语言告诉你的 AI 助手，例如：

> “搜索知乎里关于 MCP 的回答。”
> “把这篇小红书笔记的全部图片下载到 `/绝对路径/素材`。”
> “读取这条抖音视频的内容和评论。”
> “打开这个网页，根据截图填写搜索框并点击搜索。”

完整的可直接复述的示例见 [使用示例](#使用示例)。

---

## 详细参考

### 升级

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

`bilibili_download_video` 与 `bilibili_download_audio` 使用同样的绝对目录、覆盖策略和大小
限制。B 站通常返回分离的 DASH 视频/音频轨：视频工具在系统可用 `ffmpeg` 时以 stream copy
无损合并为 MP4；找不到 `ffmpeg` 时返回两个独立轨道文件，不伪装成完整视频。纯音频工具
只保存兼容性最高的音轨。分 P 视频可在 URL 中传入 `?p=N` 指定页面。

### 评论完整性与断点续抓

`xhs_comments` 与 `douyin_comments` 会滚动作评论流、展开回复，并观察页面自身发起的签名分页
请求。结果中的 `complete` 表示已观察到所有已发现评论流的终止页；`limit_reached` 表示因
`max_comments` 截断；`pages_fetched` 和 `scrolls` 可用于诊断采集过程。

热门作品的评论流要滚几分钟，超过任何 MCP 客户端愿意等待的单次调用时长，因此一次调用不追求
抓完：`time_budget_seconds`（默认 40 秒）到点后采集会**挂起而不是失败**，返回这一次新抓到的
评论，并给出 `session_id`。用同一 `url` 加上该 `session_id` 再调一次，即从上次停下的滚动位置
继续，不重复已抓过的评论：

- `budget_exhausted` 表示本次是预算到点收尾，数据完整可用，只是还没抓完；
- `session_id` 非空即可续抓，为空表示已经结束（抓完、达到上限或流已到底）；
- `collected_total` 是该会话累计已收集的评论数，配合 `total` 可判断进度；
- 每次返回的 `items` 只包含**本次新增**的评论，调用方自行合并。

挂起的会话会保留一个后台采集窗口，闲置 5 分钟后自动关闭，之后旧 `session_id` 失效，需要重新
开始采集。把 `time_budget_seconds` 调大可以减少续抓次数，但要确认 MCP 客户端的单次调用超时
（多数默认 60 秒）留得够。

### 点赞与收藏

`xhs_like`、`xhs_collect`、`douyin_like`、`douyin_collect` 接受作品 `url` 和期望状态
`enabled`（默认 `true`）。工具先读取当前页面状态，只有状态不一致时才点击一次，随后只轮询
验证结果；重复传入同一状态不会反向取消。传入 `enabled=false` 可取消点赞或收藏。

这四个工具会修改当前 Chrome Profile 对应账号的外部状态。MCP 客户端必须在每次调用前立即
取得用户明确确认；工具不会把一次未能验证的点击自动重试。

### 扩展排错

- `state: disconnected`：确认扩展已启用，然后在扩展详情页点击“重新加载”。
- 端口被占用：服务会自动尝试 `17880..17889`，以状态结果中的 `bridge_port` 为准。服务会
  监控跳过 `uv` 后的真实 MCP Host；Host 异常退出时自动释放监听端口，不需要 Agent 猜测并
  清理其他进程。
- 扩展目录变化：以最新一次 `browser_status` 返回的 `extension_dir` 为准。
- 不要分享 `pairing.json` 或 `pairing-token`，它们包含本地连接凭据。

特殊进程监督器可以通过 `BROWSER_MCP_OWNER_PID` 显式传入宿主 PID；设置为 `0` 才会关闭
宿主存活监控。普通 Codex、Claude 或命令行配置无需设置此变量。

### MCP 能力

这些工具由支持 MCP 的客户端自动调用。日常使用时直接描述目标即可，不需要手动填写接口参数。

| 工具 | 适用范围 | 能做什么 |
| --- | --- | --- |
| `browser_status` | 连接检查 | 检查 MCP server 与 Chrome 扩展是否连接，并返回服务版本、安装模式、源码 commit 及可直接执行的升级命令。 |
| `browser_read` | 通用网页 | 使用真实 Chrome 打开网页，读取文章正文、页面可见文本、JavaScript 渲染内容及页面请求返回的数据；也能利用当前 Chrome 的网站登录状态。 |
| `browser_read_page` | 通用网页 | 当网页内容较长时继续读取后续内容，并保持与首次读取相同的页面快照。 |
| `browser_snapshot` | 网页操作 | 在共享当前登录态的后台 Chrome 窗口中打开网页，不切走用户当前页面；向 Agent 返回当前视口截图、可见文字以及带编号的按钮、链接、输入框等可操作元素。未提供网址时，可以观察当前页面。 |
| `browser_click` | 网页操作 | 优先按当前截图中的元素编号发送一次 Chrome 可信点击；没有可识别元素时，也能按截图像素坐标点击。截图坐标会按实际位图尺寸映射到 CSS 视口，也可显式传入 `coordinate_space=viewport`。操作后返回新的截图和元素编号。 |
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
| `xhs_comments` | 小红书 | 滚动笔记自身的评论流并展开回复，按评论 ID 去重获取评论与子评论；在时间预算内返回已抓到的增量评论，未抓完时给出可续抓的 `session_id`。 |
| `xhs_user_notes` | 小红书 | 获取当前登录账号或指定账号发布的笔记列表；可连续收集多页并去重，查看标题、发布时间、封面、点赞数、置顶状态和笔记链接。 |
| `douyin_search` | 抖音 | 搜索抖音视频或图文作品，获取作品 ID、描述、作者、发布时间、封面及点赞、评论、收藏和分享数据。 |
| `douyin_video` | 抖音 | 读取单个抖音视频或图文作品的作者、正文、发布时间、互动数据、媒体地址和音乐信息。 |
| `douyin_like` | 抖音 | 将单个作品设置为期望的点赞/未点赞状态；调用前必须确认，重复调用同一状态不会反向取消。 |
| `douyin_collect` | 抖音 | 将单个作品设置为期望的收藏/未收藏状态；调用前必须确认，重复调用同一状态不会反向取消。 |
| `douyin_download` | 抖音 | 将单个视频或图文作品的媒体流式下载到本地目录；支持只选图片、只选视频或全部媒体。 |
| `douyin_comments` | 抖音 | 滚动作品实际评论流并展开回复，按评论 ID 去重获取评论与子评论；在时间预算内返回已抓到的增量评论，未抓完时给出可续抓的 `session_id`。 |
| `bilibili_search` | B 站 | 搜索视频并按综合、播放量、最新、弹幕量或收藏量排序，返回标题、作者、时长、标签、统计数据和规范 BV 链接。 |
| `bilibili_video` | B 站 | 读取 BV/AV 视频的标题、简介、作者、发布时间、互动统计、标签和全部分 P 信息；支持用 `?p=N` 选择分 P。 |
| `bilibili_download_video` | B 站 | 下载指定视频或分 P 的最佳兼容画面和音频；有 FFmpeg 时无损合并为 MP4，否则明确返回两个轨道。 |
| `bilibili_download_audio` | B 站 | 只下载指定视频或分 P 的最佳兼容音轨，保存为可直接识别的音频文件。 |
| `x_search` | X | 搜索 X 上的帖子，可查看热门或最新结果，并获取作者、正文、发布时间、互动数据、媒体和链接。使用当前 Chrome 的 X 登录状态。 |
| `x_post` | X | 读取单条 X 帖子的正文、作者、发布时间、回复数、转发数、点赞数、浏览数、媒体和外部链接。 |
| `reddit_search` | Reddit | 搜索 Reddit 帖子，可按相关性、热门、最高票、最新或评论数排序，查看社区、作者、票数、评论数和帖子链接。 |
| `reddit_post` | Reddit | 读取 Reddit 帖子的正文或媒体信息，并获取页面中已经加载的评论及其作者、时间、得分和层级。 |
| `google_search` | Google | 使用 Google 搜索网页，获取标题、目标网址、站点和内容摘要。 |
| `bing_search` | 必应 | 使用必应搜索网页，获取标题、目标网址、站点和内容摘要。 |
| `sogou_search` | 搜狗 | 使用搜狗搜索网页，返回原始网站链接、标题、站点和摘要；排除搜狗站内导航及带有明确广告标识的结果。 |
| `site_read_page` | 知乎等平台 | 当平台内容较长时继续读取后续内容，不重新访问目标页面，适合完整获取长回答或长文章。 |

### 使用示例

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
- “搜索 B 站关于 OpenAI 的视频，并读取第一条视频的 meta。”
- “下载这个 B 站视频，并另外提取一份纯音频。”
- “搜索 X 上关于 OpenAI 的最新帖子。”
- “读取这条 X 帖子的正文和互动数据。”
- “搜索 Reddit 上关于 MCP 的高票帖子。”
- “读取这个 Reddit 帖子以及前 20 条评论。”
- “分别用 Google、必应和搜狗搜索 Browser MCP。”

网页每次变化后都会生成一组新的元素编号，Agent 应使用最新截图中的编号继续操作。

### 直接运行

如需手动启动 stdio server：

```bash
uv run browser-mcp
```

进程会在 stdin 等待 MCP JSON-RPC，直接在终端运行时没有输出属于正常现象。

## License

[MIT](LICENSE)
