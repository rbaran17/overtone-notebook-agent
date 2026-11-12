/**
 * Live voice: a framework-free WebRTC session with GPT-Live.
 *
 * The browser sends an SDP offer to the Overtone backend, which returns the
 * answer for a GPT-Live session. Audio then flows peer-to-peer; the data
 * channel carries transcript deltas and "delegations", questions the voice
 * model hands to the backend to answer from the notebook's sources.
 *
 * No React and no globals are used directly: the peer connection, microphone
 * and client are injectable so the class can be tested with fakes.
 */

import type {
  Citation,
  LiveDelegateRequest,
  LiveMode,
  LiveSession,
  LiveSessionRequest,
  LiveTranscriptLine,
  OvertoneClient,
} from "./types";

export type LiveStatus = "idle" | "connecting" | "listening" | "thinking" | "closing" | "ended" | "error";

export interface LiveVoiceState {
  status: LiveStatus;
  /** Conversation so far, oldest first. Consecutive speech fragments from the same speaker are merged into one line. */
  transcript: LiveTranscriptLine[];
  /** Citations returned by delegated answers, deduplicated by sourceId + quote, newest last. */
  citations: Citation[];
  muted: boolean;
  /** Set when status is "error" (and for non-fatal errors while the session continues). */
  error?: string;
  session?: LiveSession;
}

export interface LiveVoiceOptions {
  /** Anything with the client's `live` namespace: `createClient(...)` or a test double. */
  client: Pick<OvertoneClient, "live">;
  notebookId: string;
  mode?: LiveMode; // default "conversation"
  sourceIds?: string[];
  artifactId?: string; // podcast mode
  turnIndex?: number; // podcast mode
  voice?: string;
  onState?: (state: LiveVoiceState) => void;
  /** Receives the remote audio stream so the app can attach it to an <audio> element. */
  onRemoteStream?: (stream: MediaStream) => void;
  /** Injectable for tests. Defaults: `new RTCPeerConnection()`, `navigator.mediaDevices.getUserMedia({ audio: true })`. */
  createPeerConnection?: () => RTCPeerConnection;
  getUserMedia?: () => Promise<MediaStream>;
}

/** The longest commentary the voice model receives in one event (characters, roughly 500 tokens). */
export const COMMENTARY_CHUNK_LIMIT = 1200;

const CHANNEL_LABEL = "oai-events";
const DELEGATE_DELAY_MS = 350;
const TRANSCRIPT_CONTEXT_LINES = 16;
const ICE_TIMEOUT_MS = 10_000;
const CONNECT_TIMEOUT_MS = 30_000;
const CLOSE_TIMEOUT_MS = 3_000;
const CANNOT_ANSWER = "I couldn't look that up just now.";
const CONNECTION_LOST = "The live connection was lost.";

type Timer = ReturnType<typeof setTimeout>;

/** The fields of server events this class reads. Anything else is ignored. */
interface ServerEvent {
  type?: unknown;
  delta?: unknown;
  delegation?: { id?: unknown } | null;
  error?: { message?: unknown } | null;
}

export class LiveVoiceSession {
  private readonly opts: LiveVoiceOptions;
  private readonly createConnection: () => RTCPeerConnection;
  private readonly requestMicrophone: () => Promise<MediaStream>;

  private current: LiveVoiceState;
  private pc: RTCPeerConnection | null = null;
  private channel: RTCDataChannel | null = null;
  private mic: MediaStream | null = null;

  /** Delegations waiting for their 350 ms settle delay, or for the backend. */
  private readonly inFlight = new Set<string>();
  private readonly pendingTimers = new Set<Timer>();

  private stopping: Promise<void> | null = null;
  private stopRequested = false;
  /** True once the session has reached "ended" or "error". Nothing is reported after that. */
  private closed = false;
  private resolveClose: (() => void) | null = null;
  private resolveReady: (() => void) | null = null;
  private eventCounter = 0;

