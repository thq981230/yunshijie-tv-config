const API_ROOT = "https://api.github.com";
const API_VERSION = "2026-03-10";
const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };
const GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com";
const GITHUB_OIDC_JWKS_URL = `${GITHUB_OIDC_ISSUER}/.well-known/jwks`;
const SUBSCRIPTION_OIDC_AUDIENCE = "api://yunshijie-tv-subscriptions";
const ALLOWED_GITHUB_REPOSITORY = "thq981230/yunshijie-tv-config";
const ALLOWED_GITHUB_REPOSITORY_ID = "1411311167";
const ALLOWED_GITHUB_OWNER_ID = "95538235";
const ALLOWED_GITHUB_WORKFLOW = `${ALLOWED_GITHUB_REPOSITORY}/.github/workflows/source-health.yml@refs/heads/main`;
let cachedGithubJwks = null;
let githubJwksCachedAt = 0;

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
      if (url.pathname === "/health" && request.method === "GET") {
        return json({
          status: "ok",
          service: "yunshijie-tv-refresh-api",
          githubConfigured: Boolean(env.GITHUB_TOKEN)
        }, 200);
      }
      if (url.pathname === "/internal/v1/subscriptions" && request.method === "GET") {
        return await subscriptionFeedConfig(request, env);
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
        return await resolvePublishedSource(url, channelId, env);
      }
      return json({ error: "NOT_FOUND" }, 404);
    } catch (error) {
      console.error("refresh worker request failed", error instanceof Error ? error.name : "UnknownError");
      return json({ error: "SERVICE_UNAVAILABLE", message: "频道更新服务暂时不可用" }, 503);
    }
  }
};

async function subscriptionFeedConfig(request, env) {
  const authorization = request.headers.get("authorization") ?? "";
  const token = authorization.match(/^Bearer\s+(.+)$/i)?.[1] ?? "";
  if (!token || token.length > 32768) return json({ error: "OIDC_TOKEN_REQUIRED" }, 401);
  const rejection = await verifyGithubActionsOidcToken(token);
  if (rejection) return json({ error: "OIDC_IDENTITY_REJECTED", reason: rejection }, 403);
  const raw = String(env.YUNSHIJIE_SUBSCRIPTION_FEEDS_JSON ?? "");
  if (!raw.trim()) return json({ configured: false, feeds: [] }, 200);
  if (raw.length > 65536) return json({ error: "SUBSCRIPTION_CONFIG_TOO_LARGE" }, 503);
  let feeds;
  try {
    feeds = JSON.parse(raw);
  } catch {
    return json({ error: "SUBSCRIPTION_CONFIG_INVALID" }, 503);
  }
  if (!Array.isArray(feeds) || feeds.length > 20 || feeds.some(feed => !feed || typeof feed !== "object" || Array.isArray(feed))) {
    return json({ error: "SUBSCRIPTION_CONFIG_INVALID" }, 503);
  }
  return json({ configured: true, feeds }, 200);
}

