"use client";

import { Check, Copy } from "lucide-react";
import { useMemo, useState } from "react";
import type { ChatMessage, Citation } from "@/lib/api";
import { Eyebrow, IconButton } from "@/components/ui";
import { AnswerText } from "./AnswerText";
import { indexSet, stripCitationMarkers } from "./citations";

interface MessageViewProps {
  message: ChatMessage;
  /** True for the assistant reply currently being written. */
  streaming: boolean;
  onOpenSource: (sourceId: string, quote?: string) => void;
}

export function MessageView({ message, streaming, onOpenSource }: MessageViewProps) {
  if (message.role === "user") {
    return (
      <div className="ml-auto max-w-[85%] whitespace-pre-wrap break-words rounded-2xl bg-surface-2 px-4 py-2.5 text-[15px] leading-relaxed text-ink">
        {message.content}
      </div>
    );
  }
  return <AssistantMessage message={message} streaming={streaming} onOpenSource={onOpenSource} />;
}

/** Unique cited sources, in order of first citation. */
function uniqueSources(citations: Citation[] | undefined): Citation[] {
  const seen = new Map<string, Citation>();
  for (const c of citations ?? []) {
    if (!seen.has(c.sourceId)) seen.set(c.sourceId, c);
  }
  return [...seen.values()];
}

function AssistantMessage({ message, streaming, onOpenSource }: MessageViewProps) {
  const [copied, setCopied] = useState(false);
  const cited = useMemo(() => uniqueSources(message.citations), [message.citations]);
  const plain = useMemo(
    () => stripCitationMarkers(message.content, indexSet(message.citations)),
    [message.content, message.citations],
  );
  const waiting = streaming && message.content === "";

  async function copy() {
    try {
      await navigator.clipboard.writeText(plain);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard unavailable (e.g. insecure origin): nothing to show.
    }
  }

  return (
    <div className="max-w-[68ch] text-[15px] text-ink">
      {waiting ? (
        <p role="status" className="flex items-center gap-2 text-[13px] text-ink-3">
          <span className="ot-bars text-accent" aria-hidden>
            <i />
            <i />
            <i />
            <i />
          </span>
          Reading your sources
        </p>
      ) : (
        <AnswerText text={message.content} citations={message.citations} onOpenSource={onOpenSource} />
      )}

      {!streaming && message.content.length > 0 && (
        <div className="mt-3 flex items-center gap-2">
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
            {cited.length > 0 && <Eyebrow className="mr-1">Sources</Eyebrow>}
            {cited.map((c) => (
              <button
                key={c.sourceId}
                type="button"
                title={c.sourceTitle}
                onClick={() => onOpenSource(c.sourceId)}
                className="max-w-[16rem] truncate rounded-full border border-line px-2.5 py-1 text-[12px] text-ink-2 transition-colors hover:border-line-strong hover:bg-surface-2 hover:text-ink"
              >
                {c.sourceTitle}
              </button>
            ))}
          </div>
          <IconButton label={copied ? "Copied" : "Copy answer"} onClick={copy}>
            {copied ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />}
          </IconButton>
        </div>
      )}
    </div>
  );
}
