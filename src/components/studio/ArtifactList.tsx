"use client";

import { AudioLines, Play, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { Button, EmptyState, Eyebrow, IconButton, Modal, Spinner, cn, timeAgo } from "@/components/ui";
import type { Artifact } from "@/lib/api";
import { KIND_META } from "./kinds";

export interface ArtifactListProps {
  artifacts: Artifact[];
  /** False until the first list call has finished. */
  loaded: boolean;
  /** Id of the audio overview currently in the player, if any. */
  activeId: string | null;
  /** Id of the failed artifact being re-submitted, if any. */
  retryingId: string | null;
  onOpen: (artifact: Artifact) => void;
  onDelete: (artifact: Artifact) => void;
  onRetry: (artifact: Artifact) => void;
}

/** "Your outputs": every artifact for the notebook, newest first, with live progress. */
export function ArtifactList({ artifacts, loaded, activeId, retryingId, onOpen, onDelete, onRetry }: ArtifactListProps) {
  const [confirming, setConfirming] = useState<Artifact | null>(null);

  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between px-1">
        <Eyebrow>Your outputs</Eyebrow>
        {loaded && artifacts.length > 0 && <span className="font-mono text-[11px] text-ink-3">{artifacts.length}</span>}
      </div>

      {!loaded ? (
        <div className="flex justify-center py-6">
          <Spinner />
        </div>
      ) : artifacts.length === 0 ? (
        <EmptyState
          icon={<AudioLines className="size-5" aria-hidden />}
          title="Nothing generated yet"
        >
          Audio overviews and documents made from your sources will appear here.
        </EmptyState>
      ) : (
        <ul className="flex flex-col gap-2">
          {artifacts.map((artifact) => (
            <ArtifactRow
              key={artifact.id}
              artifact={artifact}
              active={artifact.id === activeId}
              retrying={artifact.id === retryingId}
              onOpen={onOpen}
              onRequestDelete={setConfirming}
              onRetry={onRetry}
            />
          ))}
        </ul>
      )}

      <Modal
        open={confirming !== null}
        onClose={() => setConfirming(null)}
        title="Delete this output?"
        description={confirming?.title}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirming(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                const target = confirming;
                setConfirming(null);
                if (target) onDelete(target);
              }}
            >
              Delete
            </Button>
          </>
        }
      >
        <p className="text-sm leading-relaxed text-ink-2">It will be removed from this notebook, along with its audio or text. This cannot be undone.</p>
      </Modal>
    </section>
  );
}

function ArtifactRow({
  artifact,
  active,
  retrying,
  onOpen,
  onRequestDelete,
  onRetry,
}: {
  artifact: Artifact;
  active: boolean;
  retrying: boolean;
  onOpen: (artifact: Artifact) => void;
  onRequestDelete: (artifact: Artifact) => void;
  onRetry: (artifact: Artifact) => void;
}) {
  const meta = KIND_META[artifact.kind];
  const Icon = meta.icon;
  const isAudio = artifact.kind === "audio_overview";
  const ready = artifact.status === "ready";
  const failed = artifact.status === "error";
  const inProgress = artifact.status === "queued" || artifact.status === "generating";

  return (
    <li className={cn("rounded-2xl border bg-surface p-3 transition-colors", active ? "border-accent" : "border-line")}>
      <div className="flex items-start gap-2">
        <button
          type="button"
          disabled={!ready}
          onClick={() => onOpen(artifact)}
          className="flex min-w-0 flex-1 items-start gap-3 rounded-xl text-left disabled:cursor-default"
        >
          <span
            className={cn(
              "flex size-9 shrink-0 items-center justify-center rounded-xl",
              isAudio && ready ? "bg-accent text-accent-ink" : "bg-surface-2 text-ink-2",
            )}
          >
            {isAudio && ready ? <Play className="size-4 translate-x-px" fill="currentColor" aria-hidden /> : <Icon className="size-4" aria-hidden />}
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[14px] font-medium text-ink">{artifact.title}</span>
            {ready && (
              <span className="mt-0.5 block text-xs text-ink-3">
                {active ? "Now playing · " : ""}
                {meta.label} · {timeAgo(artifact.createdAt)}
              </span>
            )}
            {inProgress && (
              <span className="mt-0.5 flex items-center gap-2 text-xs text-accent">
                <span className="ot-bars shrink-0" aria-hidden>
                  <i />
                  <i />
                  <i />
                  <i />
                </span>
                <span className="min-w-0 truncate">{artifact.progress ?? "Queued"}</span>
              </span>
            )}
            {failed && (
              <span className="mt-0.5 line-clamp-3 block text-xs text-danger">{artifact.error ?? "Generation failed."}</span>
            )}
          </span>
        </button>
        <IconButton label={`Delete ${artifact.title}`} onClick={() => onRequestDelete(artifact)} disabled={retrying}>
          <Trash2 className="size-4" />
        </IconButton>
      </div>

      {failed && (
        <div className="mt-2 flex justify-end">
          <Button size="sm" variant="secondary" loading={retrying} icon={<RefreshCw className="size-3.5" />} onClick={() => onRetry(artifact)}>
            Try again
          </Button>
        </div>
      )}
    </li>
  );
}