  constructor(options: LiveVoiceOptions) {
    this.opts = options;
    this.createConnection = options.createPeerConnection ?? (() => new RTCPeerConnection());
    this.requestMicrophone =
      options.getUserMedia ??
      (() => {
        if (typeof navigator === "undefined" || !navigator.mediaDevices) {
          return Promise.reject(new Error("Microphone access is not available in this browser."));
        }
        return navigator.mediaDevices.getUserMedia({ audio: true });
      });
    this.current = { status: "idle", transcript: [], citations: [], muted: false };
  }

  get state(): LiveVoiceState {
    return this.current;
  }

  /** Connects. Resolves once the session is listening, or has failed (status "error"). Never throws. */
  async start(): Promise<void> {
    if (this.current.status !== "idle" || this.closed) return;

    const ready = new Promise<void>((resolve) => {
      this.resolveReady = resolve;
    });
    this.update({ status: "connecting", error: undefined });

    try {
      await this.connect();
    } catch (err) {
      this.fail(describeStartError(err));
    }
    if (this.closed) return;

    const timer = setTimeout(() => this.fail("The live connection timed out."), CONNECT_TIMEOUT_MS);
    try {
      await ready;
    } finally {
      clearTimeout(timer);
    }
  }

  setMuted(muted: boolean): void {
    if (this.closed) return;
    if (this.mic) setTrackEnabled(this.mic, !muted);
    this.update({ muted });
  }

  /** Graceful close. Safe to call more than once and from any state. */
  stop(): Promise<void> {
    if (!this.stopping) this.stopping = this.performStop();
    return this.stopping;
  }

  /* ───────────────────────── Connection ───────────────────────── */

  private async connect(): Promise<void> {
    const stream = await this.requestMicrophone();
    if (this.closed) {
      stopTracks(stream);
      return;
    }
    this.mic = stream;
    if (this.current.muted) setTrackEnabled(stream, false);

    const pc = this.createConnection();
    this.pc = pc;
    for (const track of stream.getTracks()) pc.addTrack(track, stream);
    pc.ontrack = (event) => {
      const remote = event.streams[0];
      if (remote) this.opts.onRemoteStream?.(remote);
    };
    pc.onconnectionstatechange = () => this.onConnectionState(pc);

    // The channel must exist before the offer is created, or the offer has no data section.
    this.attachChannel(pc.createDataChannel(CHANNEL_LABEL));

    await pc.setLocalDescription(await pc.createOffer());
    await this.waitForIceGathering(pc);
    if (this.closed) return;

    const local = pc.localDescription;
    if (!local) throw new Error("The browser did not produce a local offer.");

    const request: LiveSessionRequest = {
      sdp: local.sdp,
      mode: this.mode,
      sourceIds: this.opts.sourceIds,
      artifactId: this.opts.artifactId,
      turnIndex: this.opts.turnIndex,
      voice: this.opts.voice,
    };
    const session = await this.opts.client.live.createSession(this.opts.notebookId, request);
    if (this.closed) return;

    this.update({ session });
    await pc.setRemoteDescription({ type: "answer", sdp: session.sdp });
  }

  private waitForIceGathering(pc: RTCPeerConnection): Promise<void> {
    if (pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise<void>((resolve) => {
      const done = () => {
        pc.removeEventListener("icegatheringstatechange", onChange);
        clearTimeout(timer);
        resolve();
      };
      const onChange = () => {
        if (pc.iceGatheringState === "complete") done();
      };
      // Plain timer, not tracked: teardown must not leave this promise pending.
      const timer = setTimeout(done, ICE_TIMEOUT_MS);
      pc.addEventListener("icegatheringstatechange", onChange);
    });
  }

  private attachChannel(channel: RTCDataChannel): void {
    this.channel = channel;
    channel.onmessage = (event: MessageEvent) => this.handleMessage(event.data);
  }

  private onConnectionState(pc: RTCPeerConnection): void {
    if (this.closed || pc !== this.pc) return;
    switch (pc.connectionState) {
      case "connected":
        if (this.current.status === "connecting") this.becomeListening();
        break;
      case "failed":
      case "disconnected":
      case "closed":
        if (this.current.status === "closing") return;
        this.fail(CONNECTION_LOST);
        break;
      default:
        break;
    }
  }

  /* ───────────────────────── Server events ───────────────────────── */

  private handleMessage(raw: unknown): void {
    if (this.closed || typeof raw !== "string") return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return;
    }
    if (!parsed || typeof parsed !== "object") return;
    const msg = parsed as ServerEvent;

    switch (msg.type) {
      case "session.started":
        this.becomeListening();
        break;
      case "session.input_transcript.delta":
        this.appendTranscript("user", msg.delta);
        break;
      case "session.output_transcript.delta":
        this.appendTranscript("assistant", msg.delta);
        break;
      case "session.delegation.created": {
        const id = msg.delegation?.id;
        if (typeof id === "string" && id) this.onDelegation(id);
        break;
      }
      case "error": {
        const message = msg.error?.message;
        this.update({
          error: typeof message === "string" && message ? message : "The live voice service reported an error.",
        });
        break;
      }
      case "session.closed":
        if (this.resolveClose) this.resolveClose();
        else this.finish("ended");
        break;
      default:
        break;
    }
  }

