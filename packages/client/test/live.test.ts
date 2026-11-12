import assert from "node:assert/strict";
import { describe, mock, test } from "node:test";
import {
  COMMENTARY_CHUNK_LIMIT,
  LiveVoiceSession,
  splitForCommentary,
  type LiveVoiceState,
} from "../src/live";
import type {
  Citation,
  LiveDelegateRequest,
  LiveDelegateResult,
  LiveMode,
  LiveSession,
  LiveSessionRequest,
  OvertoneClient,
} from "../src/types";

/* ───────────────────────────── Fakes ───────────────────────────── */

const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

class FakeTrack {
  enabled = true;
  stopped = 0;
  constructor(readonly kind: string = "audio") {}
  stop(): void {
    this.stopped += 1;
  }
}

class FakeStream {
  constructor(readonly tracks: FakeTrack[] = [new FakeTrack()]) {}
  getTracks(): FakeTrack[] {
    return this.tracks;
  }
}

class FakeChannel {
  readyState = "connecting";
  closed = false;
  sent: string[] = [];
  onmessage: ((event: { data: unknown }) => void) | null = null;
  constructor(readonly label: string) {}

  open(): void {
    this.readyState = "open";
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.closed = true;
    this.readyState = "closed";
  }
  /** Delivers a server event as the browser would: a JSON string (or a raw string when given one that is not an object). */
  receive(event: object | string): void {
    this.onmessage?.({ data: typeof event === "string" ? event : JSON.stringify(event) });
  }
  sentEvents(): Array<Record<string, unknown>> {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
  }
  sentOfType(type: string): Array<Record<string, unknown>> {
    return this.sentEvents().filter((e) => e.type === type);
  }
}

class FakePeer {
  connectionState = "new";
  iceGatheringState = "new";
  localDescription: { type: string; sdp: string } | null = null;
  remoteDescription: { type: string; sdp: string } | null = null;
  addedTracks: FakeTrack[] = [];
  channels: FakeChannel[] = [];
  closed = false;
  ontrack: ((event: { streams: unknown[] }) => void) | null = null;
  onconnectionstatechange: (() => void) | null = null;
  private iceListeners = new Set<() => void>();

  constructor(
    private readonly calls: string[],
    private readonly autoIce: boolean,
  ) {}

  addTrack(track: FakeTrack): void {
    this.calls.push("addTrack");
    this.addedTracks.push(track);
  }
  createDataChannel(label: string): FakeChannel {
    this.calls.push(`createDataChannel:${label}`);
    const channel = new FakeChannel(label);
    this.channels.push(channel);
    return channel;
  }
  async createOffer(): Promise<{ type: string; sdp: string }> {
    this.calls.push("createOffer");
    return { type: "offer", sdp: "offer-sdp" };
  }
  async setLocalDescription(desc: { type: string; sdp: string }): Promise<void> {
    this.calls.push("setLocalDescription");
    this.localDescription = { type: desc.type, sdp: desc.sdp };
    if (this.autoIce) this.setIceGathering("complete");
  }
  async setRemoteDescription(desc: { type: string; sdp: string }): Promise<void> {
    this.calls.push(`setRemoteDescription:${desc.type}`);
    this.remoteDescription = desc;
  }
  addEventListener(type: string, fn: () => void): void {
    if (type === "icegatheringstatechange") this.iceListeners.add(fn);
  }
  removeEventListener(type: string, fn: () => void): void {
    if (type === "icegatheringstatechange") this.iceListeners.delete(fn);
  }
  setIceGathering(state: string): void {
    this.iceGatheringState = state;
    for (const fn of [...this.iceListeners]) fn();
  }
  setConnectionState(state: string): void {
    this.connectionState = state;
    this.onconnectionstatechange?.();
  }
  close(): void {
    this.closed = true;
    this.calls.push("close");
  }
}

type CreateSessionFn = (notebookId: string, request: LiveSessionRequest) => Promise<LiveSession>;
type DelegateFn = (notebookId: string, request: LiveDelegateRequest) => Promise<LiveDelegateResult>;

function cite(sourceId: string, quote: string): Citation {
  return { index: 1, sourceId, sourceTitle: `Source ${sourceId}`, quote };
}

