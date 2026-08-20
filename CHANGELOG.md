# 变更日志

本项目按[语义化版本](https://semver.org/lang/zh-CN/)维护版本号。

## [Unreleased]

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
