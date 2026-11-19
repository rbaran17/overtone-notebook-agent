/**
 * Rules for the `/api` proxy (see src/proxy.ts): which requests may reach the
 * engine, where they go, and which headers travel with them.
 *
 * Pure functions with no Next.js imports, so the rules are unit-tested without
 * a server (test/apiProxy.test.ts).
 */

export const API_PREFIX = "/api";
export const DEFAULT_BACKEND_URL = "http://127.0.0.1:8787";

export type ForbiddenCode = "forbidden_host" | "forbidden_origin";

export type ApiDecision = { ok: true } | { ok: false; status: 403; code: ForbiddenCode; message: string };

/**
 * The parts of a request the rules look at. Header values are passed as received (null when
 * absent). The method is not among them: the rules are the same for reads and writes.
 */
export interface ApiRequestInfo {
  /** How the browser reached this server: "http:" or "https:" (a trailing colon is optional). */
  protocol: string;
  host: string | null;
  origin: string | null;
  secFetchSite: string | null;
}

// A `Host` value is a name or an IP literal, optionally followed by a port. Anything else
// (userinfo, paths, spaces, empty labels) is rejected rather than interpreted.
const HOST_PATTERN = /^(\[[0-9a-f:.]+\]|[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.?)(?::(\d{1,5}))?$/i;
const OCTET = "(?:25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)";
const IPV4_PATTERN = new RegExp(`^${OCTET}(?:\\.${OCTET}){3}$`);

/** Splits a `Host` header into a lower-cased hostname (IPv6 keeps its brackets) and port. Null when malformed. */
export function parseHostHeader(value: string | null | undefined): { hostname: string; port: string } | null {
  const match = HOST_PATTERN.exec((value ?? "").trim());
  if (!match) return null;
  const hostname = match[1].toLowerCase().replace(/\.$/, "");
  return { hostname, port: match[2] ?? "" };
}

/**
 * True for a dotted-quad IPv4 address or a bracketed IPv6 address.
 *
 * Browsers normalise every other spelling of an IPv4 address (`127.1`, `0x7f.0.0.1`,
 * `2130706433`) to the dotted quad before sending `Host`, and a DNS name can never end in
 * a numeric label or contain brackets. So an IP literal in `Host` means the browser was
 * pointed at that address directly, not at a name an attacker could re-point.
 */
export function isIpLiteral(hostname: string): boolean {
  if (hostname.startsWith("[")) return hostname.endsWith("]") && hostname.includes(":");
  return IPV4_PATTERN.test(hostname);
}

/** Parses `OVERTONE_UI_ALLOWED_HOSTS`: comma-separated host names (a port, if given, is ignored). */
export function parseAllowedHosts(value: string | null | undefined): Set<string> {
  const hosts = new Set<string>();
  for (const entry of (value ?? "").split(",")) {
    const parsed = parseHostHeader(entry);
    if (parsed) hosts.add(parsed.hostname);
  }
  return hosts;
}

/**
 * DNS-rebinding guard. A rebinding page reaches this server under the attacker's own DNS
 * name, and the browser reports that name in `Host`. Only `localhost`, IP literals and
 * names the operator listed are served.
 */
export function isAllowedHost(host: string | null | undefined, allowedHosts: ReadonlySet<string>): boolean {
  const parsed = parseHostHeader(host);
  if (!parsed) return false;
  return parsed.hostname === "localhost" || isIpLiteral(parsed.hostname) || allowedHosts.has(parsed.hostname);
}

/** The origin a same-origin page would report for this request: scheme + `Host`. Null when either is unusable. */
export function requestOrigin(protocol: string, host: string | null | undefined): string | null {
  const scheme = protocol.replace(/:$/, "").toLowerCase();
  if (scheme !== "http" && scheme !== "https") return null;
  if (!host || !parseHostHeader(host)) return null;
  try {
    return new URL(`${scheme}://${host.trim()}`).origin;
  } catch {
    return null;
  }
}

/** True when an `Origin` header names the same scheme, host and port the request was sent to. */
export function isSameOrigin(origin: string, protocol: string, host: string | null | undefined): boolean {
  const own = requestOrigin(protocol, host);
  if (!own) return false;
  try {
    // `new URL("null")` throws, and non-http(s) schemes serialise to "null", which never equals `own`.
    return new URL(origin.trim()).origin === own;
  } catch {
    return false;
  }
}

function forbidden(code: ForbiddenCode, message: string): ApiDecision {
  return { ok: false, status: 403, code, message };
}

/**
 * Decides whether a request to `/api` may be forwarded to the engine.
 *
 * 1. `Host` must be `localhost`, an IP literal or an allowed name (DNS rebinding).
 * 2. `Sec-Fetch-Site`, when the browser sends it, must be `same-origin` (the UI itself) or
 *    `none` (typed into the address bar). `cross-site` and `same-site` (another port on
 *    localhost) are other pages reaching in.
 * 3. `Origin`, when present, must be this server's own origin. Browsers add it to every
 *    POST/PUT/PATCH/DELETE and to every cross-origin fetch, so this stops other pages from
 *    changing anything, and (for browsers that predate `Sec-Fetch-Site`) from reading through
 *    an engine that has CORS switched on. `Origin: null` (sandboxed frames, `file:` pages)
 *    is never this UI.
 *
 * Requests without these headers (curl, scripts) pass: they are not driven by a web page,
 * and the bind address decides who can connect at all.
 */
export function checkApiRequest(request: ApiRequestInfo, allowedHosts: ReadonlySet<string>): ApiDecision {
  if (!isAllowedHost(request.host, allowedHosts)) {
    return forbidden(
      "forbidden_host",
      "The Overtone web UI only answers API requests addressed to localhost or an IP address. " +
        "To use another host name, list it in OVERTONE_UI_ALLOWED_HOSTS.",
    );
  }

  const site = request.secFetchSite?.trim().toLowerCase();
  if (site && site !== "same-origin" && site !== "none") {
    return forbidden("forbidden_origin", "The Overtone web UI does not accept API requests from other sites.");
  }

  if (request.origin !== null && !isSameOrigin(request.origin, request.protocol, request.host)) {
    return forbidden("forbidden_origin", "The Overtone web UI does not accept API requests from other origins.");
  }

  return { ok: true };
}

/**
 * Maps `/api/x?y` on this origin to `<backend>/x?y`. Returns null for paths outside `/api`
 * and throws when `baseUrl` (OVERTONE_API_URL) is not an http(s) URL.
 *
 * The path and query are assigned through the URL API, so nothing in the request can
 * change the scheme, host or port the (possibly key-bearing) request is sent to.
 *
 * Next.js reports a proxy rewrite's target to the browser in an `x-middleware-rewrite`
 * response header, so the engine's address is visible to anyone who can use the UI.
 * Credentials embedded in the base URL are dropped for that reason; the engine's key
 * belongs in OVERTONE_API_KEY, which travels in a request header instead.
 */
export function backendUrl(baseUrl: string | null | undefined, pathname: string, search = ""): URL | null {
  if (pathname !== API_PREFIX && !pathname.startsWith(`${API_PREFIX}/`)) return null;
  const target = new URL(baseUrl?.trim() || DEFAULT_BACKEND_URL);
  if (target.protocol !== "http:" && target.protocol !== "https:") {
    throw new Error("OVERTONE_API_URL must be an http(s) URL.");
  }
  const rest = pathname.slice(API_PREFIX.length) || "/";
  target.pathname = target.pathname.replace(/\/+$/, "") + rest;
  target.search = search;
  target.hash = "";
  target.username = "";
  target.password = "";
  return target;
}

/** Browser context that is checked here and means nothing (or the wrong thing) to the engine. */
const NOT_FORWARDED = ["origin", "referer", "cookie"];

/**
 * Headers for the forwarded request.
 *
 * - With an API key configured, the server-side key is the only credential that reaches the
 *   engine: whatever `Authorization` the client sent is replaced, and the key itself never
 *   appears in anything the browser receives.
 * - `Origin` is dropped. `checkApiRequest` has already established the request is
 *   same-origin, and the engine cannot repeat that check: it sees its own address in `Host`,
 *   so it would refuse writes from a UI served under any non-loopback name.
 * - `Referer` and `Cookie` are dropped. The engine uses neither, and cookies are shared by
 *   every port on a host name, so they may belong to some other local app.
 */
export function forwardedHeaders(incoming: Headers, apiKey: string | null | undefined): Headers {
  const headers = new Headers(incoming);
  for (const name of NOT_FORWARDED) headers.delete(name);
  const key = apiKey?.trim();
  if (key) headers.set("authorization", `Bearer ${key}`);
  return headers;
}
