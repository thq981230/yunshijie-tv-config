const API_ROOT = "https://api.github.com";
const API_VERSION = "2026-03-10";
const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };

const STAGES = [
  ["Validate source catalog", 8, "正在校验频道和候选线路"],
  ["Stage the stable channel catalog", 18, "正在准备频道目录"],
  ["Probe manifests and first media segments", 48, "正在检测直播线路"],
  ["Promote healthy sources with mass-failure protection", 70, "正在选择主备线路"],
  ["Build version manifest and validate final files", 88, "正在生成最新频道配置"],
  ["Commit health state and published configuration", 96, "正在发布频道配置"]
];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders() });

    try {
      if (url.pathname === "/api/v1/health" && request.method === "GET") {
        return json({ status: "OK", refreshReady: Boolean(env.GITHUB_TOKEN) }, 200);
      }
      if (url.pathname === "/api/v1/channels/refresh" && request.method === "POST") {
        return await startRefresh(request, env);
      }
      if (url.pathname === "/api/v1/channels/refresh/status" && request.method === "GET") {
        return await refreshStatus(request, url, env);
      }
      if (url.pathname === "/api/v1/playback/resolve" && request.method === "GET") {
        const channelId = url.searchParams.get("channelId") ?? "";
        if (!/^[a-z0-9][a-z0-9_-]{0,63}$/i.test(channelId)) return json({ error: "INVALID_CHANNEL_ID" }, 400);
        return json({ error: "NO_AUTHORIZED_DYNAMIC_PROVIDER", channelId }, 404);
      }
      return json({ error: "NOT_FOUND" }, 404);
    } catch (error) {
      console.error("refresh worker request failed", error instanceof Error ? error.name : "UnknownError");
      return json({ error: "SERVICE_UNAVAILABLE", message: "频道更新服务暂时不可用" }, 503);
    }
  }
};

async function startRefresh(request, env) {
  if (!env.GITHUB_TOKEN) return json({ error: "REFRESH_NOT_CONFIGURED" }, 503);
  if (env.REFRESH_LIMITER) {
    const clientKey = request.headers.get("cf-connecting-ip") ?? "unknown-client";
    const { success } = await env.REFRESH_LIMITER.limit({ key: clientKey });
    if (!success) return json({ error: "RATE_LIMITED", message: "请稍后再试" }, 429);
  }
  const declaredLength = Number(request.headers.get("content-length") ?? "0");
  if (declaredLength > 2048) return json({ error: "REQUEST_TOO_LARGE" }, 413);
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body)) return json({ error: "INVALID_JSON" }, 400);

  const deviceId = String(body.deviceId ?? "");
  const appVersion = String(body.appVersion ?? "");
  const refreshScope = String(body.refreshScope ?? "ALL").toUpperCase();
  const channelId = body.channelId == null ? "" : String(body.channelId);
  const categoryId = body.categoryId == null ? "" : String(body.categoryId);
  if (!/^[a-z0-9-]{16,64}$/i.test(deviceId) || !/^[0-9A-Za-z.+_-]{1,32}$/.test(appVersion)) {
    return json({ error: "INVALID_CLIENT_METADATA" }, 400);
  }
  if (!["ALL", "CHANNEL", "CATEGORY"].includes(refreshScope)) return json({ error: "INVALID_REFRESH_SCOPE" }, 400);

  const inputs = { refresh_id: crypto.randomUUID(), refresh_scope: refreshScope, channel_id: "", category_id: "" };
  if (refreshScope !== "ALL") {
    const catalog = await fetchRawJson(env, "catalog/channels.json");
    if (!catalog || !Array.isArray(catalog.channels)) return json({ error: "CATALOG_UNAVAILABLE" }, 503);
    if (refreshScope === "CHANNEL") {
      if (!/^[a-z0-9][a-z0-9_-]{0,63}$/i.test(channelId) || !catalog.channels.some(row => row.id === channelId)) {
        return json({ error: "UNKNOWN_CHANNEL" }, 400);
      }
      inputs.channel_id = channelId;
    } else {
      const categories = Array.isArray(catalog.categories) ? catalog.categories : [];
      if (!/^[a-z0-9][a-z0-9_-]{0,63}$/i.test(categoryId) || !categories.some(row => row.id === categoryId)) {
        return json({ error: "UNKNOWN_CATEGORY" }, 400);
      }
      inputs.category_id = categoryId;
    }
  }

  const response = await githubFetch(env,
    `/repos/${encodeURIComponent(env.GITHUB_OWNER)}/${encodeURIComponent(env.GITHUB_REPO)}/actions/workflows/${encodeURIComponent(env.GITHUB_WORKFLOW_FILE)}/dispatches`,
    { method: "POST", body: JSON.stringify({ ref: env.GITHUB_REF || "main", inputs, return_run_details: true }) });
  if (!response.ok) {
    console.error("GitHub workflow dispatch rejected", response.status);
    return json({ error: response.status === 401 || response.status === 403 ? "GITHUB_AUTHORIZATION_FAILED" : "WORKFLOW_DISPATCH_FAILED" }, 502);
  }
  const run = await response.json().catch(() => null);
  if (!run?.workflow_run_id) return json({ error: "WORKFLOW_RUN_ID_MISSING" }, 502);
  return json({ jobId: String(run.workflow_run_id), status: "STARTED" }, 200);
}

