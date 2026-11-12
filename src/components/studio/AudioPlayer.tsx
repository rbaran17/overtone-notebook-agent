"use client";

import { Download, Mic, Pause, Play, RotateCcw, RotateCw, SkipBack, SkipForward, X } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { LivePanel } from "@/components/live/LivePanel";
import { useLiveVoice, type LiveVoiceControls } from "@/components/live/useLiveVoice";
import { Button, EmptyState, Eyebrow, IconButton, Notice, cn } from "@/components/ui";
import { api, type Artifact, type AudioScript } from "@/lib/api";
import { FORMAT_LABEL } from "./kinds";
import { SPEECH_RATES, useSpeechPlayer } from "./useSpeechPlayer";
import {
  downloadText,
  estimateTurnFromProgress,
  formatTime,
  hostName,
  slugify,
  transcriptToText,
  turnStartFraction,
} from "./util";

const RENDERED_RATES = [1, 1.25, 1.5, 2, 0.75];

export interface AudioPlayerProps {
  artifact: Artifact;
  onClose: () => void;
  /** Whether live voice is configured on the server; enables "Join the conversation". */
  liveAvailable: boolean;
}

/** What a player needs to join the episode live. Owned by AudioPlayer. */
interface JoinControls {
  live: LiveVoiceControls;
  joined: boolean;
  /** Starts a listener session at `turnIndex`. The player must pause its own playback first. */
  start: (turnIndex: number) => void;
  /** Hides the live panel. The player resumes its own playback. */
  leave: () => void;
}

/** Inline "now playing" card. Rendered MP3 when present, browser voices otherwise. */
export function AudioPlayer({ artifact, onClose, liveAvailable }: AudioPlayerProps) {
  const script = artifact.script;
  const format = artifact.request.kind === "audio_overview" ? artifact.request.options?.format : undefined;

  // Closing the player unmounts it, which ends any live session through the hook.
  const liveAudioRef = useRef<HTMLAudioElement>(null);
  const live = useLiveVoice(liveAudioRef);
  const [joined, setJoined] = useState(false);
  const join: JoinControls | null =
    liveAvailable && script
      ? {
          live,
          joined,
          start: (turnIndex) => {
            setJoined(true);
            void live.start({
              client: api,
              notebookId: artifact.notebookId,
              mode: "podcast",
              artifactId: artifact.id,
              turnIndex,
            });
          },
          leave: () => setJoined(false),
        }
      : null;

  return (
    <section aria-label="Now playing" className="flex flex-col gap-4 rounded-2xl border border-line bg-surface p-4 shadow-panel">
      <header className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <Eyebrow>{format ? FORMAT_LABEL[format] : "Audio overview"}</Eyebrow>
          <h3 className="mt-1 font-display text-[22px] leading-tight text-ink">{script?.title ?? artifact.title}</h3>
          {script?.summary && <p className="mt-1 text-[13px] leading-relaxed text-ink-2">{script.summary}</p>}
        </div>
        <IconButton label="Close player" onClick={onClose} className="-mr-1 -mt-1">
          <X className="size-4" />
        </IconButton>
      </header>

      <audio ref={liveAudioRef} autoPlay className="hidden" />

      {!script ? (
        <EmptyState title="No script to play">This overview has no transcript yet.</EmptyState>
      ) : artifact.audio ? (
        <RenderedPlayer script={script} artifactId={artifact.id} join={join} />
      ) : (
        <SpeechPlayer script={script} audioError={artifact.audioError} join={join} />
      )}
    </section>
  );
}

/* ───────────────────────── Join the conversation ───────────────────────── */

function JoinButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title="Join the conversation"
      className="inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[12px] font-medium text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
    >
      <Mic className="size-3.5" aria-hidden />
      Join
    </button>
  );
}

