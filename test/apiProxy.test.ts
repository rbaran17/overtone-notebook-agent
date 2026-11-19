import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  backendUrl,
  checkApiRequest,
  forwardedHeaders,
  isAllowedHost,
  isIpLiteral,
  isSameOrigin,
  parseAllowedHosts,
  parseHostHeader,
  type ApiRequestInfo,
} from "../src/lib/apiProxy";

const NONE: ReadonlySet<string> = new Set();

function request(overrides: Partial<ApiRequestInfo> = {}): ApiRequestInfo {
  return { protocol: "http:", host: "localhost:3000", origin: null, secFetchSite: null, ...overrides };
}

function codeOf(overrides: Partial<ApiRequestInfo>, allowed: ReadonlySet<string> = NONE): string {
  const decision = checkApiRequest(request(overrides), allowed);
  return decision.ok ? "ok" : decision.code;
}

describe("Host header", () => {
  test("parses names, IP literals and ports", () => {
    assert.deepEqual(parseHostHeader("LocalHost:3000"), { hostname: "localhost", port: "3000" });
    assert.deepEqual(parseHostHeader("localhost."), { hostname: "localhost", port: "" });
    assert.deepEqual(parseHostHeader("127.0.0.1:3000"), { hostname: "127.0.0.1", port: "3000" });
    assert.deepEqual(parseHostHeader("[::1]:3000"), { hostname: "[::1]", port: "3000" });
    assert.deepEqual(parseHostHeader(" notes.example "), { hostname: "notes.example", port: "" });
  });

  test("rejects anything that is not host[:port]", () => {
    for (const value of [
      "",
      " ",
      "localhost:",
      "localhost:abc",
      "localhost:3000:1",
      "user@localhost",
      "attacker.example/@localhost",
      "localhost/path",
      "localhost?x",
      "localhost#x",
      "local host",
      "localhost,attacker.example",
      "a..b",
      ".localhost",
      "::1",
      "[::1",
      "http://localhost",
    ]) {
      assert.equal(parseHostHeader(value), null, JSON.stringify(value));
    }
    assert.equal(parseHostHeader(null), null);
    assert.equal(parseHostHeader(undefined), null);
  });

  test("recognises IP literals only in the form browsers send", () => {
    for (const ip of ["127.0.0.1", "0.0.0.0", "192.168.1.20", "255.255.255.255", "[::1]", "[fe80::1]", "[::ffff:127.0.0.1]"]) {
      assert.equal(isIpLiteral(ip), true, ip);
    }
    for (const name of [
      "localhost",
      "256.1.1.1",
      "1.2.3",
      "1.2.3.4.5",
      "01.2.3.4",
      "2130706433",
      "0x7f.0.0.1",
      "127.0.0.1.attacker.example",
      "1.2.3.4a",
      "[1.2.3.4]",
      "[]",
    ]) {
      assert.equal(isIpLiteral(name), false, name);
    }
  });

  test("allows localhost, IP literals and listed names; refuses every other name", () => {
    for (const host of ["localhost", "localhost:3000", "LOCALHOST:3000", "127.0.0.1:3000", "192.168.1.20:3000", "[::1]:3000"]) {
      assert.equal(isAllowedHost(host, NONE), true, host);
    }
    for (const host of [
      "attacker.example:3000",
      "attacker.example",
      "localhost.attacker.example:3000",
      "127.0.0.1.attacker.example",
      "evil.localhost:3000",
      "notlocalhost",
      "",
      null,
      undefined,
    ]) {
      assert.equal(isAllowedHost(host, NONE), false, String(host));
    }
  });

  test("OVERTONE_UI_ALLOWED_HOSTS adds exact names, ignoring case, ports and blanks", () => {
    const allowed = parseAllowedHosts(" Notes.Example , studio.lan:3000,, https://bad.example ,");
    assert.deepEqual([...allowed].sort(), ["notes.example", "studio.lan"]);
    assert.equal(isAllowedHost("notes.example:3000", allowed), true);
    assert.equal(isAllowedHost("STUDIO.LAN", allowed), true);
    assert.equal(isAllowedHost("sub.notes.example", allowed), false);
    assert.equal(isAllowedHost("bad.example", allowed), false);
    assert.equal(parseAllowedHosts(undefined).size, 0);
    assert.equal(parseAllowedHosts("").size, 0);
  });
});

