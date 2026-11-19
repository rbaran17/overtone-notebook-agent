import { NextResponse, type NextRequest } from "next/server";
import { backendUrl, checkApiRequest, forwardedHeaders, parseAllowedHosts } from "@/lib/apiProxy";

/**
 * The only way from the browser to the engine.
 *
 * The engine is a separate Python service (see backend/). The UI calls `/api/*` on its own
 * origin and this proxy forwards the request, so there is no CORS setup and the engine's
 * API key stays on the server. Because forwarding replaces `Host` and makes every caller
 * look local to the engine, the checks a browser-facing server needs happen here, before
 * anything is forwarded (rules and rationale: src/lib/apiProxy.ts):
 *
 * - `Host` must be localhost, an IP literal or listed in OVERTONE_UI_ALLOWED_HOSTS;
 * - requests from other origins or sites are refused;
 * - OVERTONE_API_KEY, when set, is attached as the bearer key in place of anything the
 *   client sent.
 *
 * There is deliberately no `rewrites()` fallback in next.config.ts: if this file does not
 * run for a path, that path is not forwarded at all.
 */
export function proxy(request: NextRequest) {
  const decision = checkApiRequest(
    {
      // The URL's host is the address the server listens on; the scheme is the part that
      // reflects the request (TLS socket or X-Forwarded-Proto). `Host` is read from the header.
      protocol: request.nextUrl.protocol,
      host: request.headers.get("host"),
      origin: request.headers.get("origin"),
      secFetchSite: request.headers.get("sec-fetch-site"),
    },
    parseAllowedHosts(process.env.OVERTONE_UI_ALLOWED_HOSTS),
  );
  if (!decision.ok) {
    return NextResponse.json(
      { error: { message: decision.message, code: decision.code } },
      { status: decision.status, headers: { "Cache-Control": "no-store" } },
    );
  }

  let target: URL | null;
  try {
    target = backendUrl(process.env.OVERTONE_API_URL, request.nextUrl.pathname, request.nextUrl.search);
  } catch {
    return NextResponse.json(
      { error: { message: "OVERTONE_API_URL is not a valid http(s) URL.", code: "bad_gateway" } },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
  if (!target) return NextResponse.next();

  // An external rewrite: Next.js streams the request to the engine and the response back
  // (server-sent events and audio Range requests pass through unbuffered).
  return NextResponse.rewrite(target, {
    request: { headers: forwardedHeaders(request.headers, process.env.OVERTONE_API_KEY) },
  });
}

export const config = {
  matcher: ["/api", "/api/:path*"],
};