async function verifyGithubActionsOidcToken(token) {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return "TOKEN_FORMAT";
    const header = decodeJwtPart(parts[0]);
    if (header.alg !== "RS256" || typeof header.kid !== "string") return "TOKEN_HEADER";
    const claims = decodeJwtPart(parts[1]);
    const now = Math.floor(Date.now() / 1000);
    const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (claims.iss !== GITHUB_OIDC_ISSUER) return "CLAIM_ISSUER";
    if (!audiences.includes(SUBSCRIPTION_OIDC_AUDIENCE)) return "CLAIM_AUDIENCE";
    if (claims.repository !== ALLOWED_GITHUB_REPOSITORY) return "CLAIM_REPOSITORY";
    if (claims.ref !== "refs/heads/main") return "CLAIM_REF";
    if (String(claims.repository_id) !== ALLOWED_GITHUB_REPOSITORY_ID) return "CLAIM_REPOSITORY_ID";
    if (String(claims.repository_owner_id) !== ALLOWED_GITHUB_OWNER_ID) return "CLAIM_OWNER_ID";
    if (claims.workflow_ref !== ALLOWED_GITHUB_WORKFLOW) return "CLAIM_WORKFLOW_REF";
    if (!["push", "workflow_dispatch", "schedule"].includes(claims.event_name)) return "CLAIM_EVENT";
    if (!Number.isFinite(claims.exp) || claims.exp <= now ||
      (Number.isFinite(claims.nbf) && claims.nbf > now + 60) ||
      (Number.isFinite(claims.iat) && claims.iat > now + 60)) return "CLAIM_TIME";

    let keys = await githubJwks();
    let jwk = keys.find(key => key.kid === header.kid && key.kty === "RSA" && key.use !== "enc");
    if (!jwk && Date.now() - githubJwksCachedAt < 15 * 60 * 1000) {
      keys = await githubJwks(true);
      jwk = keys.find(key => key.kid === header.kid && key.kty === "RSA" && key.use !== "enc");
    }
    if (!jwk) return "SIGNING_KEY_NOT_FOUND";
    const key = await crypto.subtle.importKey("jwk", jwk,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    const data = new TextEncoder().encode(`${parts[0]}.${parts[1]}`);
    return await crypto.subtle.verify({ name: "RSASSA-PKCS1-v1_5" }, key, decodeBase64Url(parts[2]), data)
      ? null : "SIGNATURE_INVALID";
  } catch {
    return "TOKEN_INVALID";
  }
}

async function githubJwks(force = false) {
  if (!force && cachedGithubJwks && Date.now() - githubJwksCachedAt < 15 * 60 * 1000) return cachedGithubJwks;
  const response = await fetch(GITHUB_OIDC_JWKS_URL, {
    headers: { "accept": "application/json", "user-agent": "YunshijieTV-RefreshWorker/1.0" },
    signal: AbortSignal.timeout(8000)
  });
  if (!response.ok) throw new Error("GitHub OIDC keys unavailable");
  const payload = await response.json();
  if (!Array.isArray(payload.keys)) throw new Error("GitHub OIDC key set invalid");
  cachedGithubJwks = payload.keys;
  githubJwksCachedAt = Date.now();
  return cachedGithubJwks;
}

function decodeJwtPart(value) {
  return JSON.parse(new TextDecoder().decode(decodeBase64Url(value)));
}

function decodeBase64Url(value) {
  const base64 = String(value).replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(base64);
  return Uint8Array.from(binary, character => character.charCodeAt(0));
}

async function resolvePublishedSource(url, channelId, env) {
  const published = await fetchRawJson(env, "public/sources.json");
  if (!published || !published.channels || typeof published.channels !== "object") {
    return json({ error: "SOURCE_CATALOG_UNAVAILABLE", channelId }, 503);
  }
  const channel = published.channels[channelId];
  if (!channel) return json({ error: "CHANNEL_NOT_FOUND", channelId }, 404);
  const excluded = new Set(url.searchParams.getAll("excludeSourceIds")
    .flatMap(value => value.split(",")).map(value => value.trim()).filter(Boolean));
  const candidates = (Array.isArray(channel.sources) ? channel.sources : [])
    .filter(source => source && source.enabled !== false && source.type === "STATIC" &&
      ["HLS", "DASH"].includes(String(source.protocol).toUpperCase()) &&
      ["HEALTHY", "DEGRADED"].includes(source.health) && isPublicHttpStream(source.url))
    .sort((left, right) => sourceOrder(left, right));
  if (candidates.length === 0) {
    return json({ error: channel.status === "OFFLINE" ? "ALL_SOURCES_OFFLINE" : "NO_SOURCE", channelId },
      channel.status === "OFFLINE" ? 503 : 404);
  }
  const available = candidates.filter(source => !excluded.has(source.id));
  if (available.length === 0) return json({ error: "NO_REMAINING_SOURCE", channelId }, 404);
  const source = available[0];
  return json({
    channelId,
    sourceId: source.id,
    sessionId: crypto.randomUUID(),
    protocol: String(source.protocol).toUpperCase(),
    url: source.url,
    expiresAt: Number.MAX_SAFE_INTEGER,
    headers: source.headers && typeof source.headers === "object" ? source.headers : {},
    backupAvailable: available.length > 1,
    isLocal: false,
    availableSourceCount: candidates.length
  }, 200);
}