  private becomeListening(): void {
    if (this.current.status === "connecting") this.update({ status: "listening" });
    this.resolveReady?.();
  }

  private appendTranscript(role: LiveTranscriptLine["role"], delta: unknown): void {
    if (typeof delta !== "string" || delta === "") return;
    const lines = this.current.transcript;
    const last = lines[lines.length - 1];
    const transcript =
      last && last.role === role
        ? [...lines.slice(0, -1), { role, text: last.text + delta }]
        : [...lines, { role, text: delta }];
    this.update({ transcript });
  }

  /* ───────────────────────── Delegation ───────────────────────── */

  private onDelegation(id: string): void {
    this.inFlight.add(id);
    if (this.current.status === "listening") this.update({ status: "thinking" });
    // Give trailing transcript fragments a moment to arrive before the question is read.
    this.schedule(() => void this.runDelegation(id), DELEGATE_DELAY_MS);
  }

  private async runDelegation(id: string): Promise<void> {
    try {
      if (this.closed || this.stopRequested) return;
      const request: LiveDelegateRequest = {
        mode: this.mode,
        transcript: recentTranscript(this.current.transcript),
        sourceIds: this.opts.sourceIds,
        artifactId: this.opts.artifactId,
      };
      const result = await this.opts.client.live.delegate(this.opts.notebookId, request);
      if (this.closed) return;
      this.mergeCitations(result.citations ?? []);
      this.sendAnswer(id, result.answer);
    } catch (err) {
      if (this.closed) return;
      this.sendAnswer(id, CANNOT_ANSWER);
      this.update({ error: err instanceof Error && err.message ? err.message : String(err) });
    } finally {
      this.inFlight.delete(id);
      if (this.inFlight.size === 0 && this.current.status === "thinking") this.update({ status: "listening" });
    }
  }

  private sendAnswer(delegationId: string, answer: string): void {
    const chunks = splitForCommentary(answer);
    for (const content of chunks.length > 0 ? chunks : [CANNOT_ANSWER]) {
      this.send({
        type: "session.commentary.append",
        event_id: this.nextEventId(),
        delegation_id: delegationId,
        content,
      });
    }
  }

  private mergeCitations(incoming: Citation[]): void {
    const next = [...this.current.citations];
    for (const citation of incoming) {
      const known = next.some((c) => c.sourceId === citation.sourceId && c.quote === citation.quote);
      if (!known) next.push(citation);
    }
    if (next.length !== this.current.citations.length) this.update({ citations: next });
  }

  /* ───────────────────────── Shutdown ───────────────────────── */

  private async performStop(): Promise<void> {
    if (this.closed) return;
    this.stopRequested = true;

    const connected = this.current.status === "listening" || this.current.status === "thinking";
    if (connected) {
      this.update({ status: "closing" });
      // Stop sending audio at once; the tracks are released when the close completes.
      if (this.mic) setTrackEnabled(this.mic, false);
      const acknowledged = new Promise<void>((resolve) => {
        this.resolveClose = resolve;
      });
      this.send({ type: "session.close" });
      await withTimeout(acknowledged, CLOSE_TIMEOUT_MS);
      this.resolveClose = null;
    }
    this.finish("ended");
  }

