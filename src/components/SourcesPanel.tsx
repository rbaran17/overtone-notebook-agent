"use client";

import {
  AlignLeft,
  FileCode,
  FileJson,
  FileSpreadsheet,
  FileText,
  FileType,
  FileUp,
  Globe,
  Plus,
  Trash2,
} from "lucide-react";
import { useRef, useState, type DragEvent, type ReactNode } from "react";
import { api, type Source } from "@/lib/api";
import { AddSourceDialog } from "./AddSourceDialog";
import { Button, cn, EmptyState, errorMessage, IconButton, Modal, Notice, PanelFrame, Spinner } from "./ui";

export interface SourcesPanelProps {
  notebookId: string;
  sources: Source[];
  /** True until the first source list has loaded. */
  loading: boolean;
  /** Sources currently ticked as grounding for chat and Studio. */
  selectedIds: Set<string>;
  onSelectedChange: (ids: Set<string>) => void;
  /** Call after adding or deleting sources; the page refetches the list. */
  onSourcesChanged: () => void | Promise<void>;
  /** Open the source viewer (owned by the page). */
  onOpenSource: (sourceId: string) => void;
  className?: string;
}

interface Failure {
  name: string;
  error: string;
}

const EXTENSION_LABELS: Record<string, string> = {
  pdf: "PDF",
  docx: "Word",
  doc: "Word",
  md: "Markdown",
  markdown: "Markdown",
  txt: "Text",
  html: "HTML",
  htm: "HTML",
  csv: "CSV",
  json: "JSON",
};

function extensionOf(name: string): string {
  const match = /\.([a-z0-9]+)$/i.exec(name);
  return match ? match[1].toLowerCase() : "";
}

/** Short human label for a source's type, e.g. "PDF" or "Web page". */
export function sourceTypeLabel(source: Source): string {
  if (source.kind === "url") return "Web page";
  if (source.kind === "text") return "Pasted text";
  const ext = extensionOf(source.origin ?? source.title);
  return EXTENSION_LABELS[ext] ?? (ext ? ext.toUpperCase() : "File");
}

/** Rough word count from characters (about six per word), e.g. "~2,400 words". */
export function estimateWords(charCount: number): string {
  return `~${Math.round(charCount / 6).toLocaleString()} words`;
}

/** Hostname of a URL, without a leading "www.". Falls back to the input. */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function sourceIcon(source: Source): ReactNode {
  const cls = "size-4";
  if (source.kind === "url") return <Globe className={cls} />;
  if (source.kind === "text") return <AlignLeft className={cls} />;
  switch (extensionOf(source.origin ?? source.title)) {
    case "csv":
      return <FileSpreadsheet className={cls} />;
    case "html":
    case "htm":
      return <FileCode className={cls} />;
    case "json":
      return <FileJson className={cls} />;
    case "docx":
    case "doc":
      return <FileType className={cls} />;
    default:
      return <FileText className={cls} />;
  }
}

function metaFor(source: Source): string {
  const type = source.kind === "url" && source.origin ? hostOf(source.origin) : sourceTypeLabel(source);
  return `${type} · ${estimateWords(source.charCount)}`;
}

/** `addFiles` attaches per-file failures as a non-enumerable `errors` array. */
function uploadFailuresOf(result: Source[]): Failure[] {
  return (result as unknown as { errors?: Failure[] }).errors ?? [];
}

function isFileDrag(e: DragEvent): boolean {
  return Array.from(e.dataTransfer.types).includes("Files");
}

function SourceRow({
  source,
  checked,
  onToggle,
  onOpen,
  onDelete,
}: {
  source: Source;
  checked: boolean;
  onToggle: () => void;
  onOpen: () => void;
  onDelete: () => void;
}) {
  return (
    <li className="group flex items-center gap-1 rounded-xl pr-1 transition-colors hover:bg-surface-2">
      <button
        type="button"
        onClick={onOpen}
        className="flex min-w-0 flex-1 items-center gap-3 rounded-xl py-2.5 pl-3 text-left"
      >
        <span
          aria-hidden
          className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-ink-2 group-hover:bg-surface"
        >
          {sourceIcon(source)}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-medium text-ink">{source.title}</span>
          <span className="mt-0.5 block truncate font-mono text-[11px] text-ink-3">{metaFor(source)}</span>
        </span>
      </button>
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        aria-label={`Use ${source.title} as a source`}
        className="size-4 shrink-0 accent-accent"
      />
      <IconButton
        label={`Delete ${source.title}`}
        onClick={onDelete}
        className="opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
      >
        <Trash2 className="size-4" aria-hidden />
      </IconButton>
    </li>
  );
}

