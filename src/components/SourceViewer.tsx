"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Source } from "@/lib/api";
import { estimateWords, hostOf, sourceTypeLabel } from "./SourcesPanel";
import { EmptyState, Eyebrow, errorMessage, Modal, Notice, Spinner } from "./ui";

export interface SourceViewerProps {
  /** Source to show, or null when closed. */
  sourceId: string | null;
  /** A passage to scroll to and highlight (from a chat citation). */
  quote?: string;
  onClose: () => void;
}

/** What the last request for a given source produced. Tagged with its id so stale results can be ignored. */
type Loaded = { sourceId: string; source: Source; text: string } | { sourceId: string; error: string };

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** A case-insensitive pattern where every whitespace run in `s` matches any whitespace run. */
function patternFor(s: string): RegExp | null {
  const words = s.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return null;
  return new RegExp(words.map(escapeRegExp).join("\\s+"), "i");
}

/** Finds the quote in the text, tolerating whitespace differences; falls back to its first ~80 characters. */
function locateQuote(text: string, quote: string): { start: number; end: number } | null {
  const candidates = [patternFor(quote), patternFor(quote.trim().slice(0, 80))];
  for (const pattern of candidates) {
    if (!pattern) continue;
    const match = pattern.exec(text);
    if (match) return { start: match.index, end: match.index + match[0].length };
  }
  return null;
}

function isHttpUrl(value: string): boolean {
  return /^https?:\/\//i.test(value);
}

export function SourceViewer({ sourceId, quote, onClose }: SourceViewerProps) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const markRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (sourceId === null) return;
    let cancelled = false;
    Promise.all([api.sources.get(sourceId), api.sources.getText(sourceId)])
      .then(([source, text]) => {
        if (!cancelled) setLoaded({ sourceId, source, text });
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoaded({ sourceId, error: errorMessage(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [sourceId]);

  // Only trust a result that belongs to the source currently requested.
  const current = sourceId !== null && loaded?.sourceId === sourceId ? loaded : null;
  const ready = current && "source" in current ? current : null;
  const failed = current && "error" in current ? current.error : null;

  const located = useMemo(
    () => (ready && quote ? locateQuote(ready.text, quote) : null),
    [ready, quote],
  );

  useEffect(() => {
    markRef.current?.scrollIntoView({ block: "center" });
  }, [located]);

  function renderText(body: string) {
    if (!located) return body;
    return (
      <>
        {body.slice(0, located.start)}
        <mark ref={markRef} className="rounded bg-accent-soft px-0.5 text-ink">
          {body.slice(located.start, located.end)}
        </mark>
        {body.slice(located.end)}
      </>
    );
  }

  const source = ready?.source;

  return (
    <Modal
      open={sourceId !== null}
      onClose={onClose}
      size="xl"
      title={source?.title ?? "Source"}
    >
      {sourceId !== null && !current && (
        <div className="flex items-center gap-2 text-sm text-ink-2">
          <Spinner />
          Loading source…
        </div>
      )}

      {failed && <Notice>Could not open this source. {failed}</Notice>}

      {ready && source && (
        <>
          <div className="mb-6 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-ink-2">
            <Eyebrow>{sourceTypeLabel(source)}</Eyebrow>
            {source.origin &&
              (source.kind === "url" && isHttpUrl(source.origin) ? (
                <a
                  href={source.origin}
                  target="_blank"
                  rel="noreferrer"
                  title={hostOf(source.origin)}
                  className="max-w-full truncate text-accent underline underline-offset-2"
                >
                  {source.origin}
                </a>
              ) : (
                <span className="max-w-full truncate">{source.origin}</span>
              ))}
            <span className="text-ink-3" aria-hidden>
              ·
            </span>
            <span>{estimateWords(source.charCount)}</span>
          </div>

          {quote && located === null && (
            <Notice tone="info" className="mb-5">
              The exact passage could not be located in this source, so the text is shown without a highlight.
            </Notice>
          )}

          {ready.text.trim() ? (
            <article className="max-w-[70ch] whitespace-pre-wrap break-words text-[15px] leading-7 text-ink">
              {renderText(ready.text)}
            </article>
          ) : (
            <EmptyState title="No text was extracted from this source." />
          )}
        </>
      )}
    </Modal>
  );
}
