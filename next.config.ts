import type { NextConfig } from "next";
import { MAX_UPLOAD_BYTES } from "./src/lib/limits";

// The engine is a separate Python service (see backend/). The browser calls
// /api/* on this origin and Next.js forwards it, so there is no CORS setup and
// the backend address stays a server-side detail.
const backend = (process.env.OVERTONE_API_URL ?? "http://127.0.0.1:8787").replace(/\/+$/, "");

// Next.js drops a forwarded request once the engine has sent nothing for this
// long, and answers 500. Its default is 30 seconds, which one model call often
// exceeds: a notebook guide, a non-streaming chat reply, or the pause before the
// first token of a streamed one. Ten minutes is the read timeout of the engine's
// Claude client, so the web UI waits for the engine as long as the engine waits
// for the model.
const DEFAULT_API_TIMEOUT_SECONDS = 600;
const MAX_TIMER_MS = 2 ** 31 - 1;

function apiTimeoutMs(): number {
  const raw = process.env.OVERTONE_API_TIMEOUT?.trim();
  if (!raw) return DEFAULT_API_TIMEOUT_SECONDS * 1000;
  const ms = Math.round(Number(raw) * 1000);
  // There is no "off": Next.js reads 0 as its 30 second default.
  if (!(ms >= 1000 && ms <= MAX_TIMER_MS)) {
    throw new Error(
      `OVERTONE_API_TIMEOUT must be a number of seconds from 1 to ${Math.floor(MAX_TIMER_MS / 1000)}, got "${raw}".`,
    );
  }
  return ms;
}

// Next.js copies every forwarded request body into memory and silently cuts it
// off at this size (default 10 MB) instead of refusing it. The UI uploads one
// file per request, so the buffer has to hold the engine's largest file plus the
// multipart framing around it, and never a whole batch.
const API_BODY_LIMIT_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024;

const nextConfig: NextConfig = {
  // The API client is a workspace package shipped as TypeScript source.
  transpilePackages: ["@overtone/client"],
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/:path*` }];
  },
  experimental: {
    proxyTimeout: apiTimeoutMs(),
    proxyClientMaxBodySize: API_BODY_LIMIT_BYTES,
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
