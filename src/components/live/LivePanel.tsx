"use client";

import { Mic, MicOff } from "lucide-react";
import { useEffect, useRef } from "react";
import type { Citation, LiveStatus, LiveVoiceState } from "@/lib/api";
import { Button, Eyebrow, IconButton, Notice, cn } from "@/components/ui";

export interface LivePanelProps {
  state: LiveVoiceState;
  onStop: () => void;
  onToggleMute: () => void;
  onOpenSource?: (sourceId: string, quote?: string) => void;
  title: string;
  hint?: string;
}

const STATUS_LABEL: Record<LiveStatus, string> = {
  idle: "Not connected",
  connecting: "Connecting…",
  listening: "Listening",
  thinking: "Looking through your sources…",
  closing: "Ending…",
  ended: "Ended",
  error: "Not connected",
};

/** One pill per source: the latest passage cited from it, in first-cited order. */
function citedSources(citations: Citation[]): Citation[] {
  const bySource = new Map<string, Citation>();
  for (const citation of citations) bySource.set(citation.sourceId, citation);
  return [...bySource.values()];
}

/** Compact view of a live voice session: status, transcript, cited sources and controls. */
export function LivePanel({ state, onStop, onToggleMute, onOpenSource, title, hint }: LivePanelProps) {
  const transcriptRef = useRef<HTMLDivElement>(null);
  const { status, transcript, muted } = state;

  // Keep the newest line in view.
  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [transcript]);

  const over = status === "ended" || status === "error";
  const busy = status === "listening" || status === "thinking";
  const animate = busy && !muted;
  const label = muted && status === "listening" ? "Microphone muted" : STATUS_LABEL[status];
  const sources = citedSources(state.citations);

  return (
    <section aria-label={title} className="flex flex-col gap-3 p-3">
      <h3 className="text-sm font-semibold text-ink">{title}</h3>

      <div className="flex items-center gap-2.5 text-[13px] text-ink-2">
        <span className={cn("ot-bars text-accent", !animate && "paused")} aria-hidden>
          <i />
          <i />
          <i />
          <i />
        </span>
        <span role="status">{label}</span>
      </div>

      {state.error && <Notice>{state.error}</Notice>}

      <div
        ref={transcriptRef}
        className="scroll-quiet flex max-h-[40vh] flex-col gap-2.5 overflow-y-auto pr-1"
      >
        {transcript.length === 0 ? (
          <p className="text-[13px] text-ink-3">The conversation will appear here.</p>
        ) : (
          <ol className="flex flex-col gap-2.5">
            {transcript.map((line, index) =>
              line.role === "user" ? (
                <li key={index} className="flex justify-end">
                  <span className="max-w-[85%] rounded-2xl bg-surface-2 px-3 py-1.5 text-[13px] leading-relaxed text-ink">
                    {line.text}
                  </span>
                </li>
              ) : (
                <li key={index} className="whitespace-pre-wrap text-[14px] leading-relaxed text-ink">
                  {line.text}
                </li>
              ),
            )}
          </ol>
        )}
      </div>

      {sources.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <Eyebrow>Cited</Eyebrow>
          <div className="flex flex-wrap gap-1.5">
            {sources.map((citation) =>
              onOpenSource ? (
                <button
                  key={citation.sourceId}
                  type="button"
                  onClick={() => onOpenSource(citation.sourceId, citation.quote)}
                  title={citation.sourceTitle}
                  className="max-w-full truncate rounded-full border border-line bg-surface px-2.5 py-1 text-[12px] text-ink-2 transition-colors hover:border-line-strong hover:text-ink"
                >
                  {citation.sourceTitle}
                </button>
              ) : (
                <span
                  key={citation.sourceId}
                  title={citation.sourceTitle}
                  className="max-w-full truncate rounded-full border border-line bg-surface px-2.5 py-1 text-[12px] text-ink-2"
                >
                  {citation.sourceTitle}
                </span>
              ),
            )}
          </div>
        </div>
      )}

      <div className="flex items-center gap-2">
        <IconButton
          label={muted ? "Unmute microphone" : "Mute microphone"}
          active={muted}
          disabled={over || status === "closing"}
          onClick={onToggleMute}
        >
          {muted ? <MicOff className="size-4" aria-hidden /> : <Mic className="size-4" aria-hidden />}
        </IconButton>
        <div className="flex-1" />
        <Button size="sm" disabled={status === "closing"} onClick={onStop}>
          {over ? "Close" : "End"}
        </Button>
      </div>

      {hint && <p className="text-xs text-ink-3">{hint}</p>}
    </section>
  );
}
