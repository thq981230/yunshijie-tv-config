# 云视界 TV 远程频道配置

独立的频道目录、直播线路候选、探活结果和 APK 远程发布文件。Android 客户端只读取 `public/manifest.json` 指向的目录与已提升线路，不读取候选库。

## Source 接入规则

- 只提交已获书面授权、官方明确允许第三方播放、或用户拥有合法权利的源。
- 不从 CCTV/卫视网页播放器提取内部 URL，不绕过 Token、Cookie、DRM、会员或地域限制。
- 第一阶段只发布 `STATIC` HTTPS HLS/DASH 地址。`DYNAMIC` 仅保留模型字段，不会写入客户端配置；动态鉴权以后需要受控 Resolver。
- 目前没有可核验的 CCTV/卫视第三方播放许可，候选 source 列表按空列表初始化；不能把演示视频或未经授权的 URL 冒充正式频道。
- 单次失败不会删掉候选；连续失败通过健康状态降级。检测任务若 80% 或以上候选同时失败，会中止发布，保留最近一次 `public/sources.json`。

获得授权后，在对应 `candidates/*.json` 追加线路并注明 `authorization` 记录（公开许可 URL、合同编号的非敏感标识或授权方说明；不要提交密钥）。运行 `python -m unittest discover -s tests`、`python scripts/validate_config.py`、`python scripts/check_sources.py`、`python scripts/promote_sources.py`、`python scripts/build_manifest.py`。

## 文件

- `catalog/channels.json`：稳定目录元数据，不存线路。
- `candidates/`：按频道分组的候选源。受 GitHub 仓库写权限保护。
- `health/latest.json`：探活、连续成功/失败次数和最后检测时间。
- `public/`：Android 可公开读取的目录、已健康/保留的备用源和版本 manifest。
- `releases/`：历次 sources 配置快照，便于回滚。
- `scripts/`：验证、HLS 探活、提升和原子发布程序。
- `scripts/providers/`：Provider Registry。当前只读取人工审查后提交的候选 JSON，不会扫描网页或猜测直播地址。
- `.github/workflows/source-health.yml`：每 6 小时自动探活，也支持手动执行。
- `worker/`：Cloudflare Worker 刷新 API；GitHub Token 只保存在 Worker Secret 中。

设置 GitHub Actions workflow 权限 `contents: write`。应用构建时将 `tvGithubManifestUrl` 指向仓库公开 Raw 或 Pages 的 `public/manifest.json`。Manifest URL 一次性配置后，改线路只需更新 GitHub 仓库，不用更新 APK。

## 健康状态和提升

- 成功请求到 HLS manifest 和至少一个首个媒体分片：`HEALTHY`。
- 第一次失败增加 `failCount` 并暂存为降级备用；第二次连续失败标为 `DEGRADED`；连续三次失败为 `OFFLINE`。
- 成功后重置连续失败次数并恢复 `HEALTHY`。
- `public/sources.json` 按健康状态、priority、延迟、画质排列线路。单线路短时错误不会删候选；现场播放仍由 APK 在本次会话内逐条 failover。

## App 一键更新与 Cloudflare Worker

Worker 实现以下接口：

- `GET /api/v1/health`
- `POST /api/v1/channels/refresh`（支持 `ALL`、`CHANNEL`、`CATEGORY`）
- `GET /api/v1/channels/refresh/status?jobId=...`
- `GET /api/v1/playback/resolve`（没有获准的动态 Provider 时明确返回未配置）

刷新接口通过 GitHub workflow dispatch 触发 `source-health.yml`，API 返回真实 Actions run ID；状态接口读取 Actions run/steps，完成后汇总公开配置。Android 收到成功状态后再使用已有 GitHub manifest 下载与 Room Last Known Good 更新路径。该流程不会把 GitHub Token 打进 APK。

部署步骤：

1. 在 Cloudflare 账户内启用 `workers.dev` 子域名。进入 `worker/` 执行 `npm install`，再执行 `npx wrangler login`、`npx wrangler whoami` 和 `npx wrangler deploy`。
2. 在 GitHub 为 `thq981230/yunshijie-tv-config` 创建 Fine-grained PAT，只授予该仓库的 **Actions: Read and write**。在 `worker/` 执行 `npx wrangler secret put GITHUB_TOKEN`，在提示符中粘贴 Token；不要将它放进聊天、Git、Gradle 属性或 APK。然后再次执行 `npx wrangler deploy`。
3. 用部署输出中的 Worker HTTPS URL 检查 `GET /api/v1/health` 的 `refreshReady` 为 `true`，然后设置 Android Gradle 属性 `tvWorkerApiBaseUrl` 为该 URL，并以末尾 `/` 结尾。例如：

   ```properties
   tvWorkerApiBaseUrl=https://yunshijie-tv-refresh-api.<workers-dev-subdomain>.workers.dev/
   ```

4. 构建 APK 后打开“源管理”并点击“一键更新频道”。Worker 限制每个客户端 IP 每分钟最多 20 次刷新请求、60 次状态查询；App 同一时间只提交一个任务。

`workers.dev` 域名和 Worker Secret 属于 Cloudflare 账户配置，无法从公开 GitHub 仓库代替账户所有者创建。未部署前，App 保留本地/Room 配置；更新失败不会清空 Last Known Good。

## 定向探活

`source-health.yml` 支持全量、单频道和频道分类刷新。Worker 会先用公开目录验证传入的频道/分类 ID。定向探活仅更新目标线路的健康记录，其他线路保留原健康数据；来源仍必须先经人工审查并写入 `candidates/`，且带有授权说明。当前仓库没有获准的正式电视台线路，因此定向刷新不会凭空发现或生成正式直播源。