interface SetupOptions {
  autoIce?: boolean;
  mode?: LiveMode;
  artifactId?: string;
  turnIndex?: number;
  getUserMedia?: () => Promise<unknown>;
  createSession?: CreateSessionFn;
  delegate?: DelegateFn;
}

function setup(opts: SetupOptions = {}) {
  const calls: string[] = [];
  const peers: FakePeer[] = [];
  const states: LiveVoiceState[] = [];
  const remoteStreams: unknown[] = [];
  const mic = new FakeStream();

  const defaultCreateSession: CreateSessionFn = async (_notebookId, request) => {
    calls.push("createSession");
    return { sessionId: "sess_1", sdp: "answer-sdp", mode: request.mode ?? "conversation", model: "gpt-live-1", voice: "alloy" };
  };
  const createSession = mock.fn<CreateSessionFn>(opts.createSession ?? defaultCreateSession);
  const delegate = mock.fn<DelegateFn>(opts.delegate ?? (async () => ({ answer: "ok", citations: [] })));
  const client = { live: { createSession, delegate } } as unknown as Pick<OvertoneClient, "live">;

  const session = new LiveVoiceSession({
    client,
    notebookId: "nb1",
    mode: opts.mode,
    sourceIds: ["src1"],
    artifactId: opts.artifactId,
    turnIndex: opts.turnIndex,
    voice: "alloy",
    onState: (state) => states.push(state),
    onRemoteStream: (stream) => remoteStreams.push(stream),
    createPeerConnection: () => {
      const peer = new FakePeer(calls, opts.autoIce ?? true);
      peers.push(peer);
      return peer as unknown as RTCPeerConnection;
    },
    getUserMedia: (opts.getUserMedia ?? (async () => {
      calls.push("getUserMedia");
      return mic;
    })) as () => Promise<MediaStream>,
  });

  return {
    session,
    calls,
    peers,
    states,
    remoteStreams,
    mic,
    createSession,
    delegate,
    peer: () => peers[0],
    channel: (): FakeChannel => peers[0].channels[0],
    statuses: () => states.map((s) => s.status),
  };
}

type Harness = ReturnType<typeof setup>;

/** Starts a session and completes the handshake with session.started. */
async function connect(h: Harness): Promise<void> {
  const started = h.session.start();
  await flush();
  h.channel().open();
  h.channel().receive({ type: "session.started" });
  await started;
}

/** Status sequence with consecutive repeats collapsed. */
function distinctStatuses(h: Harness): string[] {
  return h.statuses().filter((status, i, all) => i === 0 || status !== all[i - 1]);
}

/* ───────────────────────── Setup protocol ───────────────────────── */

