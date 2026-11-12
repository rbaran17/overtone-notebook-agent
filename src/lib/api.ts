import { createClient } from "@overtone/client";

export type * from "@overtone/client";

/**
 * The browser talks to the engine through the same REST API any other app
 * would use. `/api` is forwarded to the Python backend by next.config.ts.
 */
export const api = createClient({ baseUrl: "/api" });
