"use client";

import { Send, Square } from "lucide-react";
import { Textarea } from "@/components/ui";

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  streaming: boolean;
  disabled: boolean;
  placeholder: string;
}

/** Auto-growing message field (1 to 8 lines) with a send button that becomes Stop while replying. */
export function Composer({ value, onChange, onSend, onStop, streaming, disabled, placeholder }: ComposerProps) {
  const canSend = !disabled && !streaming && value.trim().length > 0;
  // An invisible copy of the text sizes the grid row, so the textarea grows with its content
  // up to the cap (max-h-48, about 8 lines) without any DOM measuring. The copy must match the
  // textarea's padding, border and type exactly.
  const mirror = value === "" || value.endsWith("\n") ? `${value}​` : value;

  return (
    <div className="flex items-end gap-2 p-3">
      <div className="grid min-w-0 flex-1">
        <div
          aria-hidden
          className="invisible col-start-1 row-start-1 max-h-48 overflow-hidden whitespace-pre-wrap break-words border border-transparent px-3 py-2.5 text-sm leading-relaxed"
        >
          {mirror}
        </div>
        <Textarea
          rows={1}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              if (canSend) onSend();
            }
          }}
          disabled={disabled}
          placeholder={placeholder}
          aria-label="Message"
          className="col-start-1 row-start-1 max-h-48 self-stretch overflow-y-auto"
        />
      </div>
      {streaming ? (
        <button
          type="button"
          onClick={onStop}
          aria-label="Stop generating"
          title="Stop generating"
          className="inline-flex size-10 shrink-0 items-center justify-center rounded-xl border border-line bg-surface text-ink transition-colors hover:border-line-strong hover:bg-surface-2"
        >
          <Square className="size-4 fill-current" aria-hidden />
        </button>
      ) : (
        <button
          type="button"
          onClick={onSend}
          disabled={!canSend}
          aria-label="Send message"
          title="Send message"
          className="inline-flex size-10 shrink-0 items-center justify-center rounded-xl bg-accent text-accent-ink transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Send className="size-4" aria-hidden />
        </button>
      )}
    </div>
  );
}