describe("LiveVoiceSession: connecting", () => {
  test("follows the protocol order and passes the local SDP and options to createSession", async () => {
    const h = setup({ artifactId: "art1", turnIndex: 3 });
    const started = h.session.start();
    await flush();

    assert.deepEqual(h.calls, [
      "getUserMedia",
      "addTrack",
      "createDataChannel:oai-events",
      "createOffer",
      "setLocalDescription",
      "createSession",
      "setRemoteDescription:answer",
    ]);

    const [notebookId, request] = h.createSession.mock.calls[0].arguments;
    assert.equal(notebookId, "nb1");
    assert.equal(request.sdp, "offer-sdp");
    assert.equal(request.mode, "conversation");
    assert.deepEqual(request.sourceIds, ["src1"]);
    assert.equal(request.artifactId, "art1");
    assert.equal(request.turnIndex, 3);
    assert.equal(request.voice, "alloy");

    assert.deepEqual(h.peer().remoteDescription, { type: "answer", sdp: "answer-sdp" });
    assert.equal(h.peer().addedTracks[0], h.mic.tracks[0]);

    const remote = { id: "remote" };
    h.peer().ontrack?.({ streams: [remote] });
    assert.deepEqual(h.remoteStreams, [remote]);

    h.channel().receive({ type: "session.started" });
    await started;
    assert.equal(h.session.state.status, "listening");
    assert.equal(h.session.state.session?.sessionId, "sess_1");
  });

  test("moves connecting, then listening, publishing a new object for each change", async () => {
    const h = setup();
    await connect(h);
    assert.deepEqual(distinctStatuses(h), ["connecting", "listening"]);
    assert.notEqual(h.states[0], h.states[1]);
  });

  test("treats a connected peer connection as listening when session.started never arrives", async () => {
    const h = setup();
    const started = h.session.start();
    await flush();
    h.peer().setConnectionState("connected");
    await started;
    assert.equal(h.session.state.status, "listening");
  });

  test("waits for ICE gathering to complete, with a 10 second cap", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const h = setup({ autoIce: false });
    const started = h.session.start();
    await flush();
    assert.equal(h.createSession.mock.callCount(), 0);

    t.mock.timers.tick(9_999);
    await flush();
    assert.equal(h.createSession.mock.callCount(), 0);

    t.mock.timers.tick(1);
    await flush();
    assert.equal(h.createSession.mock.callCount(), 1);

    h.channel().receive({ type: "session.started" });
    await started;
    assert.equal(h.session.state.status, "listening");
  });

  test("does not hang when the connection never comes up", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const h = setup();
    const started = h.session.start();
    await flush();
    t.mock.timers.tick(30_000);
    await started;
    assert.equal(h.session.state.status, "error");
    assert.equal(h.session.state.error, "The live connection timed out.");
    assert.equal(h.mic.tracks[0].stopped, 1);
  });

  test("a blocked microphone sets an error and stops nothing it never had", async () => {
    const denied = Object.assign(new Error("Permission denied"), { name: "NotAllowedError" });
    const h = setup({ getUserMedia: async () => Promise.reject(denied) });
    await h.session.start();
    assert.equal(h.session.state.status, "error");
    assert.match(h.session.state.error ?? "", /Microphone access was blocked/);
    assert.equal(h.peers.length, 0);
    assert.equal(h.createSession.mock.callCount(), 0);
  });

  test("a createSession failure tears down and stops the microphone", async () => {
    const h = setup({
      createSession: async () => {
        throw new Error("Quota exceeded");
      },
    });
    await h.session.start();
    assert.equal(h.session.state.status, "error");
    assert.equal(h.session.state.error, "Quota exceeded");
    assert.equal(h.mic.tracks[0].stopped, 1);
    assert.equal(h.peer().closed, true);
    assert.equal(h.channel().closed, true);
  });

  test("a microphone that arrives after stop() is stopped at once", async () => {
    let resolveMic: (stream: FakeStream) => void = () => {};
    const h = setup({
      getUserMedia: () =>
        new Promise((resolve) => {
          resolveMic = resolve;
        }),
    });
    const started = h.session.start();
    await h.session.stop();
    assert.equal(h.session.state.status, "ended");

    const late = new FakeStream();
    resolveMic(late);
    await started;
    assert.equal(late.tracks[0].stopped, 1);
    assert.equal(h.peers.length, 0);
    assert.deepEqual(h.statuses(), ["connecting", "ended"]);
  });

  test("a failed peer connection is reported as a lost connection", async () => {
    const h = setup();
    await connect(h);
    h.peer().setConnectionState("failed");
    assert.equal(h.session.state.status, "error");
    assert.equal(h.session.state.error, "The live connection was lost.");
    assert.equal(h.mic.tracks[0].stopped, 1);
  });

  test("a disconnected peer connection is also treated as lost", async () => {
    const h = setup();
    await connect(h);
    h.peer().setConnectionState("disconnected");
    assert.equal(h.session.state.status, "error");
  });
});

/* ───────────────────────── Transcript and events ───────────────────────── */

