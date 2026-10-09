import test from "node:test";
import assert from "node:assert/strict";
import worker from "./index.js";

const env = { GITHUB_TOKEN: "test-worker-token", GITHUB_OWNER: "thq981230", GITHUB_REPO: "yunshijie-tv-config",
  GITHUB_WORKFLOW_FILE: "source-health.yml", GITHUB_REF: "main",
  RAW_CONFIG_BASE_URL: "https://raw.githubusercontent.com/thq981230/yunshijie-tv-config/main/" };

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
}

test("health reports configuration state without exposing GitHub credentials", async () => {
  const configured = await worker.fetch(new Request("https://worker.test/health"), env);
  assert.equal(configured.status, 200);
  assert.deepEqual(await configured.json(), {
    status: "ok", service: "yunshijie-tv-refresh-api", githubConfigured: true
  });

  const unconfigured = await worker.fetch(new Request("https://worker.test/health"), { ...env, GITHUB_TOKEN: undefined });
  const payload = await unconfigured.json();
  assert.equal(unconfigured.status, 200);
  assert.deepEqual(payload, {
    status: "ok", service: "yunshijie-tv-refresh-api", githubConfigured: false
  });
  assert.equal(JSON.stringify(payload).includes(env.GITHUB_TOKEN), false);
});