  /** Moves to a terminal status once: tears everything down, then reports the final state. */
  private finish(status: "ended" | "error", patch: Partial<LiveVoiceState> = {}): void {
    if (this.closed) return;
    this.closed = true;
    this.teardown();
    this.current = { ...this.current, ...patch, status };
    this.opts.onState?.(this.current);
    this.resolveReady?.();
  }

  private fail(message: string): void {
    this.finish("error", { error: message });
  }

  private teardown(): void {
    for (const timer of this.pendingTimers) clearTimeout(timer);
    this.pendingTimers.clear();
    if (this.mic) {
      stopTracks(this.mic);
      this.mic = null;
    }
    const channel = this.channel;
    this.channel = null;
    channel?.close();
    const pc = this.pc;
    this.pc = null;
    pc?.close();
  }

  /* ───────────────────────── Helpers ───────────────────────── */

  private get mode(): LiveMode {
    return this.opts.mode ?? "conversation";
  }

  /** Applies a patch and reports the new state. Ignored once the session is closed. */
  private update(patch: Partial<LiveVoiceState>): void {
    if (this.closed) return;
    this.current = { ...this.current, ...patch };
    this.opts.onState?.(this.current);
  }

  private send(event: object): void {
    const channel = this.channel;
    if (channel && channel.readyState === "open") channel.send(JSON.stringify(event));
  }

  private nextEventId(): string {
    this.eventCounter += 1;
    return `evt_${Date.now().toString(36)}_${this.eventCounter}`;
  }

  private schedule(fn: () => void, ms: number): void {
    const timer: Timer = setTimeout(() => {
      this.pendingTimers.delete(timer);
      fn();
    }, ms);
    this.pendingTimers.add(timer);
  }
}

/**
 * Splits a delegated answer into commentary events of at most COMMENTARY_CHUNK_LIMIT
 * characters, breaking after sentences where possible and inside a sentence at a
 * space when it must. The pieces concatenate back to the original text exactly.
 * Returns an empty array for blank text.
 */
export function splitForCommentary(text: string, limit: number = COMMENTARY_CHUNK_LIMIT): string[] {
  if (text.trim() === "") return [];
  if (text.length <= limit) return [text];

  const sentences = text.match(/[^.!?]*[.!?]+\s*|[^.!?]+$/g) ?? [text];
  const pieces = sentences.flatMap((sentence) => hardSplit(sentence, limit));

  const chunks: string[] = [];
  let current = "";
  for (const piece of pieces) {
    if (current.length + piece.length <= limit) {
      current += piece;
    } else {
      chunks.push(current);
      current = piece;
    }
  }
  if (current) chunks.push(current);
  return chunks;
}

/** Cuts a piece longer than `limit` at its last whitespace before the limit (or at the limit). */
function hardSplit(piece: string, limit: number): string[] {
  const out: string[] = [];
  let rest = piece;
  while (rest.length > limit) {
    let cut = limit;
    for (let i = limit - 1; i > 0; i--) {
      if (/\s/.test(rest[i])) {
        cut = i + 1;
        break;
      }
    }
    out.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  if (rest) out.push(rest);
  return out;
}

function recentTranscript(lines: LiveTranscriptLine[]): LiveTranscriptLine[] {
  return lines
    .map((line) => ({ role: line.role, text: line.text.trim() }))
    .filter((line) => line.text.length > 0)
    .slice(-TRANSCRIPT_CONTEXT_LINES);
}

function describeStartError(err: unknown): string {
  if (typeof err === "object" && err !== null && (err as { name?: unknown }).name === "NotAllowedError") {
    return "Microphone access was blocked. Allow the microphone for this site and try again.";
  }
  if (err instanceof Error && err.message) return err.message;
  if (typeof err === "string" && err) return err;
  return "The live session could not be started.";
}

function stopTracks(stream: MediaStream): void {
  for (const track of stream.getTracks()) track.stop();
}

function setTrackEnabled(stream: MediaStream, enabled: boolean): void {
  for (const track of stream.getTracks()) track.enabled = enabled;
}

/** Resolves after `ms` or when `promise` settles, whichever comes first. */
function withTimeout(promise: Promise<void>, ms: number): Promise<void> {
  let timer: Timer | undefined;
  const timeout = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}
