/**
 * Wire types for the Overtone REST API.
 *
 * These mirror the Pydantic models in `backend/overtone/models.py` field for
 * field (the API speaks camelCase JSON). Browser-safe: no runtime imports.
 */

export type ID = string;
/** ISO-8601 timestamp. */
export type Timestamp = string;

// ───────────────────────────── Notebooks ─────────────────────────────

export interface Notebook {
  id: ID;
  title: string;
  description?: string;
  /** Emoji or short glyph shown on the notebook card. */
  icon?: string;
  createdAt: Timestamp;
  updatedAt: Timestamp;
  sourceCount?: number;
  /** Cached notebook guide; cleared whenever the sources change. */
  guide?: NotebookGuide;
}

export interface NotebookGuide {
  summary: string;
  questions: string[];
  generatedAt: Timestamp;
}

// ────────────────────────────── Sources ──────────────────────────────

export type SourceKind = "file" | "url" | "text";

export interface Source {
  id: ID;
  notebookId: ID;
  kind: SourceKind;
  title: string;
  /** Original filename or URL. */
  origin?: string;
  mimeType?: string;
  /** Length of the extracted text. */
  charCount: number;
  /** First ~280 characters of the extracted text, for previews. */
  excerpt: string;
  createdAt: Timestamp;
}

/** What callers hand to `sources.add`. */
export type SourceInput =
  | { type: "text"; text: string; title?: string }
  | { type: "url"; url: string; title?: string }
  | { type: "file"; filename: string; data: Uint8Array; mimeType?: string; title?: string };

// ─────────────────────────────── Chat ────────────────────────────────

export interface Citation {
  /** 1-based number; matches the `[n]` marker embedded in the message text. */
  index: number;
  sourceId: ID;
  sourceTitle: string;
  /** The exact passage from the source that supports the claim. */
  quote: string;
  startChar?: number;
  endChar?: number;
}

export interface ChatMessage {
  id: ID;
  notebookId: ID;
  role: "user" | "assistant";
  /** Markdown. Assistant messages contain `[n]` citation markers. */
  content: string;
  citations?: Citation[];
  createdAt: Timestamp;
}

export interface ChatInput {
  message: string;
  /** Restrict grounding to these sources. Omit to use every source. */
  sourceIds?: ID[];
}

export type ChatEvent =
  | { type: "text"; delta: string }
  | { type: "citation"; citation: Citation }
  | { type: "done"; message: ChatMessage }
  | { type: "error"; error: string; code?: string; status?: number };

// ───────────────────────────── Artifacts ─────────────────────────────

export type DocumentKind = "summary" | "briefing" | "study_guide" | "faq" | "timeline" | "custom";
export type ArtifactKind = "audio_overview" | DocumentKind;

/** deep_dive / brief / debate / critique are two-host shows; solo is one narrator. */
export type AudioFormat = "deep_dive" | "brief" | "debate" | "critique" | "solo";
export type AudioLength = "short" | "medium" | "long";

export interface AudioHost {
  /** Leave empty to use the default host name. */
  name: string;
  /** Provider-specific voice id. Omit to use the provider default. */
  voice?: string;
}

export interface AudioOptions {
  format?: AudioFormat;
  length?: AudioLength;
  /** Free-form steering: audience, focus, tone, what to skip. */
  instructions?: string;
  hosts?: AudioHost[];
  /** Output language, e.g. "English", "Spanish". */
  language?: string;
}

export interface DialogueTurn {
  /** Index into `AudioScript.hosts`. */
  speaker: number;
  text: string;
}

export interface AudioScript {
  title: string;
  summary: string;
  hosts: AudioHost[];
  turns: DialogueTurn[];
}

export interface ArtifactAudio {
  mimeType: string;
  blobKey: string;
  bytes: number;
  /** TTS provider id that rendered it. */
  provider: string;
}

export type ArtifactStatus = "queued" | "generating" | "ready" | "error";

export interface Artifact {
  id: ID;
  notebookId: ID;
  kind: ArtifactKind;
  title: string;
  status: ArtifactStatus;
  /** Human-readable stage while generating, e.g. "Recording audio (4/22)". */
  progress?: string;
  error?: string;
  sourceIds: ID[];
  request: ArtifactRequest;
  /** Markdown body, for document kinds. */
  content?: string;
  /** Dialogue, for audio overviews. */
  script?: AudioScript;
  /**
   * Rendered audio. `null` on a ready audio overview means no TTS provider was
   * configured (or it failed, see `audioError`): play the script client-side.
   */
  audio?: ArtifactAudio | null;
  audioError?: string;
  createdAt: Timestamp;
  updatedAt: Timestamp;
}

export type ArtifactRequest =
  | { kind: "audio_overview"; sourceIds?: ID[]; options?: AudioOptions }
  | { kind: DocumentKind; sourceIds?: ID[]; instructions?: string };

// ──────────────────────────── Capabilities ───────────────────────────