describe("same-origin check", () => {
  test("compares scheme, host and port with the Host header", () => {
    assert.equal(isSameOrigin("http://localhost:3000", "http:", "localhost:3000"), true);
    assert.equal(isSameOrigin("http://LOCALHOST:3000", "http", "localhost:3000"), true);
    assert.equal(isSameOrigin("http://127.0.0.1:3000", "http:", "127.0.0.1:3000"), true);
    assert.equal(isSameOrigin("http://[::1]:3000", "http:", "[::1]:3000"), true);
    assert.equal(isSameOrigin("https://notes.example", "https:", "notes.example"), true);
    // Default ports are the same origin whether or not they are spelled out.
    assert.equal(isSameOrigin("http://localhost", "http:", "localhost:80"), true);
    assert.equal(isSameOrigin("https://notes.example", "https:", "notes.example:443"), true);
  });

  test("differs on scheme, host or port", () => {
    assert.equal(isSameOrigin("https://localhost:3000", "http:", "localhost:3000"), false);
    assert.equal(isSameOrigin("http://localhost:3001", "http:", "localhost:3000"), false);
    assert.equal(isSameOrigin("http://127.0.0.1:3000", "http:", "localhost:3000"), false);
    assert.equal(isSameOrigin("https://evil.example", "http:", "localhost:3000"), false);
    assert.equal(isSameOrigin("http://localhost:3000.evil.example", "http:", "localhost:3000"), false);
    assert.equal(isSameOrigin("http://localhost:3000@evil.example", "http:", "localhost:3000"), false);
  });

  test("null, malformed and non-http origins never match", () => {
    for (const origin of ["null", "", "localhost:3000", "//localhost:3000", "file://", "chrome-extension://abc", "http://localhost:3000, https://evil.example"]) {
      assert.equal(isSameOrigin(origin, "http:", "localhost:3000"), false, JSON.stringify(origin));
    }
    assert.equal(isSameOrigin("http://localhost:3000", "http:", null), false);
    assert.equal(isSameOrigin("http://localhost:3000", "ftp:", "localhost:3000"), false);
  });
});

describe("checkApiRequest", () => {
  test("plain requests to localhost or an IP pass (curl, the UI's own GETs)", () => {
    assert.equal(codeOf({}), "ok");
    assert.equal(codeOf({ host: "127.0.0.1:3000" }), "ok");
    assert.equal(codeOf({ host: "192.168.1.20:3000" }), "ok");
    assert.equal(codeOf({ host: "[::1]:3000" }), "ok");
    assert.equal(codeOf({ secFetchSite: "same-origin" }), "ok");
    assert.equal(codeOf({ secFetchSite: "none" }), "ok");
  });

  test("a DNS-rebinding Host is refused, whatever the other headers say", () => {
    assert.equal(codeOf({ host: "attacker.example:3000" }), "forbidden_host");
    // Headers a rebinding page controls, or that the browser fills in "correctly" for it.
    assert.equal(
      codeOf({ host: "attacker.example:3000", origin: "http://attacker.example:3000", secFetchSite: "same-origin" }),
      "forbidden_host",
    );
    assert.equal(codeOf({ host: null }), "forbidden_host");
    assert.equal(codeOf({ host: "attacker.example/@localhost" }), "forbidden_host");
    // The operator can opt a name in.
    assert.equal(codeOf({ host: "notes.example:3000" }, new Set(["notes.example"])), "ok");
  });

  test("the UI's own state-changing requests pass (browsers send Origin on POST/PATCH/DELETE)", () => {
    assert.equal(codeOf({ origin: "http://localhost:3000", secFetchSite: "same-origin" }), "ok");
    assert.equal(codeOf({ host: "127.0.0.1:3000", origin: "http://127.0.0.1:3000" }), "ok");
    assert.equal(
      codeOf({ protocol: "https:", host: "notes.example", origin: "https://notes.example" }, new Set(["notes.example"])),
      "ok",
    );
  });

  test("requests carrying another origin are refused", () => {
    assert.equal(codeOf({ origin: "https://evil.example" }), "forbidden_origin");
    assert.equal(codeOf({ origin: "null" }), "forbidden_origin");
    assert.equal(codeOf({ origin: "" }), "forbidden_origin");
    // Another port, another scheme, or the other loopback spelling are all other origins.
    assert.equal(codeOf({ origin: "http://localhost:5173" }), "forbidden_origin");
    assert.equal(codeOf({ origin: "https://localhost:3000" }), "forbidden_origin");
    assert.equal(codeOf({ origin: "http://127.0.0.1:3000" }), "forbidden_origin");
  });

  test("Sec-Fetch-Site from another site is refused", () => {
    assert.equal(codeOf({ secFetchSite: "cross-site" }), "forbidden_origin");
    assert.equal(codeOf({ secFetchSite: "same-site" }), "forbidden_origin");
    assert.equal(codeOf({ secFetchSite: "Cross-Site" }), "forbidden_origin");
    assert.equal(codeOf({ secFetchSite: "something-new" }), "forbidden_origin");
    // A matching Origin does not rescue a cross-site request.
    assert.equal(codeOf({ origin: "http://localhost:3000", secFetchSite: "cross-site" }), "forbidden_origin");
  });

  test("refusals are 403 with a message", () => {
    const decision = checkApiRequest(request({ host: "attacker.example" }), NONE);
    assert.equal(decision.ok, false);
    if (!decision.ok) {
      assert.equal(decision.status, 403);
      assert.match(decision.message, /OVERTONE_UI_ALLOWED_HOSTS/);
    }
  });
});