async function refreshStatus(request, url, env) {
  if (!env.GITHUB_TOKEN) return json({ error: "REFRESH_NOT_CONFIGURED" }, 503);
  if (env.STATUS_LIMITER) {
    const clientKey = request.headers.get("cf-connecting-ip") ?? "unknown-client";
    const { success } = await env.STATUS_LIMITER.limit({ key: clientKey });
    if (!success) return json({ error: "RATE_LIMITED", message: "请稍后再查看更新状态" }, 429);
  }
  const jobId = url.searchParams.get("jobId") ?? "";
  if (!/^\d{1,20}$/.test(jobId)) return json({ error: "INVALID_JOB_ID" }, 400);
  const repo = `/repos/${encodeURIComponent(env.GITHUB_OWNER)}/${encodeURIComponent(env.GITHUB_REPO)}`;
  const runResponse = await githubFetch(env, `${repo}/actions/runs/${jobId}`);
  if (runResponse.status === 404) return json({ error: "JOB_NOT_FOUND" }, 404);
  if (!runResponse.ok) return json({ error: "GITHUB_STATUS_UNAVAILABLE" }, 502);
  const run = await runResponse.json();
  if (run.path && run.path !== `.github/workflows/${env.GITHUB_WORKFLOW_FILE}`) return json({ error: "JOB_NOT_FOUND" }, 404);

  if (run.status === "completed") {
    if (run.conclusion !== "success") {
      return json({ jobId, status: "FAILED", progress: 100, stage: "频道探测或配置发布失败",
        message: "刷新失败，继续使用上一次可用配置" }, 200);
    }
    const [manifest, sources] = await Promise.all([
      fetchRawJson(env, "public/manifest.json"),
      fetchRawJson(env, "public/sources.json")
    ]);
    return json({ jobId, status: "SUCCESS", progress: 100, stage: "频道更新完成",
      sourceVersion: Number(manifest?.sourceVersion) || null,
      summary: await summarizeSources(env, sources), message: null }, 200);
  }

  const jobsResponse = await githubFetch(env, `${repo}/actions/runs/${jobId}/jobs?per_page=100`);
  const jobsPayload = jobsResponse.ok ? await jobsResponse.json().catch(() => ({})) : {};
  const completedNames = (jobsPayload.jobs ?? []).flatMap(job => (job.steps ?? [])
    .filter(step => step.status === "completed" && step.conclusion === "success")
    .map(step => step.name));
  const completedCount = STAGES.filter(([name]) => completedNames.some(completed => completed.includes(name))).length;
  const current = STAGES.find(([name]) => !completedNames.some(completed => completed.includes(name)));
  const running = (jobsPayload.jobs ?? []).flatMap(job => job.steps ?? []).find(step => step.status === "in_progress");
  return json({ jobId, status: "RUNNING", progress: Math.min(96, Math.max(2, completedCount ? STAGES[completedCount - 1][1] : 2)),
    stage: running ? localStage(running.name) : (current?.[2] ?? "等待频道检查"), sourceVersion: null, summary: null }, 200);
}

function localStage(name) {
  return STAGES.find(([step]) => name.includes(step))?.[2] ?? "正在运行频道检查";
}

async function summarizeSources(env, current) {
  const channels = current?.channels && typeof current.channels === "object" ? current.channels : {};
  const entries = Object.values(channels);
  const sources = entries.flatMap(channel => Array.isArray(channel.sources) ? channel.sources : []);
  const summary = {
    totalChannels: entries.length,
    availableChannels: entries.filter(channel => channel.status === "AVAILABLE").length,
    newSources: 0,
    recoveredSources: 0,
    offlineSources: sources.filter(source => ["OFFLINE", "EXPIRED"].includes(source.health)).length,
    noSourceChannels: entries.filter(channel => channel.status === "NO_SOURCE").length
  };
  const version = Number(current?.version) || 0;
  if (version > 1) {
    const previous = await fetchRawJson(env, `releases/sources-${version - 1}.json`);
    if (previous?.channels) {
      const previousSources = Object.values(previous.channels).flatMap(channel => channel.sources ?? []);
      const oldById = new Map(previousSources.map(source => [source.id, source]));
      summary.newSources = sources.filter(source => !oldById.has(source.id)).length;
      summary.recoveredSources = sources.filter(source => source.health === "HEALTHY" &&
        oldById.has(source.id) && oldById.get(source.id).health !== "HEALTHY").length;
    }
  }
  return summary;
}

async function fetchRawJson(env, path) {
  const base = String(env.RAW_CONFIG_BASE_URL ?? "");
  if (!base.startsWith("https://raw.githubusercontent.com/") || !base.endsWith("/")) return null;
  const response = await fetch(new URL(path.split("/").map(encodeURIComponent).join("/"), base), {
    headers: { "cache-control": "no-cache", "user-agent": "YunshijieTV-RefreshWorker/1.0" },
    signal: AbortSignal.timeout(8000)
  });
  if (!response.ok) return null;
  return response.json().catch(() => null);
}

async function githubFetch(env, path, init = {}) {
  const url = path.startsWith("https://") ? path : `${API_ROOT}${path}`;
  return fetch(url, {
    ...init,
    headers: {
      "accept": "application/vnd.github+json",
      "authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "x-github-api-version": API_VERSION,
      "user-agent": "YunshijieTV-RefreshWorker/1.0",
      ...(init.headers ?? {}),
      ...(init.body ? { "content-type": "application/json" } : {})
    },
    signal: AbortSignal.timeout(10000)
  });
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { ...JSON_HEADERS, ...corsHeaders() } });
}

function corsHeaders() {
  return { "access-control-allow-origin": "*", "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type", "access-control-max-age": "86400" };
}
