# Slow Android

这是 Slow 的独立 Android 客户端目录。服务端仍由 `apps/api` 提供，浏览器客户端仍位于 `apps/web`。

## 当前体验版

`0.1.x` 是仅供内部安装的在线体验壳，Android WebView 直接加载 `https://slow.net.cn`：

- 与现有网页登录保持同源，沿用当前 Session Cookie 与 CSRF 保护。
- APK 不包含 API Key、数据库配置或任何服务端密钥。
- 必须联网使用；线上前端更新后，客户端会立即使用新版本。
- 该模式不作为应用商店正式发布架构。

## 后续正式客户端边界

正式版本应移除 `server.url`，把前端静态资源打包进 APK，并通过独立环境配置访问 HTTPS API。届时需要为移动端明确认证、深链、升级、离线状态和客户端版本兼容策略，不能依赖 WebView 内的线上页面作为长期发布方案。

## 本地构建

```bash
pnpm install
pnpm android:add
pnpm android:debug
```

调试 APK 默认生成在 `android/app/build/outputs/apk/debug/app-debug.apk`。
