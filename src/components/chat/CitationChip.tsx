"use client";

import { createContext, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Citation } from "@/lib/api";

export interface CitationsValue {
  byIndex: ReadonlyMap<number, Citation>;
  onOpenSource: (sourceId: string, quote?: string) => void;
}

/** Supplies the citations of the message being rendered to the chips inside its Markdown. */
export const CitationsContext = createContext<CitationsValue | null>(null);

const CARD_WIDTH = 288;
const CARD_HEIGHT_ESTIMATE = 150;
const GAP = 8;

interface Placement {
  left: number;
  top: number;
  above: boolean;
}

/**
 * A small numbered chip for an inline `[n]` citation. Hover or focus shows the
 * source and the quoted passage; click opens the source. The card is portalled
 * to <body> with fixed positioning so the scrolling panel cannot clip it.
 */
export function CitationChip({
  index,
  citation,
  onOpen,
}: {
  index: number;
  citation: Citation;
  onOpen: (sourceId: string, quote?: string) => void;
}) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [placement, setPlacement] = useState<Placement | null>(null);
  const cardId = useId();

  function show() {
    const el = buttonRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const left = Math.min(
      Math.max(rect.left + rect.width / 2 - CARD_WIDTH / 2, GAP),
      window.innerWidth - CARD_WIDTH - GAP,
    );
    const above =
      rect.bottom + GAP + CARD_HEIGHT_ESTIMATE > window.innerHeight && rect.top - GAP - CARD_HEIGHT_ESTIMATE > 0;
    setPlacement({ left, top: above ? rect.top - GAP : rect.bottom + GAP, above });
  }

  function hide() {
    setPlacement(null);
  }

  useEffect(() => {
    if (!placement) return;
    const dismiss = () => setPlacement(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismiss();
    };
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", dismiss, true);
    window.addEventListener("resize", dismiss);
    return () => {
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", dismiss, true);
      window.removeEventListener("resize", dismiss);
    };
  }, [placement]);

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        aria-label={`Source ${index}: ${citation.sourceTitle}`}
        aria-describedby={placement ? cardId : undefined}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        onClick={() => onOpen(citation.sourceId, citation.quote)}
        className="mx-px inline-flex min-w-5 items-center justify-center rounded-full bg-surface-2 px-1 align-[0.08em] font-mono text-[11px] leading-4 text-ink-2 transition-colors hover:bg-accent-soft hover:text-accent"
      >
        {index}
      </button>
      {placement &&
        createPortal(
          <div
            id={cardId}
            role="tooltip"
            style={{
              left: placement.left,
              top: placement.top,
              width: CARD_WIDTH,
              transform: placement.above ? "translateY(-100%)" : undefined,
            }}
            className="pointer-events-none fixed z-50 rounded-xl border border-line bg-surface p-3 text-[13px] leading-relaxed text-ink shadow-panel"
          >
            <p className="truncate font-semibold">{citation.sourceTitle}</p>
            {citation.quote && <p className="mt-1 line-clamp-4 text-ink-2">{`“${citation.quote}”`}</p>}
          </div>,
          document.body,
        )}
    </>
  );
}
