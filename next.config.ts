import type { NextConfig } from "next";

// The engine is a separate Python service (see backend/). The browser calls
// /api/* on this origin and Next.js forwards it, so there is no CORS setup and
// the backend address stays a server-side detail.
const backend = (process.env.OVERTONE_API_URL ?? "http://127.0.0.1:8787").replace(/\/+$/, "");

const nextConfig: NextConfig = {
  // The API client is a workspace package shipped as TypeScript source.
  transpilePackages: ["@overtone/client"],
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/:path*` }];
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