function sourceOrder(left, right) {
  const health = value => value === "HEALTHY" ? 0 : 1;
  const quality = value => ({ "8K": 8000, "4K": 4000, "2160P": 2160, "1080P": 1080, FHD: 1080,
    "720P": 720, HD: 720, "480P": 480, "360P": 360, SD: 360 }[String(value).toUpperCase()] ?? 0);
  return health(left.health) - health(right.health) || Number(left.priority ?? 100) - Number(right.priority ?? 100) ||
    Number(left.latencyMs ?? Number.MAX_SAFE_INTEGER) - Number(right.latencyMs ?? Number.MAX_SAFE_INTEGER) ||
    quality(right.quality) - quality(left.quality) || String(left.id).localeCompare(String(right.id));
}

function isPublicHttpStream(value) {
  try {
    const parsed = new URL(String(value));
    const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
    if (!(parsed.protocol === "https:" || parsed.protocol === "http:") || parsed.username || parsed.password || !host ||
      host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local")) return false;
    if (isNonPublicIpLiteral(host)) return false;
    if (parsed.hash) return false;
    return ![...parsed.searchParams.keys()].some(isSensitiveQueryKey);
  } catch {
    return false;
  }
}

function isSensitiveQueryKey(value) {
  let decoded = String(value).replace(/\+/g, " ");
  for (let i = 0; i < 3; i += 1) {
    try {
      const next = decodeURIComponent(decoded);
      if (next === decoded) break;
      decoded = next;
    } catch {
      break;
    }
  }
  const key = decoded.normalize("NFKC").toLowerCase().replace(/[^a-z0-9]/g, "");
  const exact = new Set([
    "token", "accesstoken", "auth", "authorization", "signature", "sig", "sign", "expires", "expire",
    "key", "authkey", "txsecret", "wstime", "wssecret", "hdnts", "policy", "jwt", "secret",
    "accesskey", "credential", "credentials", "authinfo", "mac", "macaddress", "stb", "stbid",
    "device", "deviceid", "clientid", "userid", "user", "session", "sessionid", "sid", "uid",
    "serial", "serialnumber", "password", "passwd", "pwd", "cookie", "account", "clientmac"
  ]);
  const markers = ["auth", "token", "secret", "credential", "password", "passwd", "cookie", "signature",
    "accesskey", "mac", "device", "stb", "session", "serial", "user"];
  return exact.has(key) || key.startsWith("key") || key.endsWith("key") || markers.some(marker => key.includes(marker));
}

function isNonPublicIpLiteral(host) {
  const octets = host.split(".");
  if (octets.length === 4 && octets.every(part => /^\d{1,3}$/.test(part) && Number(part) <= 255)) {
    const [a, b, c] = octets.map(Number);
    return a === 0 || a === 10 || a === 127 || a >= 224 ||
      (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && (b === 168 || (b === 0 && [0, 2].includes(c)) || (b === 88 && c === 99) || (b === 0 && c === 0))) ||
      (a === 100 && b >= 64 && b <= 127) || (a === 198 && ([18, 19].includes(b) || (b === 51 && c === 100))) ||
      (a === 203 && b === 0 && c === 113) || a === 255;
  }
  if (host.includes(":")) {
    // Permit global-unicast IPv6 only; reject local, multicast, mapped, and documentation ranges.
    return !/^[23]/.test(host) || host.startsWith("2001:db8:");
  }
  return false;
}

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
