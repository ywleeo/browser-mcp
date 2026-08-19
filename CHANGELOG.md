# 变更日志

本项目按[语义化版本](https://semver.org/lang/zh-CN/)维护版本号。

## [Unreleased]

暂无。

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