/** The live panel plus "Back to the episode" once the call is over. */
function JoinBody({ join, onBack }: { join: JoinControls; onBack: () => void }) {
  const { state } = join.live;
  const over = state.status === "ended" || state.status === "error";
  return (
    <div className="flex flex-col gap-3 rounded-2xl border border-line">
      <LivePanel
        state={state}
        title="You joined the episode"
        hint="The episode is paused while you talk."
        onStop={() => void join.live.stop()}
        onToggleMute={() => join.live.setMuted(!state.muted)}
      />
      {over && (
        <div className="px-3 pb-3">
          <Button variant="primary" className="w-full" onClick={onBack}>
            Back to the episode
          </Button>
        </div>
      )}
    </div>
  );
}

/* ───────────────────────── Rendered MP3 ───────────────────────── */

function RenderedPlayer({
  script,
  artifactId,
  join,
}: {
  script: AudioScript;
  artifactId: string;
  join: JoinControls | null;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [now, setNow] = useState(0);
  const [duration, setDuration] = useState(Number.NaN);
  const [rateIndex, setRateIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const src = api.artifacts.audioUrl(artifactId);
  const downloadHref = `${src}${src.includes("?") ? "&" : "?"}download=1`;
  const seekable = Number.isFinite(duration) && duration > 0;
  const turnIndex = estimateTurnFromProgress(script.turns, seekable ? now / duration : 0);
  const joined = join?.joined ?? false;

  function joinEpisode() {
    audioRef.current?.pause();
    join?.start(turnIndex);
  }

  function backToEpisode() {
    join?.leave();
    audioRef.current?.play().catch(() => setError("The browser blocked playback. Press play again."));
  }

  function readDuration(audio: HTMLAudioElement) {
    setDuration(Number.isFinite(audio.duration) ? audio.duration : Number.NaN);
  }

  function seekTo(seconds: number) {
    const audio = audioRef.current;
    if (!audio) return;
    const max = seekable ? duration : Number.POSITIVE_INFINITY;
    const target = Math.min(max, Math.max(0, seconds));
    audio.currentTime = target;
    setNow(target);
  }

  function togglePlay() {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      setError(null);
      audio.play().catch(() => setError("The browser blocked playback. Press play again."));
    } else {
      audio.pause();
    }
  }

  function cycleRate() {
    const next = (rateIndex + 1) % RENDERED_RATES.length;
    setRateIndex(next);
    if (audioRef.current) audioRef.current.playbackRate = RENDERED_RATES[next];
  }

  return (
    <div className="flex flex-col gap-4">
      <audio
        ref={audioRef}
        preload="metadata"
        src={src}
        className="hidden"
        onLoadedMetadata={(e) => readDuration(e.currentTarget)}
        onDurationChange={(e) => readDuration(e.currentTarget)}
        onTimeUpdate={(e) => setNow(e.currentTarget.currentTime)}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onError={() => {
          setPlaying(false);
          setError("This audio could not be loaded. Try again later, or download the file.");
        }}
      />

      <div className="flex items-center justify-center gap-5">
        <SkipButton
          label="Back 15 seconds"
          icon={<RotateCcw className="size-5" />}
          seconds="15"
          disabled={joined}
          onClick={() => seekTo(now - 15)}
        />
        <button
          type="button"
          onClick={togglePlay}
          disabled={joined}
          aria-label={playing ? "Pause" : "Play"}
          className="flex size-14 items-center justify-center rounded-full bg-accent text-accent-ink shadow-panel transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
        >
          {playing ? <Pause className="size-6" fill="currentColor" /> : <Play className="size-6 translate-x-px" fill="currentColor" />}
        </button>
        <SkipButton
          label="Forward 30 seconds"
          icon={<RotateCw className="size-5" />}
          seconds="30"
          disabled={joined}
          onClick={() => seekTo(now + 30)}
        />
      </div>

      <div>
        <input
          type="range"
          aria-label="Seek"
          min={0}
          max={seekable ? duration : 1}
          step={0.1}
          value={seekable ? Math.min(now, duration) : 0}
          disabled={!seekable || joined}
          onChange={(e) => seekTo(Number(e.target.value))}
          className="h-1.5 w-full cursor-pointer accent-accent disabled:cursor-not-allowed disabled:opacity-40"
        />
        <div className="mt-1 flex justify-between font-mono text-[11px] tabular-nums text-ink-3">
          <span>{formatTime(now)}</span>
          <span>{formatTime(duration)}</span>
        </div>
      </div>

      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={cycleRate}
          aria-label={`Playback speed ${RENDERED_RATES[rateIndex]} times, change speed`}
          className="h-8 min-w-14 rounded-lg px-2.5 font-mono text-[12px] text-ink-2 hover:bg-surface-2 hover:text-ink"
        >
          {RENDERED_RATES[rateIndex]}×
        </button>
        {join && !joined && <JoinButton onClick={joinEpisode} />}
        <a
          href={downloadHref}
          download
          aria-label="Download audio"
          title="Download audio"
          className="inline-flex size-8 items-center justify-center rounded-lg text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <Download className="size-4" />
        </a>
      </div>

      {error && <Notice onDismiss={() => setError(null)}>{error}</Notice>}

      {join?.joined && <JoinBody join={join} onBack={backToEpisode} />}

      <Transcript
        script={script}
        current={turnIndex}
        disabled={joined}
        onSelect={(index) => {
          if (seekable && !joined) seekTo(turnStartFraction(script.turns, index) * duration);
        }}
      />
    </div>
  );
}

function SkipButton({
  label,
  icon,
  seconds,
  disabled,
  onClick,
}: {
  label: string;
  icon: ReactNode;
  seconds: string;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <IconButton label={label} size="md" disabled={disabled} onClick={onClick} className="relative size-10">
      {icon}
      <span className="pointer-events-none absolute inset-0 flex items-center justify-center pt-px font-mono text-[8px] font-semibold">
        {seconds}
      </span>
    </IconButton>
  );
}

/* ───────────────────────── Browser voices ───────────────────────── */

function SpeechPlayer({
  script,
  audioError,
  join,
}: {
  script: AudioScript;
  audioError?: string;
  join: JoinControls | null;
}) {
  const speech = useSpeechPlayer(script);
  const total = script.turns.length;
  const turn = Math.min(speech.currentTurn, Math.max(0, total - 1));
  const canControl = speech.supported && total > 0;
  const progress = total ? ((turn + 1) / total) * 100 : 0;
  const rateIndex = SPEECH_RATES.indexOf(speech.rate);
  const joined = join?.joined ?? false;
  // The turn to restart from when the listener returns.
  const [joinTurn, setJoinTurn] = useState(0);

  function joinEpisode() {
    speech.pause();
    setJoinTurn(turn);
    join?.start(turn);
  }

  function backToEpisode() {
    join?.leave();
    speech.jumpTo(joinTurn);
  }

  return (
    <div className="flex flex-col gap-4">
      {speech.supported ? (
        <Notice tone="info">
          Playing with your browser&apos;s voices. Add an OPENAI_API_KEY or ELEVENLABS_API_KEY on the server for studio-quality audio.
        </Notice>
      ) : (
        <Notice tone="info">This browser cannot read aloud. The transcript is still available below.</Notice>
      )}
      {audioError && <Notice tone="info">Audio rendering failed: {audioError}</Notice>}

      <div className="flex items-center justify-center gap-3">
        <IconButton
          label="Previous turn"
          size="md"
          disabled={!canControl || joined || turn <= 0}
          onClick={() => speech.jumpTo(turn - 1)}
        >
          <SkipBack className="size-5" />
        </IconButton>
        <button
          type="button"
          disabled={!canControl || joined}
          onClick={() => (speech.playing ? speech.pause() : speech.play())}
          aria-label={speech.playing ? "Pause" : "Play"}
          className="flex size-14 items-center justify-center rounded-full bg-accent text-accent-ink shadow-panel transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
        >
          {speech.playing ? <Pause className="size-6" fill="currentColor" /> : <Play className="size-6 translate-x-px" fill="currentColor" />}
        </button>
        <IconButton
          label="Next turn"
          size="md"
          disabled={!canControl || joined || turn >= total - 1}
          onClick={() => speech.jumpTo(turn + 1)}
        >
          <SkipForward className="size-5" />
        </IconButton>
      </div>

      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="font-mono text-[11px] tabular-nums text-ink-3">
            Turn {total ? turn + 1 : 0} of {total}
          </div>
          <div
            role="progressbar"
            aria-label="Reading progress"
            aria-valuemin={0}
            aria-valuemax={total}
            aria-valuenow={total ? turn + 1 : 0}
            className="mt-1.5 h-1 overflow-hidden rounded-full bg-surface-2"
          >
            <div className="h-full rounded-full bg-accent transition-[width] duration-300" style={{ width: `${progress}%` }} />
          </div>
        </div>
        {join && !joined && <JoinButton onClick={joinEpisode} />}
        <button
          type="button"
          disabled={!speech.supported}
          onClick={() => speech.setRate(SPEECH_RATES[(rateIndex + 1) % SPEECH_RATES.length])}
          aria-label={`Reading speed ${speech.rate} times, change speed`}
          className="h-8 min-w-14 shrink-0 rounded-lg px-2.5 font-mono text-[12px] text-ink-2 hover:bg-surface-2 hover:text-ink disabled:opacity-40"
        >
          {speech.rate}×
        </button>
      </div>

      {join?.joined && <JoinBody join={join} onBack={backToEpisode} />}

      <Transcript
        script={script}
        current={turn}
        disabled={joined}
        onSelect={(index) => {
          if (!joined) speech.jumpTo(index);
        }}
      />
    </div>
  );
}

/* ───────────────────────── Transcript ───────────────────────── */

function Transcript({
  script,
  current,
  disabled = false,
  onSelect,
}: {
  script: AudioScript;
  current: number;
  disabled?: boolean;
  onSelect: (index: number) => void;
}) {
  const listRef = useRef<HTMLOListElement>(null);

  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>('[data-current="true"]');
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [current]);

  return (
    <div className="flex flex-col gap-2 border-t border-line pt-3">
      <div className="flex items-center justify-between">
        <Eyebrow>Transcript</Eyebrow>
        <button
          type="button"
          onClick={() => downloadText(`${slugify(script.title)}.txt`, transcriptToText(script), "text/plain;charset=utf-8")}
          className="inline-flex h-7 items-center gap-1.5 rounded-lg px-2 text-[12px] font-medium text-ink-2 hover:bg-surface-2 hover:text-ink"
        >
          <Download className="size-3.5" aria-hidden />
          Download transcript
        </button>
      </div>
      <ol ref={listRef} className="scroll-quiet flex max-h-[40vh] flex-col gap-1 overflow-y-auto pr-1">
        {script.turns.map((turn, index) => {
          const isCurrent = index === current;
          const isHostOne = turn.speaker === 0;
          return (
            <li key={index}>
              <button
                type="button"
                data-current={isCurrent ? "true" : undefined}
                aria-current={isCurrent ? "true" : undefined}
                disabled={disabled}
                onClick={() => onSelect(index)}
                className={cn(
                  "w-full rounded-xl px-3 py-2 text-left transition-colors disabled:cursor-not-allowed",
                  isCurrent ? "bg-accent-soft text-ink" : "text-ink-2 hover:bg-surface-2",
                )}
              >
                <span
                  className={cn(
                    "block font-mono text-[11px] uppercase tracking-[0.08em]",
                    isHostOne ? "text-accent" : "text-ink",
                  )}
                >
                  {hostName(script, turn.speaker)}
                </span>
                <span className="mt-0.5 block text-[14px] leading-relaxed">{turn.text}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