export interface TtsVoice {
  id: string;
  label: string;
}

export interface Capabilities {
  name: "overtone";
  version: string;
  /** Model id used for chat and generation. */
  model: string;
  /** Agent runtime the engine is built on. */
  framework: string;
  /** Text-to-speech provider that renders episodes, or null (browser voices). */
  tts: { provider: string; label: string; voices: TtsVoice[]; defaultVoices: string[] } | null;
  /** Live voice (GPT-Live), or null when not configured. */
  live: { model: string; voices: string[]; defaultVoice: string } | null;
}

// ───────────────────────────── Live voice ────────────────────────────

/** "conversation": talk to the notebook. "podcast": join an episode as a listener. */
export type LiveMode = "conversation" | "podcast";

export interface LiveSessionRequest {
  /** The browser's WebRTC SDP offer. */
  sdp: string;
  mode?: LiveMode;
  sourceIds?: ID[];
  /** Podcast mode: the audio overview being listened to… */
  artifactId?: ID;
  /** …and the turn the listener paused on. */
  turnIndex?: number;
  voice?: string;
}

export interface LiveSession {
  sessionId: string;
  /** The SDP answer to hand to `setRemoteDescription`. */
  sdp: string;
  mode: LiveMode;
  model: string;
  voice: string;
}

export interface LiveTranscriptLine {
  role: "user" | "assistant";
  text: string;
}

export interface LiveDelegateRequest {
  mode?: LiveMode;
  /** Recent conversation, oldest first. The last user line is the question. */
  transcript: LiveTranscriptLine[];
  sourceIds?: ID[];
  artifactId?: ID;
}

export interface LiveDelegateResult {
  /** Short, speakable answer for the voice model to relay. */
  answer: string;
  citations: Citation[];
}

// ─────────────────────────────── Client ──────────────────────────────

/** Typed client over the REST API. Implemented by `createClient`. */
export interface OvertoneClient {
  health(): Promise<Capabilities>;
  notebooks: {
    list(): Promise<Notebook[]>;
    create(input?: { title?: string; description?: string; icon?: string }): Promise<Notebook>;
    get(id: ID): Promise<Notebook>;
    update(id: ID, patch: { title?: string; description?: string; icon?: string }): Promise<Notebook>;
    delete(id: ID): Promise<void>;
    /** Summary + suggested questions. Cached until the sources change. */
    guide(id: ID, opts?: { refresh?: boolean }): Promise<NotebookGuide>;
  };
  sources: {
    list(notebookId: ID): Promise<Source[]>;
    /** File bytes are uploaded as multipart. */
    add(notebookId: ID, input: SourceInput): Promise<Source>;
    /** Browser convenience: upload File objects from an <input type="file"> or a drop. */
    addFiles(notebookId: ID, files: File[]): Promise<Source[]>;
    get(id: ID): Promise<Source>;
    getText(id: ID): Promise<string>;
    delete(id: ID): Promise<void>;
  };
  chat: {
    history(notebookId: ID): Promise<ChatMessage[]>;
    clear(notebookId: ID): Promise<void>;
    /** Streams the grounded answer. The final event is `done` (or `error`). */
    stream(notebookId: ID, input: ChatInput, opts?: { signal?: AbortSignal }): AsyncIterable<ChatEvent>;
    send(notebookId: ID, input: ChatInput): Promise<ChatMessage>;
  };
  artifacts: {
    list(notebookId: ID): Promise<Artifact[]>;
    /** Returns immediately with a queued artifact; generation runs on the server. */
    create(notebookId: ID, request: ArtifactRequest): Promise<Artifact>;
    generate(notebookId: ID, request: ArtifactRequest, opts?: { onProgress?: (a: Artifact) => void }): Promise<Artifact>;
    get(id: ID): Promise<Artifact>;
    /** Polls until ready or errored. */
    wait(id: ID, opts?: { onProgress?: (a: Artifact) => void; intervalMs?: number }): Promise<Artifact>;
    /** URL of the rendered audio, usable as an <audio src>. */
    audioUrl(id: ID): string;
    getAudio(id: ID): Promise<{ data: Uint8Array; mimeType: string } | null>;
    delete(id: ID): Promise<void>;
  };
  live: {
    /** Exchange a WebRTC SDP offer for a GPT-Live session. The API key stays on the server. */
    createSession(notebookId: ID, request: LiveSessionRequest): Promise<LiveSession>;
    /** Answer a question the voice model delegated, grounded in the notebook's sources. */
    delegate(notebookId: ID, request: LiveDelegateRequest): Promise<LiveDelegateResult>;
  };
}

/** Thrown by the client for failed requests. `status` is the HTTP status. */
export class OvertoneError extends Error {
  status: number;
  code: string;
  constructor(message: string, opts: { status?: number; code?: string } = {}) {
    super(message);
    this.name = "OvertoneError";
    this.status = opts.status ?? 400;
    this.code = opts.code ?? "bad_request";
  }
}