describe("backendUrl", () => {
  test("maps /api/x?y to <backend>/x?y", () => {
    assert.equal(backendUrl(undefined, "/api/notebooks")?.href, "http://127.0.0.1:8787/notebooks");
    assert.equal(backendUrl("", "/api/notebooks")?.href, "http://127.0.0.1:8787/notebooks");
    assert.equal(backendUrl("http://127.0.0.1:9000", "/api/notebooks/n1/guide", "?refresh=1")?.href, "http://127.0.0.1:9000/notebooks/n1/guide?refresh=1");
    assert.equal(backendUrl("http://engine.internal:8787/", "/api/health")?.href, "http://engine.internal:8787/health");
    assert.equal(backendUrl("https://engine.example/overtone/", "/api/artifacts/a1/audio", "?download=1")?.href, "https://engine.example/overtone/artifacts/a1/audio?download=1");
    assert.equal(backendUrl(undefined, "/api")?.href, "http://127.0.0.1:8787/");
    assert.equal(backendUrl(undefined, "/api/")?.href, "http://127.0.0.1:8787/");
  });

  test("keeps percent-encoding in ids", () => {
    assert.equal(backendUrl(undefined, "/api/sources/a%2Fb%20c/text")?.href, "http://127.0.0.1:8787/sources/a%2Fb%20c/text");
  });

  test("ignores paths outside /api", () => {
    for (const pathname of ["/", "/apix", "/apix/notebooks", "/API/notebooks", "/notebook/abc", "api/notebooks", ""]) {
      assert.equal(backendUrl(undefined, pathname), null, JSON.stringify(pathname));
    }
  });

  test("the request cannot change where the request (and the API key) is sent", () => {
    const base = "http://127.0.0.1:8787";
    for (const [pathname, search] of [
      ["/api//evil.example/x", ""],
      ["/api/@evil.example", ""],
      ["/api/\\\\evil.example", ""],
      ["/api/http://evil.example/", ""],
      ["/api/x", "?@evil.example"],
      ["/api/x", "?u=http://evil.example#frag"],
      ["/api/%2F%2Fevil.example", ""],
    ] as const) {
      const target = backendUrl(base, pathname, search);
      assert.ok(target, pathname);
      assert.equal(target.origin, base, `${pathname}${search}`);
      assert.equal(target.username, "", pathname);
    }
  });

  test("drops credentials embedded in OVERTONE_API_URL and rejects non-http URLs", () => {
    const target = backendUrl("http://user:secret@engine.internal:8787", "/api/health");
    assert.equal(target?.href, "http://engine.internal:8787/health");
    assert.throws(() => backendUrl("not a url", "/api/health"));
    assert.throws(() => backendUrl("file:///etc/passwd", "/api/health"));
  });
});

describe("forwardedHeaders", () => {
  test("attaches the server-side key and replaces a client-supplied Authorization", () => {
    const incoming = new Headers({ Authorization: "Bearer from-the-browser", Accept: "application/json", Range: "bytes=0-99" });
    const headers = forwardedHeaders(incoming, "server-key");
    assert.equal(headers.get("authorization"), "Bearer server-key");
    assert.equal(headers.get("accept"), "application/json");
    assert.equal(headers.get("range"), "bytes=0-99");
    // The incoming headers are left untouched.
    assert.equal(incoming.get("authorization"), "Bearer from-the-browser");
  });

  test("drops Origin, Referer and Cookie, with or without a key", () => {
    for (const key of [undefined, "server-key"]) {
      const incoming = new Headers({
        Origin: "https://notes.example",
        Referer: "https://notes.example/notebook/n1",
        Cookie: "other_app_session=abc",
        "Content-Type": "application/json",
        "Sec-Fetch-Site": "same-origin",
      });
      const headers = forwardedHeaders(incoming, key);
      assert.equal(headers.has("origin"), false);
      assert.equal(headers.has("referer"), false);
      assert.equal(headers.has("cookie"), false);
      assert.equal(headers.get("content-type"), "application/json");
      assert.equal(headers.get("sec-fetch-site"), "same-origin");
      assert.equal(incoming.get("origin"), "https://notes.example");
    }
  });

  test("adds the key when the client sent none", () => {
    assert.equal(forwardedHeaders(new Headers(), " server-key\n").get("authorization"), "Bearer server-key");
  });

  test("without a configured key the client's headers pass through unchanged", () => {
    for (const key of [undefined, null, "", "   "]) {
      const headers = forwardedHeaders(new Headers({ Authorization: "Bearer mine", "Content-Type": "application/json" }), key);
      assert.equal(headers.get("authorization"), "Bearer mine");
      assert.equal(headers.get("content-type"), "application/json");
      assert.equal(forwardedHeaders(new Headers(), key).has("authorization"), false);
    }
  });
});
