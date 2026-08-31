# 变更日志

本项目按[语义化版本](https://semver.org/lang/zh-CN/)维护版本号。

## [0.13.3] - 2026-08-31

### 修复

- 一个配对进程退出时不再关闭其他客户端挂起的评论采集会话。这是 0.13.2 修掉的读取标签页缺陷的同类：
  `bridge.shutdown` 过去调用全局的 `closeAllCommentSessions()`，把所有客户端的会话连同窗口和
  debugger 一起释放，另一个客户端的续抓因此失去 `session_id`。会话现在记录开启它的 bridge port，
  退出只回收自己那一份。从旧存储格式恢复的会话没有 port，不会被任何 shutdown 认领，改由空闲清扫
  回收——宁可晚回收，也不误杀。

## [0.13.2] - 2026-08-31

### 修复

- 一个配对进程退出时不再关闭其他客户端正在进行的读取。多个配对进程可以同时驱动同一个 Chrome
  profile，而 `bridge.shutdown` 过去会无差别关掉所有登记在案的读取标签页，另一个客户端的在途读取
  因此报 `No tab with id`。读取标签页现在按 bridge port 记账，退出只回收自己那一份，与
  `cleanupBridgeSessionsForPort` 的既有做法一致。

## [0.13.1] - 2026-08-31

### 修复

- 普通网页读取不再 attach `chrome.debugger`，Chrome 的调试横幅随之消失。0.13.0 判断错了这个横幅
  的作用域：它由 Chromium 的 `GlobalConfirmInfoBar` 绘制，只要有扩展 attach 了 debugger，profile
  内**所有**窗口的所有标签页都会显示，把读取标签页挪进独立后台窗口不可能让它消失。现在
  `browser.fetch` 只在 `extract="xhr"` 时 attach（`Network.getResponseBody` 没有扩展 API 替代品），
  其余模式直接导航目标 URL，并省掉一次 `about:blank` 中转。视觉交互、评论采集与点赞收藏依赖可信
  输入事件，仍然 attach。

### 变更

- 读取路径不再逐跳校验重定向。目标 URL 在打开标签页前仍走 Python `PublicUrlPolicy`（scheme、内网
  hostname、非公网 IP、DNS 解析结果），但 Chrome 自行跟随的重定向不再回问策略：逐跳校验只能靠
  `Fetch.requestPaused`，而它正是横幅的来源。走 Python httpx 的媒体下载不受影响，仍逐跳校验。

### 移除

- 0.13.0 引入的共享最小化后台窗口移除，只读标签页回到用户当前窗口，以 `active: false` 打开、用完
  即关。它当初是为了隔离横幅而建，而横幅根本不受窗口影响；横幅已由上一条修复，多出来的那个窗口只
  剩碍事。`extension/background_tabs.js` 保留为只读标签页的唯一入口，负责登记标签页并在 bridge
  断开时回收 service worker 被回收后残留的标签页。

## [0.13.0] - 2026-08-31

### 新增

- 新增 `browser_dialog`，通过 CDP 原生处理 `alert`、`confirm`、`prompt` 与 `beforeunload`
  离开页面确认框；接受或取消后立即返回完整新截图和元素引用。

### 改进

- 只读适配器的标签页不再建在用户当前窗口，而是统一放进一个共享的最小化后台窗口
  （`extension/background_tabs.js`）。`browser.fetch` 为校验每一跳导航必须 attach
  `chrome.debugger`，Chrome 会在被附加标签页所在窗口顶部强制显示调试横幅且扩展无法关闭；
  换窗口之后横幅和后台标签页都不再打扰用户正在使用的窗口，导航校验能力不变。该窗口按需创建、
  跨调用复用、空闲两分钟后关闭，并在 service worker 重启后可恢复。

### 修复

- Chrome 原生 dialog 打开时保留 debugger 会话并暂停普通页面操作；用户手动按 Esc 关闭后会让旧视觉
  状态失效。后续若 Agent 使用旧元素或旧坐标，操作会被安全跳过并自动返回新截图，不再以
  `element not found` 错误卡死。

## [0.12.2] - 2026-08-24

### 修复

- 通用视觉快照现在扫描全部可注入 frame 与开放 Shadow DOM，并通过 CDP pierced DOM 将元素框
  统一映射到主视口；`browser_click` 则成为完全独立的截图坐标管线，不遍历 DOM 或 iframe，也不把
  后端节点 ID 作为点击前置条件。它只移动可信鼠标、读取坐标下第一个最上层 hover 节点并按下释放；
  `element_id` 仅作为最新截图中心点的简写。点击后仅用 CDP layout metrics 与新截图返回视觉结果。
- `browser_type` 改用 Chrome `Input.insertText` 可信输入管线，不再直接改写 input、textarea 或
  contenteditable 的 DOM 值；React 等受控编辑器会收到真实输入状态更新，避免正文已经显示但
  提交按钮仍保持禁用。
- frame 注入改为通过 `webNavigation` 逐个执行并跳过其他扩展拥有的 frame，避免密码管理器等
  扩展在输入框聚焦后注入 `chrome-extension://` iframe，导致整个交互结果捕获失败。
- 非点击语义操作会从 Browser MCP 隔离页面中移除外部扩展 iframe 容器；截图坐标点击完全绕开
  iframe 处理。普通网页 iframe 与 reCAPTCHA frame 不受影响，避免密码管理器 frame 干扰交互。

## [0.12.1] - 2026-08-24

### 文档

- README 安装改为以 `pip install ai-browser-mcp`（或 `uvx ai-browser-mcp`）为首选，源码安装作为备选。
- 新增「本地运行，数据不出本机」隐私段：MCP server 与扩展只走 localhost、登录态与 Cookie 从不经
  MCP 传出、记录与产物留本地；并明确边界说明（读取的页面内容会返回给 AI 助手）。

## [0.12.0] - 2026-08-24

### 发布与文档

- 包名改为 `ai-browser-mcp`（PyPI 原名 `browser-mcp` 已被占用），并新增
  `.github/workflows/publish.yml`：打 `v*` tag 时用仓库的 `PYPI_API_TOKEN` 自动构建并发布。
- README 重写为 pitch 前置：开头突出「让你的 AI 真正搜遍全网、登录能访问的站点都能抓、还能后台
  自动化操作」，并新增英文版 `README.en.md`；PyPI 页面以此为 `readme`。

### 改进

- `xhs_comments` 与 `douyin_comments` 改为限时采集：一次调用在 `time_budget_seconds`
  （默认 40 秒）到点后挂起而不是超时失败，返回本次新抓到的评论、`collected_total` 进度和
  可续抓的 `session_id`；带上该 `session_id` 再次调用即从上次的滚动位置、去重集合和分页
  状态继续，不重复已抓过的评论。此前热门作品的评论采集会在 MCP 客户端 60 秒超时时整批丢弃。
- 新增 `extension/comment_sessions.js`：评论采集会话的窗口、去重集合与循环状态在调用之间
  存活，镜像到 `chrome.storage.session` 以便 service worker 回收后恢复或清理，闲置 5 分钟
  由 alarm 关闭，配对进程退出时立即全部释放。
- 评论采集的 bridge 超时不再是固定 180 秒，而是按本次预算加 15 秒余量，触发它明确表示扩展
  卡死而非采集慢。

### 修复

- 通用 `browser_click` 不再调用会产生 `isTrusted=false` 事件的 `element.click()`，改为在
  debugger 导航守卫存续期间解析可命中的 DOM 坐标，并只发送一次 CDP 可信鼠标点击。
- 截图坐标现在按 JPEG 实际像素尺寸映射到 CSS 视口，修复 Retina 和页面缩放环境下的点击
  偏移；视口快照同时返回截图宽高，调用方仍可显式选择 CSS viewport 坐标。
- 元素引用点击会在滚动后重新计算可见区域并检查遮挡；无法命中的目标直接返回诊断错误，
  不再把未产生页面效果的合成事件误报为成功。

## [0.11.0] - 2026-08-20

### 新增

- 新增 `bilibili_search` 与 `bilibili_video`，通过当前 Chrome 会话搜索 B 站视频并读取
  BV/AV 标识、内容 meta、作者、标签、互动统计及分 P 信息。
- 新增 `bilibili_download_video` 与 `bilibili_download_audio`，支持指定 `?p=N` 下载分 P
  视频，或只保存最佳兼容音轨。
- 新增独立 `bilibili.fetch` 扩展协议和 `sites/bilibili*.py` adapter，不把 B 站 API、
  DASH 选择或 FFmpeg 编排混入其他平台模块。

### 改进

- B 站 DASH 下载优先最高可用画质和 AVC 兼容轨；检测到 FFmpeg 时使用 stream copy 无损
  合并画面与音频，未安装 FFmpeg 时明确返回两个独立轨道。
- B 站公开搜索 API 遇到 HTTP 412 风控时自动回退到真实搜索结果页，并对重复渲染卡片按
  BV 标识去重。
- 共享媒体下载器增加 B 站 CDN、Referer/Origin、音频 MP4 容器和 `.m4a` 扩展名兼容，
  保留逐跳 URL 校验、大小限制、SHA-256 与原子发布。
- MCP 服务新增 owner watchdog：自动跳过 `uv` wrapper 监控真实宿主，宿主异常退出且 stdin
  未正常关闭时仍能终止服务并释放 bridge 端口。
- 正常关闭会先发送 `bridge.shutdown`；扩展在显式关闭或 WebSocket 断开后按端口清理隔离
  交互窗口、队列和 debugger，不关闭用户原有标签页。

### 验证

- 已用真实扩展会话完成搜索、meta、视频和纯音频下载；测试 MP4 经 `ffprobe` 确认包含
  H.264 + AAC，纯音频 M4A 只包含 AAC。

## [0.10.0] - 2026-08-19

### 新增

- 新增 `xhs_like`、`xhs_collect`、`douyin_like`、`douyin_collect` 四个窄工具，支持通过
  `enabled` 设置点赞/收藏的期望状态。
- 新增站点专用 `xhs.mutate` 与 `douyin.mutate` 扩展协议，写操作不进入通用读取命名空间。

### 改进

- 点赞与收藏先读取当前页面状态，仅在不一致时发送一次可信点击；重复调用同一状态为 no-op。
- 点击后只轮询验证最终状态，验证失败不会重试点击，避免页面延迟导致反向切换。

### 安全性

- 四个变更型工具均声明非只读、可撤销型 destructive 和幂等风险标记，并在工具说明中要求
  MCP 客户端在调用前立即取得用户明确确认。
- 小红书只定位详情底栏的语义图标，抖音只定位作品级 `data-e2e` 控件，避免误点评论点赞。
- 真实媒体 smoke test 改为运行时搜索并获取有效 `xsec_token`，不再把临时访问参数写入源码，
  同时在控制台摘要中对该参数脱敏。

## [0.9.0] - 2026-08-19

### 新增

- 新增 `douyin_search`、`douyin_video`、`douyin_comments`，通过当前 Chrome 登录态完成
  抖音搜索、视频/图文详情读取，以及主评论和展开回复采集。
- 新增 `xhs_download`、`douyin_download`，支持按 `images`、`video` 或 `all` 下载媒体，
  可指定绝对输出目录、覆盖策略和单文件大小上限。
- `site_login_status` 新增抖音登录状态检查。
- 新增抖音独立页面注入与桥接脚本，避免平台实现污染通用页面桥接。

### 改进

- 抖音评论采集滚动作评论流而不是页面 `body`，并支持展开回复及 root/reply 分页完成判定。
- 抖音图文详情在页面未请求 `/aweme/detail/` 时，从服务端渲染的 React Flight 状态读取
  作品与图片信息。
- 小红书视频解析兼容 `EF4`、`EF5`、`EF6` 等不透明流分组以及 snake/camel URL 字段。
- 下载结果返回最终路径、字节数、Content-Type 和 SHA-256；默认避免覆盖同名文件。

### 安全性

- 平台请求继续由页面自身生成签名，Browser MCP 不复制签名算法，也不返回或持久化 Cookie。
- 媒体下载限制为平台 CDN，并对每次重定向执行公共地址校验，拒绝 HTML/JSON 伪媒体响应。
- 下载采用流式大小限制、同目录 `.part` 临时文件和原子落盘，失败时不发布不完整文件。
