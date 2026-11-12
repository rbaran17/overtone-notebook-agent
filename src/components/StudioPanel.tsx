"use client";

import { AudioLines, Sparkles, SlidersHorizontal } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArtifactList } from "./studio/ArtifactList";
import { ArtifactViewer } from "./studio/ArtifactViewer";
import { AudioOverviewDialog } from "./studio/AudioOverviewDialog";
import { AudioPlayer } from "./studio/AudioPlayer";
import { DocumentTiles } from "./studio/DocumentTiles";
import { Button, Notice, PanelFrame, errorMessage } from "./ui";
import { api, type Artifact, type Capabilities, type Source } from "@/lib/api";

export interface StudioPanelProps {
  notebookId: string;
  sources: Source[];
  /** Sources ticked in the Sources panel; generation is grounded on these. */
  selectedIds: Set<string>;
  /** Engine capabilities (model, TTS provider + voices). Null while loading. */
  capabilities: Capabilities | null;
  className?: string;
}

const POLL_MS = 2000;

export function StudioPanel({ notebookId, selectedIds, capabilities, className }: StudioPanelProps) {
  const sourceIds = [...selectedIds];
  const hasSources = sourceIds.length > 0;

  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [viewingId, setViewingId] = useState<string | null>(null);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [quickPending, setQuickPending] = useState(false);
  const [customizeOpen, setCustomizeOpen] = useState(false);

  // Reset per-notebook state during render when the notebook changes.
  const [trackedNotebook, setTrackedNotebook] = useState(notebookId);
  if (trackedNotebook !== notebookId) {
    setTrackedNotebook(notebookId);
    setArtifacts([]);
    setLoaded(false);
    setListError(null);
    setActionError(null);
    setPlayingId(null);
    setViewingId(null);
    setRetryingId(null);
    setCustomizeOpen(false);
  }

  const activeNotebook = useRef(notebookId);
  const topRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    activeNotebook.current = notebookId;
  }, [notebookId]);

  const refresh = useCallback(async () => {
    const id = notebookId;
    try {
      const list = await api.artifacts.list(id);
      if (activeNotebook.current !== id) return;
      setArtifacts(list);
      setListError(null);
    } catch (err) {
      if (activeNotebook.current === id) setListError(errorMessage(err));
    } finally {
      if (activeNotebook.current === id) setLoaded(true);
    }
  }, [notebookId]);

  // Initial load for this notebook. State is only set from the promise callbacks.
  useEffect(() => {
    let cancelled = false;
    api.artifacts
      .list(notebookId)
      .then((list) => {
        if (cancelled) return;
        setArtifacts(list);
        setListError(null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setListError(errorMessage(err));
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [notebookId]);

  // Poll only while something is still being generated.
  const busy = artifacts.some((a) => a.status === "queued" || a.status === "generating");
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [busy, refresh]);

  const playing = artifacts.find((a) => a.id === playingId && a.kind === "audio_overview" && a.status === "ready") ?? null;
  const viewing = artifacts.find((a) => a.id === viewingId && a.status === "ready") ?? null;

  // Opening a player near the bottom of the list would leave it off-screen; bring it into view.
  useEffect(() => {
    if (playingId) topRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [playingId]);

  function handleCreated(artifact: Artifact) {
    setActionError(null);
    setArtifacts((prev) => [artifact, ...prev.filter((a) => a.id !== artifact.id)]);
    void refresh();
  }

  function openArtifact(artifact: Artifact) {
    if (artifact.kind === "audio_overview") setPlayingId(artifact.id);
    else setViewingId(artifact.id);
  }

  async function deleteArtifact(artifact: Artifact) {
    setActionError(null);
    try {
      await api.artifacts.delete(artifact.id);
      setArtifacts((prev) => prev.filter((a) => a.id !== artifact.id));
      if (playingId === artifact.id) setPlayingId(null);
      if (viewingId === artifact.id) setViewingId(null);
    } catch (err) {
      setActionError(errorMessage(err));
    }
  }

  async function retryArtifact(failed: Artifact) {
    setActionError(null);
    setRetryingId(failed.id);
    try {
      const fresh = await api.artifacts.create(notebookId, failed.request);
      setArtifacts((prev) => [fresh, ...prev.filter((a) => a.id !== failed.id && a.id !== fresh.id)]);
      try {
        await api.artifacts.delete(failed.id);
      } catch {
        setActionError("A new attempt started, but the failed output could not be removed.");
      }
      void refresh();
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setRetryingId(null);
    }
  }

  async function generateDefaultOverview() {
    setActionError(null);
    setQuickPending(true);
    try {
      const artifact = await api.artifacts.create(notebookId, {
        kind: "audio_overview",
        sourceIds,
        options: { format: "deep_dive", length: "medium" },
      });
      handleCreated(artifact);
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setQuickPending(false);
    }
  }

  const voiceNote = capabilities
    ? capabilities.tts
      ? `${capabilities.tts.label} voices`
      : "Browser voices (no TTS key on the server)"
    : "Checking voice engine…";

  return (
    <PanelFrame title="Studio" className={className}>
      <div ref={topRef} className="flex flex-col gap-3 p-3">
        {actionError && <Notice onDismiss={() => setActionError(null)}>{actionError}</Notice>}
        {listError && <Notice>Could not load your outputs: {listError}</Notice>}

        {playing && (
          <AudioPlayer
            key={playing.id}
            artifact={playing}
            onClose={() => setPlayingId(null)}
            liveAvailable={Boolean(capabilities?.live)}
          />
        )}

        <section className="rounded-2xl border border-line bg-surface p-4 shadow-panel">
          <div className="flex items-start gap-3">
            <span className="flex size-11 shrink-0 items-center justify-center rounded-2xl bg-accent text-accent-ink">
              <AudioLines className="size-5" aria-hidden />
            </span>
            <div className="min-w-0">
              <h3 className="font-display text-[22px] leading-tight text-ink">Audio overview</h3>
              <p className="mt-1 text-[13px] leading-relaxed text-ink-2">
                Turn the selected sources into a conversation you can listen to.
              </p>
            </div>
          </div>
          <div className="mt-4 grid grid-cols-2 gap-2">
            <Button
              variant="primary"
              className="w-full"
              icon={<Sparkles className="size-4" />}
              loading={quickPending}
              disabled={!hasSources}
              onClick={generateDefaultOverview}
            >
              Generate
            </Button>
            <Button
              variant="secondary"
              className="w-full"
              icon={<SlidersHorizontal className="size-4" />}
              disabled={!hasSources}
              onClick={() => setCustomizeOpen(true)}
            >
              Customize
            </Button>
          </div>
          {!hasSources && <p className="mt-2 text-xs text-ink-3">Tick a source in the Sources panel to get started.</p>}
          <p className="mt-3 font-mono text-[11px] text-ink-3">{voiceNote}</p>
        </section>

        <DocumentTiles notebookId={notebookId} sourceIds={sourceIds} onCreated={handleCreated} />

        <ArtifactList
          artifacts={artifacts}
          loaded={loaded}
          activeId={playingId}
          retryingId={retryingId}
          onOpen={openArtifact}
          onDelete={deleteArtifact}
          onRetry={retryArtifact}
        />
      </div>

      {customizeOpen && (
        <AudioOverviewDialog
          notebookId={notebookId}
          sourceIds={sourceIds}
          capabilities={capabilities}
          onClose={() => setCustomizeOpen(false)}
          onCreated={handleCreated}
        />
      )}
      {viewing && <ArtifactViewer key={viewing.id} artifact={viewing} onClose={() => setViewingId(null)} />}
    </PanelFrame>
  );
}