test("refresh dispatch returns the actual Actions run id and keeps token server side", async () => {
  const originalFetch = globalThis.fetch;
  let dispatched;
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    if (url.includes("catalog/channels.json")) return jsonResponse({ channels: [{ id: "cctv1" }], categories: [] });
    dispatched = { url, init };
    return jsonResponse({ workflow_run_id: 918273, run_url: "https://api.github.com/run/918273" }, 200);
  };
  try {
    const response = await worker.fetch(new Request("https://worker.test/api/v1/channels/refresh", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ deviceId: "12345678-1234-1234-1234-123456789abc", appVersion: "0.4.0", refreshScope: "CHANNEL", channelId: "cctv1" })
    }), env);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { jobId: "918273", status: "STARTED" });
    assert.equal(dispatched.init.headers.authorization, "Bearer test-worker-token");
    const body = JSON.parse(dispatched.init.body);
    assert.equal(body.ref, "main");
    assert.equal(body.inputs.refresh_scope, "CHANNEL");
    assert.equal(body.inputs.channel_id, "cctv1");
    assert.equal(body.return_run_details, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("refresh rejects arbitrary channel ids before dispatch", async () => {
  const originalFetch = globalThis.fetch;
  let dispatchCalled = false;
  globalThis.fetch = async input => {
    if (String(input).includes("catalog/channels.json")) return jsonResponse({ channels: [{ id: "cctv1" }], categories: [] });
    dispatchCalled = true;
    return jsonResponse({ workflow_run_id: 1 });
  };
  try {
    const response = await worker.fetch(new Request("https://worker.test/api/v1/channels/refresh", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ deviceId: "12345678-1234-1234-1234-123456789abc", appVersion: "0.4.0", refreshScope: "CHANNEL", channelId: "unknown" })
    }), env);
    assert.equal(response.status, 400);
    assert.equal(dispatchCalled, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("status reflects the terminal workflow result and computes source counts", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async input => {
    const url = String(input);
    if (url.includes("/actions/runs/123")) return jsonResponse({ status: "completed", conclusion: "success", path: ".github/workflows/source-health.yml" });
    if (url.endsWith("/public/manifest.json")) return jsonResponse({ sourceVersion: 4 });
    if (url.endsWith("/public/sources.json")) return jsonResponse({ version: 4, channels: {
      cctv1: { status: "AVAILABLE", sources: [{ id: "s1", health: "HEALTHY" }] },
      cctv2: { status: "OFFLINE", sources: [{ id: "s2", health: "OFFLINE" }] },
      cctv3: { status: "NO_SOURCE", sources: [] }
    } });
    if (url.endsWith("/releases/sources-3.json")) return jsonResponse({ channels: { cctv1: { sources: [{ id: "s1", health: "OFFLINE" }] } } });
    throw new Error(`Unexpected request: ${url}`);
  };
  try {
    const response = await worker.fetch(new Request("https://worker.test/api/v1/channels/refresh/status?jobId=123"), env);
    const value = await response.json();
    assert.equal(value.status, "SUCCESS");
    assert.equal(value.sourceVersion, 4);
    assert.deepEqual(value.summary, { totalChannels: 3, availableChannels: 1, newSources: 1, recoveredSources: 1, offlineSources: 1, noSourceChannels: 1 });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("status maps completed workflow steps to the current Chinese stage", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async input => {
    const url = String(input);
    if (url.includes("/jobs?per_page=100")) return jsonResponse({ jobs: [{ steps: [
      { name: "Validate source catalog and run unit tests", status: "completed", conclusion: "success" },
      { name: "Stage the stable channel catalog", status: "in_progress", conclusion: null }
    ] }] });
    if (url.includes("/actions/runs/456")) return jsonResponse({ status: "in_progress", path: ".github/workflows/source-health.yml" });
    throw new Error(`Unexpected request: ${url}`);
  };
  try {
    const response = await worker.fetch(new Request("https://worker.test/api/v1/channels/refresh/status?jobId=456"), env);
    const value = await response.json();
    assert.equal(value.status, "RUNNING");
    assert.equal(value.progress, 8);
    assert.equal(value.stage, "正在准备频道目录");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("playback resolver returns the best published healthy source", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async input => {
    assert.match(String(input), /public\/sources\.json$/);
    return jsonResponse({ channels: { cctv1: { status: "AVAILABLE", sources: [
      { id: "backup", channelId: "cctv1", type: "STATIC", protocol: "HLS", url: "https://cdn-b.example/live.m3u8", health: "HEALTHY", priority: 2 },
      { id: "primary", channelId: "cctv1", type: "STATIC", protocol: "HLS", url: "https://cdn-a.example/live.m3u8", health: "HEALTHY", priority: 1 }
    ] } } });
  };
  try {
    const response = await worker.fetch(new Request("https://worker.test/api/v1/playback/resolve?channelId=cctv1"), env);
    const source = await response.json();
    assert.equal(response.status, 200);
    assert.equal(source.sourceId, "primary");
    assert.equal(source.protocol, "HLS");
    assert.equal(source.backupAvailable, true);
    assert.equal(source.availableSourceCount, 2);
    assert.equal(source.url, "https://cdn-a.example/live.m3u8");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("playback resolver can skip a failed source and reports empty production channels", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => jsonResponse({ channels: {
    cctv1: { status: "AVAILABLE", sources: [
      { id: "a", type: "STATIC", protocol: "HLS", url: "https://cdn-a.example/live.m3u8", health: "HEALTHY", priority: 1 },
      { id: "b", type: "STATIC", protocol: "HLS", url: "https://cdn-b.example/live.m3u8", health: "HEALTHY", priority: 2 }
    ] },
    cctv2: { status: "NO_SOURCE", sources: [] }
  } });
  try {
    const failover = await worker.fetch(new Request("https://worker.test/api/v1/playback/resolve?channelId=cctv1&excludeSourceIds=a"), env);
    assert.equal((await failover.json()).sourceId, "b");
    const noSource = await worker.fetch(new Request("https://worker.test/api/v1/playback/resolve?channelId=cctv2"), env);
    assert.equal(noSource.status, 404);
    assert.deepEqual(await noSource.json(), { error: "NO_SOURCE", channelId: "cctv2" });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("playback resolver refuses unsafe or expiring stream URLs", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => jsonResponse({ channels: { cctv1: { status: "AVAILABLE", sources: [
    { id: "private", type: "STATIC", protocol: "HLS", url: "http://192.168.1.1/live.m3u8", health: "HEALTHY", priority: 1 },
    { id: "token", type: "STATIC", protocol: "HLS", url: "https://cdn-a.example/live.m3u8?token=secret", health: "HEALTHY", priority: 2 }
  ] } } });
  try {
    const response = await worker.fetch(new Request("https://worker.test/api/v1/playback/resolve?channelId=cctv1"), env);
    assert.equal(response.status, 404);
    assert.equal((await response.json()).error, "NO_SOURCE");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

