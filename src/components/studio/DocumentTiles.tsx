"use client";

import { useState } from "react";
import { Button, Eyebrow, Modal, Notice, Spinner, Textarea, cn, errorMessage } from "@/components/ui";
import { api, type Artifact, type DocumentKind } from "@/lib/api";
import { KIND_META } from "./kinds";

const TILES: { kind: DocumentKind; label: string }[] = [
  { kind: "summary", label: "Summary" },
  { kind: "briefing", label: "Briefing" },
  { kind: "study_guide", label: "Study guide" },
  { kind: "faq", label: "FAQ" },
  { kind: "timeline", label: "Timeline" },
  { kind: "custom", label: "Custom" },
];

export interface DocumentTilesProps {
  notebookId: string;
  sourceIds: string[];
  onCreated: (artifact: Artifact) => void;
}

/** Quick-generate tiles for written documents. "Custom" asks for a description first. */
export function DocumentTiles({ notebookId, sourceIds, onCreated }: DocumentTilesProps) {
  const [pending, setPending] = useState<DocumentKind | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [customOpen, setCustomOpen] = useState(false);
  const disabled = sourceIds.length === 0;

  async function generate(kind: DocumentKind) {
    setError(null);
    setPending(kind);
    try {
      onCreated(await api.artifacts.create(notebookId, { kind, sourceIds }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setPending(null);
    }
  }

  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between px-1">
        <Eyebrow>Documents</Eyebrow>
        {disabled && <span className="text-xs text-ink-3">Select sources first</span>}
      </div>
      {error && <Notice onDismiss={() => setError(null)}>{error}</Notice>}
      <div className="grid grid-cols-2 gap-2">
        {TILES.map(({ kind, label }) => {
          const Icon = KIND_META[kind].icon;
          const busy = pending === kind;
          return (
            <button
              key={kind}
              type="button"
              disabled={disabled || pending !== null}
              onClick={() => (kind === "custom" ? setCustomOpen(true) : generate(kind))}
              className={cn(
                "flex items-center gap-3 rounded-2xl border border-line bg-surface px-3 py-3 text-left text-[14px] font-medium text-ink transition-colors",
                "hover:border-line-strong hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50",
              )}
            >
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-ink-2">
                {busy ? <Spinner className="size-4" /> : <Icon className="size-4" aria-hidden />}
              </span>
              <span className="min-w-0 truncate">{label}</span>
            </button>
          );
        })}
      </div>

      {customOpen && (
        <CustomDocumentDialog
          notebookId={notebookId}
          sourceIds={sourceIds}
          onClose={() => setCustomOpen(false)}
          onCreated={onCreated}
        />
      )}
    </section>
  );
}

function CustomDocumentDialog({
  notebookId,
  sourceIds,
  onClose,
  onCreated,
}: {
  notebookId: string;
  sourceIds: string[];
  onClose: () => void;
  onCreated: (artifact: Artifact) => void;
}) {
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trimmed = text.trim();

  async function submit() {
    if (!trimmed) return;
    setSubmitting(true);
    setError(null);
    try {
      onCreated(await api.artifacts.create(notebookId, { kind: "custom", sourceIds, instructions: trimmed }));
      onClose();
    } catch (err) {
      setError(errorMessage(err));
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="Custom document"
      description={KIND_META.custom.label}
      size="md"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={submitting} disabled={!trimmed} onClick={submit}>
            Generate
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <label className="block">
          <span className="mb-1.5 block text-[13px] font-medium text-ink">Describe the document you want</span>
          <Textarea
            required
            rows={5}
            maxLength={1000}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="For example: a one-page brief for a non-technical manager, focused on costs and risks."
            autoFocus
          />
        </label>
        {error && <Notice onDismiss={() => setError(null)}>{error}</Notice>}
      </div>
    </Modal>
  );
}
