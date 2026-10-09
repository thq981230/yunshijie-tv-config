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
- `.github/workflows/source-health.yml`：每 6 小时自动探活，也支持手动执行。

设置 GitHub Actions workflow 权限 `contents: write`。应用构建时将 `tvGithubManifestUrl` 指向仓库公开 Raw 或 Pages 的 `public/manifest.json`。Manifest URL 一次性配置后，改线路只需更新 GitHub 仓库，不用更新 APK。

## 健康状态和提升

- 成功请求到 HLS manifest 和至少一个首个媒体分片：`HEALTHY`。
- 第一次失败增加 `failCount` 并暂存为降级备用；第二次连续失败标为 `DEGRADED`；连续三次失败为 `OFFLINE`。
- 成功后重置连续失败次数并恢复 `HEALTHY`。
- `public/sources.json` 按健康状态、priority、延迟、画质排列线路。单线路短时错误不会删候选；现场播放仍由 APK 在本次会话内逐条 failover。