describe("LiveVoiceSession: transcript and events", () => {
  test("merges fragments from one speaker and starts a new line on a role change", async () => {
    const h = setup();
    await connect(h);
    const ch = h.channel();
    ch.receive({ type: "session.input_transcript.delta", delta: "Hel" });
    ch.receive({ type: "session.input_transcript.delta", delta: "lo" });
    ch.receive({ type: "session.output_transcript.delta", delta: "Hi " });
    ch.receive({ type: "session.output_transcript.delta", delta: "there" });
    ch.receive({ type: "session.input_transcript.delta", delta: "Thanks" });
    assert.deepEqual(h.session.state.transcript, [
      { role: "user", text: "Hello" },
      { role: "assistant", text: "Hi there" },
      { role: "user", text: "Thanks" },
    ]);
  });

  test("ignores unknown types, unparsable messages and empty deltas", async () => {
    const h = setup();
    await connect(h);
    const before = h.states.length;
    const ch = h.channel();
    ch.receive("not json {");
    ch.receive({ type: "session.something.new" });
    ch.receive({});
    ch.receive({ type: "session.input_transcript.delta", delta: 42 });
    ch.receive({ type: "session.input_transcript.delta", delta: "" });
    assert.equal(h.states.length, before);
    assert.deepEqual(h.session.state.transcript, []);
  });

  test("error events are recorded while the session keeps running", async () => {
    const h = setup();
    await connect(h);
    h.channel().receive({ type: "error", error: { message: "Rate limited" } });
    assert.equal(h.session.state.error, "Rate limited");
    assert.equal(h.session.state.status, "listening");
  });

  test("a session.closed event from the server ends the session and stops the microphone", async () => {
    const h = setup();
    await connect(h);
    h.channel().receive({ type: "session.closed" });
    assert.equal(h.session.state.status, "ended");
    assert.equal(h.mic.tracks[0].stopped, 1);
    assert.equal(h.peer().closed, true);
  });

  test("nothing is reported once the session has ended", async () => {
    const h = setup();
    await connect(h);
    h.channel().receive({ type: "session.closed" });
    const count = h.states.length;
    h.channel().receive({ type: "session.input_transcript.delta", delta: "late" });
    h.channel().receive({ type: "error", error: { message: "late error" } });
    assert.equal(h.states.length, count);
  });
});

/* ───────────────────────── Delegation ───────────────────────── */

