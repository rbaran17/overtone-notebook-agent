import { createClient } from "@overtone/client";

export type * from "@overtone/client";

/**
 * The browser talks to the engine through the same REST API any other app
 * would use. `/api` is forwarded to the Python backend by src/proxy.ts.
 *
 * No `apiKey` here, on purpose: when the engine requires a key (OVERTONE_API_KEY),
 * the proxy attaches it on the server. The key is never in this bundle, in a
 * request the browser makes, or in a URL (`artifacts.audioUrl()` adds `?key=` only
 * for clients that hold a key, so the `<audio>` element and the download link
 * here are plain `/api/artifacts/<id>/audio` paths).
 */
export const api = createClient({ baseUrl: "/api" });
