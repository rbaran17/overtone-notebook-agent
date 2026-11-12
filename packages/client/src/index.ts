import {
  OvertoneError,
  type Artifact,
  type ArtifactRequest,
  type Capabilities,
  type ChatEvent,
  type ChatInput,
  type ChatMessage,
  type ID,
  type LiveDelegateRequest,
  type LiveDelegateResult,
  type LiveSession,
  type LiveSessionRequest,
  type Notebook,
  type NotebookGuide,
  type OvertoneClient,
  type Source,
  type SourceInput,
} from "./types";

export type * from "./types";
export { OvertoneError } from "./types";
export * from "./live";

// Browser-safe and Node 20+: only fetch, FormData, Blob, ReadableStream and TextDecoder.

export interface ClientOptions {
  /** Where the API is reachable, e.g. "/api" (proxied) or "http://127.0.0.1:8787". */
  baseUrl: string;
  /** Sent as `Authorization: Bearer <apiKey>` when set. */
  apiKey?: string;
  /** Custom fetch (tests, React Native, auth wrappers). Default: globalThis.fetch. */
  fetch?: typeof fetch;
}

interface ErrorItem {
  name?: string;
  error: string;
  code: string;
}

export function createClient(opts: ClientOptions): OvertoneClient {
  const base = opts.baseUrl.replace(/\/+$/, "");
  // Call fetch as a plain function bound to globalThis ("Illegal invocation" otherwise).
  const doFetch: typeof fetch = opts.fetch ?? globalThis.fetch.bind(globalThis);
  const enc = encodeURIComponent;

  function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
    return opts.apiKey ? { ...extra, Authorization: `Bearer ${opts.apiKey}` } : { ...extra };
  }

  async function toError(res: Response): Promise<OvertoneError> {
    const body = (await res.json().catch(() => null)) as { error?: { message?: string; code?: string } } | null;
    return new OvertoneError(body?.error?.message ?? (res.statusText || `Request failed with status ${res.status}`), {
      status: res.status,
      code: body?.error?.code ?? "http_error",
    });
  }

  async function call(method: string, path: string, body?: unknown): Promise<Response> {
    const init: RequestInit = {
      method,
      headers: authHeaders(body === undefined ? {} : { "Content-Type": "application/json" }),
    };
    if (body !== undefined) init.body = JSON.stringify(body);
    const res = await doFetch(`${base}${path}`, init);
    if (!res.ok) throw await toError(res);
    return res;
  }

  async function getJson<T>(path: string): Promise<T> {
    return (await call("GET", path)).json() as Promise<T>;
  }

  async function postJson<T>(path: string, body: unknown): Promise<T> {
    return (await call("POST", path, body)).json() as Promise<T>;
  }

  async function postForm<T>(path: string, form: FormData): Promise<T> {
    // No Content-Type: fetch sets the multipart boundary itself.
    const res = await doFetch(`${base}${path}`, { method: "POST", headers: authHeaders(), body: form });
    if (!res.ok) throw await toError(res);
    return res.json() as Promise<T>;
  }

  /** Thrown when a request succeeded at HTTP level but no source was created. */
  function sourceFailure(errors: ErrorItem[]): OvertoneError {
    const first = errors[0];
    return new OvertoneError(first?.error ?? "The source could not be added.", {
      status: 400,
      code: first?.code ?? "bad_request",
    });
  }

  function parseSseEvent(block: string): ChatEvent | null {
    const data = block
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data) return null;
    try {
      return JSON.parse(data) as ChatEvent;
    } catch {
      return null;
    }
  }

  /** Streams a chat answer as ChatEvents. HTTP failures become a single `error` event. */
  async function* streamChat(notebookId: ID, input: ChatInput, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
    let res: Response;
    try {
      res = await doFetch(`${base}/notebooks/${enc(notebookId)}/chat`, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" }),
        body: JSON.stringify({ ...input, stream: true }),
        signal,
      });
    } catch (err) {
      if (signal?.aborted) return;
      yield { type: "error", error: err instanceof Error ? err.message : String(err) };
      return;
    }
    if (!res.ok) {
      const err = await toError(res);
      yield { type: "error", error: err.message };
      return;
    }
    if (!res.body) {
      yield { type: "error", error: "The server sent an empty response." };
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() ?? "";
        for (const block of blocks) {
          const event = parseSseEvent(block);
          if (event) yield event;
        }
      }
      buffer = (buffer + decoder.decode()).replace(/\r\n/g, "\n");
      const last = parseSseEvent(buffer);
      if (last) yield last;
    } catch (err) {
      if (!signal?.aborted) yield { type: "error", error: err instanceof Error ? err.message : String(err) };
    } finally {
      await reader.cancel().catch(() => {});
    }
  }

  const audioUrl = (id: ID): string => {
    const key = opts.apiKey ? `?key=${enc(opts.apiKey)}` : "";
    return `${base}/artifacts/${enc(id)}/audio${key}`;
  };

  const getArtifact = async (id: ID): Promise<Artifact> =>
    (await getJson<{ artifact: Artifact }>(`/artifacts/${enc(id)}`)).artifact;

  const createArtifact = async (notebookId: ID, request: ArtifactRequest): Promise<Artifact> =>
    (await postJson<{ artifact: Artifact }>(`/notebooks/${enc(notebookId)}/artifacts`, request)).artifact;

  const waitArtifact = async (
    id: ID,
    opts: { onProgress?: (a: Artifact) => void; intervalMs?: number } = {},
  ): Promise<Artifact> => {
    const interval = opts.intervalMs ?? 1500;
    let seen: string | undefined;
    while (true) {
      const artifact = await getArtifact(id);
      if (artifact.updatedAt !== seen) {
        seen = artifact.updatedAt;
        opts.onProgress?.(artifact);
      }
      if (artifact.status === "ready" || artifact.status === "error") return artifact;
      await new Promise((resolve) => setTimeout(resolve, interval));
    }
  };

  return {
    async health(): Promise<Capabilities> {
      return getJson<Capabilities>("/health");
    },

    notebooks: {
      async list(): Promise<Notebook[]> {
        return (await getJson<{ notebooks: Notebook[] }>("/notebooks")).notebooks;
      },
      async create(input = {}): Promise<Notebook> {
        return (await postJson<{ notebook: Notebook }>("/notebooks", input)).notebook;
      },
      async get(id: ID): Promise<Notebook> {
        return (await getJson<{ notebook: Notebook }>(`/notebooks/${enc(id)}`)).notebook;
      },
      async update(id: ID, patch): Promise<Notebook> {
        return (await call("PATCH", `/notebooks/${enc(id)}`, patch)).json().then((b: { notebook: Notebook }) => b.notebook);
      },
      async delete(id: ID): Promise<void> {
        await call("DELETE", `/notebooks/${enc(id)}`);
      },
      async guide(id: ID, guideOpts?: { refresh?: boolean }): Promise<NotebookGuide> {
        const query = guideOpts?.refresh ? "?refresh=1" : "";
        return (await getJson<{ guide: NotebookGuide }>(`/notebooks/${enc(id)}/guide${query}`)).guide;
      },
    },

    sources: {
      async list(notebookId: ID): Promise<Source[]> {
        return (await getJson<{ sources: Source[] }>(`/notebooks/${enc(notebookId)}/sources`)).sources;
      },

      async add(notebookId: ID, input: SourceInput): Promise<Source> {
        let body: { sources?: Source[]; errors?: ErrorItem[] };
        if (input.type === "file") {
          const form = new FormData();
          form.append("file", new Blob([input.data as BlobPart], { type: input.mimeType }), input.filename);
          body = await postForm(`/notebooks/${enc(notebookId)}/sources`, form);
        } else {
          body = await postJson(`/notebooks/${enc(notebookId)}/sources`, input);
        }
        if (!body.sources?.length) throw sourceFailure(body.errors ?? []);
        return body.sources[0];
      },

      async addFiles(notebookId: ID, files: File[]): Promise<Source[]> {
        if (files.length === 0) {
          const none: Source[] = [];
          Object.defineProperty(none, "errors", { value: [] as ErrorItem[] });
          return none;
        }
        const form = new FormData();
        for (const file of files) form.append("file", file, file.name);
        const body = await postForm<{ sources?: Source[]; errors?: ErrorItem[] }>(
          `/notebooks/${enc(notebookId)}/sources`,
          form,
        );
        const errors = body.errors ?? [];
        if (!body.sources?.length) throw sourceFailure(errors);
        const result: Source[] = [...body.sources];
        // Partial failures stay visible without changing the array's shape.
        Object.defineProperty(result, "errors", { value: errors });
        return result;
      },

      async get(id: ID): Promise<Source> {
        return (await getJson<{ source: Source }>(`/sources/${enc(id)}`)).source;
      },

      async getText(id: ID): Promise<string> {
        return (await call("GET", `/sources/${enc(id)}/text`)).text();
      },

      async delete(id: ID): Promise<void> {
        await call("DELETE", `/sources/${enc(id)}`);
      },
    },

    chat: {
      async history(notebookId: ID): Promise<ChatMessage[]> {
        return (await getJson<{ messages: ChatMessage[] }>(`/notebooks/${enc(notebookId)}/messages`)).messages;
      },

      async clear(notebookId: ID): Promise<void> {
        await call("DELETE", `/notebooks/${enc(notebookId)}/messages`);
      },

      stream(notebookId: ID, input: ChatInput, streamOpts?: { signal?: AbortSignal }) {
        return streamChat(notebookId, input, streamOpts?.signal);
      },

      async send(notebookId: ID, input: ChatInput): Promise<ChatMessage> {
        return (await postJson<{ message: ChatMessage }>(`/notebooks/${enc(notebookId)}/chat`, { ...input, stream: false })).message;
      },
    },

    artifacts: {
      async list(notebookId: ID): Promise<Artifact[]> {
        return (await getJson<{ artifacts: Artifact[] }>(`/notebooks/${enc(notebookId)}/artifacts`)).artifacts;
      },

      create: createArtifact,

      async generate(notebookId: ID, request: ArtifactRequest, genOpts?: { onProgress?: (a: Artifact) => void }): Promise<Artifact> {
        const created = await createArtifact(notebookId, request);
        const done = await waitArtifact(created.id, { onProgress: genOpts?.onProgress });
        if (done.status === "error") {
          throw new OvertoneError(done.error ?? "Generation failed.", { status: 502, code: "generation_failed" });
        }
        return done;
      },

      get: getArtifact,

      wait: waitArtifact,

      audioUrl,

      async getAudio(id: ID): Promise<{ data: Uint8Array; mimeType: string } | null> {
        // The key travels in the Authorization header here, not in the URL.
        const res = await doFetch(`${base}/artifacts/${enc(id)}/audio`, { headers: authHeaders() });
        if (res.status === 404) return null;
        if (!res.ok) throw await toError(res);
        const mimeType = (res.headers.get("content-type") ?? "audio/mpeg").split(";")[0].trim();
        return { data: new Uint8Array(await res.arrayBuffer()), mimeType };
      },

      async delete(id: ID): Promise<void> {
        await call("DELETE", `/artifacts/${enc(id)}`);
      },
    },

    live: {
      async createSession(notebookId: ID, request: LiveSessionRequest): Promise<LiveSession> {
        return (await postJson<{ session: LiveSession }>(`/notebooks/${enc(notebookId)}/live/sessions`, request)).session;
      },

      async delegate(notebookId: ID, request: LiveDelegateRequest): Promise<LiveDelegateResult> {
        return postJson<LiveDelegateResult>(`/notebooks/${enc(notebookId)}/live/delegate`, request);
      },
    },
  };
}