describe("LiveVoiceSession: delegation", () => {
  test("round trip: trimmed transcript in, commentary out, citations merged, back to listening", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const h = setup({
      delegate: async () => ({
        answer: "Ada was born in 1815.",
        citations: [cite("src1", "born in 1815"), cite("src1", "born in 1815")],
      }),
    });
    await connect(h);
    const ch = h.channel();
    ch.receive({ type: "session.input_transcript.delta", delta: "  What year was Ada born?  " });
    ch.receive({ type: "session.delegation.created", delegation: { id: "d1" } });
    assert.equal(h.session.state.status, "thinking");
    assert.equal(h.delegate.mock.callCount(), 0, "waits for trailing fragments first");

    t.mock.timers.tick(350);
    await flush();

    assert.equal(h.delegate.mock.callCount(), 1);
    const [notebookId, request] = h.delegate.mock.calls[0].arguments;
    assert.equal(notebookId, "nb1");
    assert.equal(request.mode, "conversation");
    assert.deepEqual(request.transcript, [{ role: "user", text: "What year was Ada born?" }]);
    assert.deepEqual(request.sourceIds, ["src1"]);

    const commentary = ch.sentOfType("session.commentary.append");
    assert.equal(commentary.length, 1);
    assert.equal(commentary[0].delegation_id, "d1");
    assert.equal(commentary[0].content, "Ada was born in 1815.");
    assert.equal(typeof commentary[0].event_id, "string");

    assert.equal(h.session.state.citations.length, 1);
    assert.equal(h.session.state.status, "listening");
  });

  test("passes the last 16 non-empty lines, trimmed", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const h = setup();
    await connect(h);
    const ch = h.channel();
    for (let i = 0; i < 20; i++) {
      ch.receive({
        type: i % 2 === 0 ? "session.input_transcript.delta" : "session.output_transcript.delta",
        delta: `  msg ${i}  `,
      });
    }
    ch.receive({ type: "session.delegation.created", delegation: { id: "d1" } });
    t.mock.timers.tick(350);
    await flush();

    const [, request] = h.delegate.mock.calls[0].arguments;
    assert.equal(request.transcript.length, 16);
    assert.equal(request.transcript[0].text, "msg 4");
    assert.equal(request.transcript[15].text, "msg 19");
    assert.equal(request.transcript[15].role, "assistant");
  });

  test("a long answer is split into commentary events of at most 1,200 characters with nothing lost", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const answer = Array.from({ length: 150 }, (_, i) => `Sentence number ${i} is about the sources.`).join(" ");
    assert.ok(answer.length > 1200 * 2);
    const h = setup({ delegate: async () => ({ answer, citations: [] }) });
    await connect(h);
    h.channel().receive({ type: "session.delegation.created", delegation: { id: "d9" } });
    t.mock.timers.tick(350);
    await flush();

    const commentary = h.channel().sentOfType("session.commentary.append");
    assert.ok(commentary.length >= 3);
    for (const event of commentary) {
      assert.equal(event.delegation_id, "d9");
      assert.ok(String(event.content).length <= 1200);
    }
    assert.equal(commentary.map((e) => e.content).join(""), answer);
    assert.equal(new Set(commentary.map((e) => e.event_id)).size, commentary.length, "event ids are unique");
  });

  test("a failed delegate call says so to the voice model and keeps the session going", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const h = setup({
      delegate: async () => {
        throw new Error("Backend unavailable");
      },
    });
    await connect(h);
    const ch = h.channel();
    ch.receive({ type: "session.delegation.created", delegation: { id: "d2" } });
    t.mock.timers.tick(350);
    await flush();

    const commentary = ch.sentOfType("session.commentary.append");
    assert.deepEqual(
      commentary.map((e) => [e.delegation_id, e.content]),
      [["d2", "I couldn't look that up just now."]],
    );
    assert.equal(h.session.state.error, "Backend unavailable");
    assert.equal(h.session.state.status, "listening");

    ch.receive({ type: "session.input_transcript.delta", delta: "still here" });
    assert.equal(h.session.state.transcript.at(-1)?.text, "still here");
  });

  test("overlapping delegations are handled independently and status waits for both", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const pending: Array<(result: LiveDelegateResult) => void> = [];
    const h = setup({
      delegate: () =>
        new Promise<LiveDelegateResult>((resolve) => {
          pending.push(resolve);
        }),
    });
    await connect(h);
    const ch = h.channel();
    ch.receive({ type: "session.delegation.created", delegation: { id: "d1" } });
    ch.receive({ type: "session.delegation.created", delegation: { id: "d2" } });
    t.mock.timers.tick(350);
    await flush();
    assert.equal(h.delegate.mock.callCount(), 2);

    pending[1]({ answer: "second", citations: [cite("src1", "Lovelace")] });
    await flush();
    assert.deepEqual(
      ch.sentOfType("session.commentary.append").map((e) => [e.delegation_id, e.content]),
      [["d2", "second"]],
    );
    assert.equal(h.session.state.status, "thinking");

    pending[0]({ answer: "first", citations: [cite("src1", "Lovelace"), cite("src1", "1815")] });
    await flush();
    assert.deepEqual(
      ch.sentOfType("session.commentary.append").map((e) => [e.delegation_id, e.content]),
      [
        ["d2", "second"],
        ["d1", "first"],
      ],
    );
    assert.equal(h.session.state.citations.length, 2, "duplicate citations are merged");
    assert.equal(h.session.state.status, "listening");
  });

  test("a result that arrives after the session ended is dropped silently", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    let resolveDelegate: (result: LiveDelegateResult) => void = () => {};
    const h = setup({
      delegate: () =>
        new Promise<LiveDelegateResult>((resolve) => {
          resolveDelegate = resolve;
        }),
    });
    await connect(h);
    h.channel().receive({ type: "session.delegation.created", delegation: { id: "d1" } });
    t.mock.timers.tick(350);
    await flush();

    const stopping = h.session.stop();
    h.channel().receive({ type: "session.closed" });
    await stopping;
    const count = h.states.length;

    resolveDelegate({ answer: "too late", citations: [cite("src1", "x")] });
    await flush();
    assert.equal(h.channel().sentOfType("session.commentary.append").length, 0);
    assert.equal(h.states.length, count);
    assert.equal(h.session.state.citations.length, 0);
  });
});

/* ───────────────────────── Mute and stop ───────────────────────── */

