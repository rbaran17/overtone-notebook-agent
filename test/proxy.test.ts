import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";
import { getRewrittenUrl, isRewrite, unstable_doesMiddlewareMatch } from "next/experimental/testing/server";
import { NextRequest } from "next/server";
import { config, proxy } from "../src/proxy";

const ENV_KEYS = ["OVERTONE_API_URL", "OVERTONE_API_KEY", "OVERTONE_UI_ALLOWED_HOSTS"] as const;
const saved = Object.fromEntries(ENV_KEYS.map((key) => [key, process.env[key]]));

function setEnv(values: Partial<Record<(typeof ENV_KEYS)[number], string>>) {
  for (const key of ENV_KEYS) {
    if (values[key] === undefined) delete process.env[key];
    else process.env[key] = values[key];
  }
}

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
});

/** A request as the server sees it: the URL carries the listen address, `Host` what the client asked for. */
function apiRequest(path: string, init: { method?: string; headers?: Record<string, string> } = {}): NextRequest {
  return new NextRequest(`http://127.0.0.1:3000${path}`, {
    method: init.method ?? "GET",
    headers: { host: "localhost:3000", ...init.headers },
  });
}

// How Next.js carries "send these request headers upstream" from a proxy to its router.
// These names are Next.js internals. If an upgrade changes them, check against a running
// server (curl a stub engine through /api and look at what it received) before adjusting.
function forwarded(response: Response): { names: string[]; get: (name: string) => string | null } {
  const names = (response.headers.get("x-middleware-override-headers") ?? "").split(",").filter(Boolean);
  return { names, get: (name) => response.headers.get(`x-middleware-request-${name}`) };
}

describe("proxy", () => {
  test("runs for /api and nothing else", () => {
    for (const url of ["/api", "/api/health", "/api/notebooks/n1/chat", "/api/artifacts/a1/audio?download=1"]) {
      assert.equal(unstable_doesMiddlewareMatch({ config, url }), true, url);
    }
    for (const url of ["/", "/notebook/n1", "/apix/health", "/_next/static/chunk.js", "/favicon.ico"]) {
      assert.equal(unstable_doesMiddlewareMatch({ config, url }), false, url);
    }
  });

  test("forwards /api/x?y to the engine, default address", () => {
    setEnv({});
    const response = proxy(apiRequest("/api/notebooks/n1/guide?refresh=1"));
    assert.equal(isRewrite(response), true);
    assert.equal(getRewrittenUrl(response), "http://127.0.0.1:8787/notebooks/n1/guide?refresh=1");
    assert.equal(forwarded(response).names.includes("authorization"), false);
  });

  test("honours OVERTONE_API_URL", () => {
    setEnv({ OVERTONE_API_URL: "http://engine.internal:9000/" });
    assert.equal(getRewrittenUrl(proxy(apiRequest("/api/health"))), "http://engine.internal:9000/health");
  });

  test("attaches OVERTONE_API_KEY in place of the client's Authorization, and only upstream", () => {
    setEnv({ OVERTONE_API_KEY: "server-key" });
    const response = proxy(apiRequest("/api/notebooks", { headers: { authorization: "Bearer from-the-browser" } }));
    assert.equal(isRewrite(response), true);
    assert.equal(forwarded(response).get("authorization"), "Bearer server-key");
    // Nothing the browser could read mentions the key.
    assert.equal(response.headers.get("authorization"), null);
    assert.doesNotMatch(getRewrittenUrl(response) ?? "", /server-key/);
  });

  test("does not forward Origin, Referer or Cookie", () => {
    setEnv({});
    const response = proxy(
      apiRequest("/api/notebooks", {
        method: "POST",
        headers: {
          origin: "http://localhost:3000",
          referer: "http://localhost:3000/",
          cookie: "other_app=1",
          "content-type": "application/json",
        },
      }),
    );
    assert.equal(isRewrite(response), true);
    const { names } = forwarded(response);
    assert.ok(names.includes("content-type"), names.join(","));
    for (const name of ["origin", "referer", "cookie"]) assert.equal(names.includes(name), false, name);
  });

  test("refuses a DNS-rebinding Host with 403 forbidden_host and forwards nothing", async () => {
    setEnv({ OVERTONE_API_KEY: "server-key" });
    const response = proxy(apiRequest("/api/notebooks", { headers: { host: "attacker.example:3000" } }));
    assert.equal(response.status, 403);
    assert.equal(isRewrite(response), false);
    const body = (await response.json()) as { error: { code: string; message: string } };
    assert.equal(body.error.code, "forbidden_host");
    assert.equal(typeof body.error.message, "string");
    assert.equal(forwarded(response).names.length, 0);
  });

  test("OVERTONE_UI_ALLOWED_HOSTS lets a named host through", () => {
    setEnv({ OVERTONE_UI_ALLOWED_HOSTS: "notes.example, studio.lan" });
    assert.equal(isRewrite(proxy(apiRequest("/api/health", { headers: { host: "notes.example:3000" } }))), true);
    assert.equal(proxy(apiRequest("/api/health", { headers: { host: "other.example:3000" } })).status, 403);
  });

  test("refuses requests from other origins with 403 forbidden_origin", async () => {
    setEnv({});
    const cases: { method: string; headers: Record<string, string> }[] = [
      { method: "POST", headers: { origin: "https://evil.example" } },
      { method: "DELETE", headers: { origin: "null" } },
      { method: "PATCH", headers: { origin: "http://localhost:5173" } },
      { method: "GET", headers: { "sec-fetch-site": "cross-site" } },
      { method: "OPTIONS", headers: { origin: "https://evil.example", "sec-fetch-site": "cross-site" } },
    ];
    for (const init of cases) {
      const response = proxy(apiRequest("/api/notebooks", init));
      assert.equal(response.status, 403, JSON.stringify(init));
      const body = (await response.json()) as { error: { code: string } };
      assert.equal(body.error.code, "forbidden_origin", JSON.stringify(init));
    }
  });

  test("lets the UI's own writes through", () => {
    setEnv({});
    const response = proxy(
      apiRequest("/api/notebooks", {
        method: "POST",
        headers: { origin: "http://localhost:3000", "sec-fetch-site": "same-origin" },
      }),
    );
    assert.equal(isRewrite(response), true);
    assert.equal(getRewrittenUrl(response), "http://127.0.0.1:8787/notebooks");
  });

  test("a broken OVERTONE_API_URL is a 502, not a forward somewhere else", async () => {
    setEnv({ OVERTONE_API_URL: "not a url" });
    const response = proxy(apiRequest("/api/health"));
    assert.equal(response.status, 502);
    assert.equal(isRewrite(response), false);
    assert.equal(((await response.json()) as { error: { code: string } }).error.code, "bad_gateway");
  });
});