export function SourcesPanel({
  notebookId,
  sources,
  loading,
  selectedIds,
  onSelectedChange,
  onSourcesChanged,
  onOpenSource,
  className,
}: SourcesPanelProps) {
  const [addOpen, setAddOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const [uploading, setUploading] = useState<number | null>(null);
  const [failures, setFailures] = useState<Failure[]>([]);
  const [pendingDelete, setPendingDelete] = useState<Source | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const selectedCount = sources.filter((s) => selectedIds.has(s.id)).length;
  const allSelected = sources.length > 0 && selectedCount === sources.length;
  const someSelected = selectedCount > 0 && !allSelected;

  function toggleAll() {
    onSelectedChange(allSelected ? new Set() : new Set(sources.map((s) => s.id)));
  }

  function toggleOne(id: string) {
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onSelectedChange(next);
  }

  async function uploadFiles(list: File[]) {
    if (list.length === 0 || uploading !== null) return;
    setUploading(list.length);
    setFailures([]);
    try {
      const result = await api.sources.addFiles(notebookId, list);
      setFailures(uploadFailuresOf(result));
      await onSourcesChanged();
    } catch (err) {
      const name = list.length === 1 ? list[0].name : `${list.length} files`;
      setFailures([{ name, error: errorMessage(err) }]);
    } finally {
      setUploading(null);
    }
  }

  async function confirmDelete() {
    const target = pendingDelete;
    if (!target) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await api.sources.delete(target.id);
      setPendingDelete(null);
      await onSourcesChanged();
    } catch (err) {
      setDeleteError(errorMessage(err));
    } finally {
      setDeleting(false);
    }
  }

  function closeDelete() {
    if (deleting) return;
    setPendingDelete(null);
    setDeleteError(null);
  }

  function onDragEnter(e: DragEvent<HTMLDivElement>) {
    if (!isFileDrag(e)) return;
    dragDepth.current += 1;
    setDragging(true);
  }

  function onDragLeave(e: DragEvent<HTMLDivElement>) {
    if (!isFileDrag(e)) return;
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragging(false);
  }

  function onDragOver(e: DragEvent<HTMLDivElement>) {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    void uploadFiles(Array.from(e.dataTransfer.files));
  }

  const openAdd = () => setAddOpen(true);

  let content: ReactNode;
  if (loading) {
    content = (
      <ul className="space-y-2 p-3" aria-busy="true" aria-label="Loading sources">
        {[0, 1, 2, 3].map((i) => (
          <li key={i} className="flex items-center gap-3 px-2 py-2.5" aria-hidden>
            <span className="size-8 shrink-0 animate-pulse rounded-lg bg-surface-2" />
            <span className="min-w-0 flex-1 space-y-2">
              <span className="block h-3 w-3/4 animate-pulse rounded bg-surface-2" />
              <span className="block h-2.5 w-1/2 animate-pulse rounded bg-surface-2" />
            </span>
          </li>
        ))}
      </ul>
    );
  } else if (sources.length === 0) {
    content = (
      <EmptyState
        icon={<FileUp className="size-5" aria-hidden />}
        title="No sources yet"
        action={
          <Button variant="primary" size="sm" icon={<Plus className="size-4" aria-hidden />} onClick={openAdd}>
            Add
          </Button>
        }
      >
        Add PDFs, Word documents, web pages or pasted text. Everything Overtone says and records is grounded in them.
      </EmptyState>
    );
  } else {
    content = (
      <>
        <label className="sticky top-0 z-[1] flex cursor-pointer items-center gap-3 border-b border-line bg-surface px-4 py-2.5">
          <input
            ref={(el) => {
              if (el) el.indeterminate = someSelected;
            }}
            type="checkbox"
            checked={allSelected}
            onChange={toggleAll}
            className="size-4 shrink-0 accent-accent"
          />
          <span className="text-[13px] text-ink">Select all</span>
          <span className="ml-auto font-mono text-[11px] text-ink-3">
            {selectedCount} of {sources.length} selected
          </span>
        </label>
        <ul className="p-2">
          {sources.map((source) => (
            <SourceRow
              key={source.id}
              source={source}
              checked={selectedIds.has(source.id)}
              onToggle={() => toggleOne(source.id)}
              onOpen={() => onOpenSource(source.id)}
              onDelete={() => {
                setDeleteError(null);
                setPendingDelete(source);
              }}
            />
          ))}
        </ul>
      </>
    );
  }

  return (
    <div
      className={cn("relative flex min-h-0 min-w-0 flex-col", className)}
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      <PanelFrame
        title="Sources"
        meta={loading ? undefined : sources.length}
        className="min-h-0 flex-1"
        actions={
          <Button size="sm" variant="primary" icon={<Plus className="size-4" aria-hidden />} onClick={openAdd}>
            Add
          </Button>
        }
      >
        {uploading !== null && (
          <div role="status" className="flex items-center gap-2 border-b border-line px-4 py-2.5 text-[13px] text-ink-2">
            <Spinner />
            Uploading {uploading} {uploading === 1 ? "file" : "files"}…
          </div>
        )}

        {failures.length > 0 && (
          <div className="p-3">
            <Notice onDismiss={() => setFailures([])}>
              <p className="font-medium">
                {failures.length === 1 ? "A file could not be added." : `${failures.length} files could not be added.`}
              </p>
              <ul className="mt-1.5 space-y-1">
                {failures.map((f, i) => (
                  <li key={`${f.name}-${i}`} className="break-words">
                    <span className="font-medium">{f.name}</span>: {f.error}
                  </li>
                ))}
              </ul>
            </Notice>
          </div>
        )}

        {content}
      </PanelFrame>

      {dragging && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed border-accent bg-surface/90 text-center"
        >
          <FileUp className="size-6 text-accent" />
          <p className="text-sm font-medium text-ink">Drop to add to this notebook</p>
        </div>
      )}

      <AddSourceDialog
        notebookId={notebookId}
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onAdded={onSourcesChanged}
      />

      <Modal
        open={pendingDelete !== null}
        onClose={closeDelete}
        title="Remove source?"
        footer={
          <>
            <Button onClick={closeDelete} disabled={deleting}>
              Cancel
            </Button>
            <Button variant="danger" loading={deleting} onClick={confirmDelete}>
              Remove source
            </Button>
          </>
        }
      >
        <p className="text-sm leading-relaxed text-ink-2">
          <span className="font-medium text-ink">{pendingDelete?.title}</span> will be removed from this notebook. Chat
          and Studio will no longer draw on it, and this can&apos;t be undone.
        </p>
        {deleteError && <Notice className="mt-4">{deleteError}</Notice>}
      </Modal>
    </div>
  );
}