describe("LiveVoiceSession: mute and stop", () => {
  test("setMuted disables and re-enables the microphone tracks and is reflected in state", async () => {
    const h = setup();
    await connect(h);
    h.session.setMuted(true);
    assert.equal(h.mic.tracks[0].enabled, false);
    assert.equal(h.session.state.muted, true);
    h.session.setMuted(false);
    assert.equal(h.mic.tracks[0].enabled, true);
    assert.equal(h.session.state.muted, false);
  });

  test("a mute set before the microphone arrives applies as soon as it does", async () => {
    const h = setup();
    h.session.setMuted(true);
    await connect(h);
    assert.equal(h.mic.tracks[0].enabled, false);
  });

  test("stop() sends session.close, waits for session.closed, then releases everything; idempotent", async () => {
    const h = setup();
    await connect(h);
    const stopping = h.session.stop();
    assert.equal(h.session.state.status, "closing");
    assert.equal(h.mic.tracks[0].enabled, false, "audio stops being sent at once");
    assert.equal(h.channel().sentOfType("session.close").length, 1);

    h.channel().receive({ type: "session.closed" });
    await stopping;

    assert.equal(h.session.state.status, "ended");
    assert.equal(h.mic.tracks[0].stopped, 1);
    assert.equal(h.channel().closed, true);
    assert.equal(h.peer().closed, true);

    await h.session.stop();
    await h.session.stop();
    assert.equal(h.session.state.status, "ended");
    assert.equal(h.mic.tracks[0].stopped, 1);
    assert.equal(h.channel().sentOfType("session.close").length, 1);
    assert.equal(h.states.at(-1)?.status, "ended");
  });

  test("stop() gives up after 3 seconds if session.closed never arrives", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const h = setup();
    await connect(h);
    const stopping = h.session.stop();

    t.mock.timers.tick(2_999);
    await flush();
    assert.equal(h.session.state.status, "closing");

    t.mock.timers.tick(1);
    await stopping;
    assert.equal(h.session.state.status, "ended");
    assert.equal(h.mic.tracks[0].stopped, 1);
    assert.equal(h.peer().closed, true);
  });

  test("stop() before the connection is up ends at once without a close handshake", async () => {
    const h = setup();
    const started = h.session.start();
    await flush();
    await h.session.stop();
    await started;
    assert.equal(h.session.state.status, "ended");
    assert.equal(h.mic.tracks[0].stopped, 1);
    assert.equal(h.channel().sentOfType("session.close").length, 0);
  });

  test("a server-initiated session.closed while listening ends the session", async () => {
    const h = setup();
    await connect(h);
    h.channel().receive({ type: "session.closed" });
    assert.equal(h.session.state.status, "ended");
  });
});

/* ───────────────────────── Commentary splitting ───────────────────────── */

describe("splitForCommentary", () => {
  test("short answers are sent whole", () => {
    assert.deepEqual(splitForCommentary("Short answer."), ["Short answer."]);
  });

  test("blank answers produce no chunks", () => {
    assert.deepEqual(splitForCommentary("   "), []);
  });

  test("splits after sentences, keeping every character", () => {
    const text = Array.from({ length: 80 }, (_, i) => `Sentence ${i} ends here.`).join(" ");
    const chunks = splitForCommentary(text);
    assert.ok(chunks.length > 1);
    assert.ok(chunks.every((c) => c.length <= COMMENTARY_CHUNK_LIMIT));
    assert.equal(chunks.join(""), text);
    assert.ok(chunks[0].endsWith(" "), "a boundary keeps its space with the earlier chunk");
  });

  test("a run of text with no sentence breaks is cut at spaces", () => {
    const text = Array.from({ length: 400 }, (_, i) => `word${i}`).join(" ");
    const chunks = splitForCommentary(text);
    assert.ok(chunks.every((c) => c.length <= COMMENTARY_CHUNK_LIMIT));
    assert.equal(chunks.join(""), text);
  });

  test("a single token longer than the limit is cut hard", () => {
    const text = "x".repeat(2500);
    const chunks = splitForCommentary(text);
    assert.deepEqual(chunks.map((c) => c.length), [1200, 1200, 100]);
    assert.equal(chunks.join(""), text);
  });
});
