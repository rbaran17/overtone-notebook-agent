"use client";

import { BookOpen, MoreHorizontal, Pencil, Plus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { api, type Notebook } from "@/lib/api";
import { Button, cn, EmptyState, Eyebrow, errorMessage, Field, Input, Logo, Modal, Notice, timeAgo } from "@/components/ui";

const DEFAULT_TITLE = "Untitled notebook";

function sourceLabel(count: number | undefined): string {
  if (!count) return "No sources yet";
  return count === 1 ? "1 source" : `${count} sources`;
}

function byRecentlyUpdated(a: Notebook, b: Notebook): number {
  return Date.parse(b.updatedAt) - Date.parse(a.updatedAt);
}

export default function HomePage() {
  const router = useRouter();
  const [notebooks, setNotebooks] = useState<Notebook[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<Notebook | null>(null);
  const [deleting, setDeleting] = useState<Notebook | null>(null);

  // Bumped by Retry to re-run the fetch below.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api.notebooks
      .list()
      .then((list) => {
        if (cancelled) return;
        setNotebooks([...list].sort(byRecentlyUpdated));
        setLoadError(null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(errorMessage(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  useEffect(() => {
    document.title = "Overtone";
  }, []);

  function retryLoad() {
    setLoading(true);
    setAttempt((n) => n + 1);
  }

  async function createNotebook() {
    setCreating(true);
    setCreateError(null);
    try {
      const notebook = await api.notebooks.create({ title: DEFAULT_TITLE });
      router.push(`/notebook/${notebook.id}`);
    } catch (err) {
      setCreateError(errorMessage(err));
      setCreating(false);
    }
  }

  function replaceNotebook(updated: Notebook) {
    setNotebooks((prev) => prev.map((n) => (n.id === updated.id ? { ...n, ...updated } : n)));
  }

  function removeNotebook(id: string) {
    setNotebooks((prev) => prev.filter((n) => n.id !== id));
  }

  const showEmpty = !loading && !loadError && notebooks.length === 0;
  const showGrid = !loading && notebooks.length > 0;

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="mx-auto flex h-16 w-full max-w-6xl items-center gap-2.5 px-4 sm:px-6">
        <Logo className="text-ink" />
        <span className="font-display text-[22px] leading-none text-ink">Overtone</span>
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-4 sm:px-6">
        <section className="pb-12 pt-8 sm:pb-14 sm:pt-14">
          <h1 className="max-w-3xl font-display text-[40px] leading-[1.05] tracking-tight text-ink sm:text-6xl">
            Turn your sources into something you can listen to.
          </h1>
          <p className="mt-4 max-w-xl text-base leading-relaxed text-ink-2">
            Gather documents, web pages and notes in one notebook, ask questions that cite them, and generate audio
            overviews and study guides.
          </p>
          <div className="mt-8">
            <Button
              variant="primary"
              size="md"
              icon={<Plus className="size-4" aria-hidden />}
              loading={creating}
              onClick={createNotebook}
            >
              New notebook
            </Button>
          </div>
          {createError && <Notice className="mt-5 max-w-md">Could not create the notebook. {createError}</Notice>}
        </section>

        <section aria-label="Your notebooks" className="pb-16">
          <div className="mb-4">
            <Eyebrow>Your notebooks</Eyebrow>
          </div>

          {loadError && (
            <Notice className="mb-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <span>Could not load your notebooks. {loadError}</span>
                <Button size="sm" onClick={retryLoad}>
                  Retry
                </Button>
              </div>
            </Notice>
          )}

          {loading && (
            <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4" aria-busy="true">
              {[0, 1, 2, 3].map((i) => (
                <li key={i} className="h-40 animate-pulse rounded-2xl border border-line bg-surface" aria-hidden />
              ))}
            </ul>
          )}

          {showEmpty && (
            <div className="rounded-2xl border border-line bg-surface">
              <EmptyState
                icon={<BookOpen className="size-5" aria-hidden />}
                title="Create your first notebook"
                action={
                  <Button
                    variant="primary"
                    size="md"
                    icon={<Plus className="size-4" aria-hidden />}
                    loading={creating}
                    onClick={createNotebook}
                  >
                    New notebook
                  </Button>
                }
              >
                A notebook holds your sources, the chat about them and anything you generate from them.
              </EmptyState>
            </div>
          )}

          {showGrid && (
            <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {notebooks.map((notebook) => (
                <li key={notebook.id}>
                  <NotebookCard
                    notebook={notebook}
                    onRename={() => setRenaming(notebook)}
                    onDelete={() => setDeleting(notebook)}
                  />
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>

      <footer className="mx-auto w-full max-w-6xl px-4 pb-10 sm:px-6">
        <p className="font-mono text-[11px] leading-relaxed text-ink-3">
          Also runs headless (Python SDK, REST API, CLI): overtone --help
        </p>
      </footer>

      {renaming && (
        <RenameDialog
          key={renaming.id}
          notebook={renaming}
          onClose={() => setRenaming(null)}
          onRenamed={(updated) => {
            replaceNotebook(updated);
            setRenaming(null);
          }}
        />
      )}

      {deleting && (
        <DeleteDialog
          key={deleting.id}
          notebook={deleting}
          onClose={() => setDeleting(null)}
          onDeleted={(id) => {
            removeNotebook(id);
            setDeleting(null);
          }}
        />
      )}
    </div>
  );
}

function NotebookCard({ notebook, onRename, onDelete }: { notebook: Notebook; onRename: () => void; onDelete: () => void }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const menuId = useId();

  function closeMenu(returnFocus: boolean) {
    setMenuOpen(false);
    if (returnFocus) menuButton.current?.focus();
  }

  return (
    <article
      className="relative flex h-full flex-col rounded-2xl border border-line bg-surface p-5 shadow-panel transition-colors hover:border-line-strong"
      onKeyDown={(e) => {
        if (e.key === "Escape" && menuOpen) closeMenu(true);
      }}
    >
      <div className="flex items-start gap-3">
        <span
          aria-hidden
          className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-surface-2 text-lg text-ink-2"
        >
          {notebook.icon ? notebook.icon : <BookOpen className="size-5" />}
        </span>
        <h3 className="min-w-0 flex-1 pr-8 text-[15px] font-semibold leading-snug text-ink">
          {/* The link's ::after covers the whole card, so the card itself is clickable. */}
          <Link
            href={`/notebook/${notebook.id}`}
            className="line-clamp-2 break-words after:absolute after:inset-0 after:rounded-2xl"
          >
            {notebook.title}
          </Link>
        </h3>
      </div>

      <div className="absolute right-3 top-3 z-10">
        <button
          ref={menuButton}
          type="button"
          aria-label={`Actions for ${notebook.title}`}
          title="More actions"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          aria-controls={menuOpen ? menuId : undefined}
          onClick={() => setMenuOpen((open) => !open)}
          className="inline-flex size-8 items-center justify-center rounded-lg text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <MoreHorizontal className="size-4" aria-hidden />
        </button>

        {menuOpen && (
          <>
            <div className="fixed inset-0 z-20" aria-hidden onClick={() => closeMenu(false)} />
            <div
              id={menuId}
              role="menu"
              aria-label={`Actions for ${notebook.title}`}
              className="absolute right-0 top-10 z-30 w-40 rounded-xl border border-line bg-surface p-1 shadow-panel"
            >
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  closeMenu(false);
                  onRename();
                }}
                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[13px] text-ink hover:bg-surface-2"
              >
                <Pencil className="size-4 text-ink-2" aria-hidden />
                Rename
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  closeMenu(false);
                  onDelete();
                }}
                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[13px] text-danger hover:bg-danger-soft"
              >
                <Trash2 className="size-4" aria-hidden />
                Delete
              </button>
            </div>
          </>
        )}
      </div>

      <div className="mt-auto flex items-center justify-between gap-3 pt-6">
        <Eyebrow className={cn(!notebook.sourceCount && "text-ink-3")}>{sourceLabel(notebook.sourceCount)}</Eyebrow>
        <Eyebrow>{timeAgo(notebook.updatedAt)}</Eyebrow>
      </div>
    </article>
  );
}

function RenameDialog({
  notebook,
  onClose,
  onRenamed,
}: {
  notebook: Notebook;
  onClose: () => void;
  onRenamed: (updated: Notebook) => void;
}) {
  const formId = useId();
  const formRef = useRef<HTMLFormElement>(null);
  const [value, setValue] = useState(notebook.title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const input = formRef.current?.querySelector("input");
    input?.focus();
    input?.select();
  }, []);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busy) return;
    const title = value.trim();
    if (!title) {
      setError("Give the notebook a name.");
      return;
    }
    if (title === notebook.title) {
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const updated = await api.notebooks.update(notebook.id, { title });
      onRenamed(updated);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={() => {
        if (!busy) onClose();
      }}
      title="Rename notebook"
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="primary" loading={busy} disabled={!value.trim()}>
            Save
          </Button>
        </>
      }
    >
      <form id={formId} ref={formRef} onSubmit={submit} className="space-y-3">
        <Field label="Notebook name">
          <Input
            value={value}
            maxLength={200}
            onChange={(e) => setValue(e.target.value)}
            aria-invalid={error ? true : undefined}
          />
        </Field>
        {error && <Notice>{error}</Notice>}
      </form>
    </Modal>
  );
}

function DeleteDialog({
  notebook,
  onClose,
  onDeleted,
}: {
  notebook: Notebook;
  onClose: () => void;
  onDeleted: (id: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await api.notebooks.delete(notebook.id);
      onDeleted(notebook.id);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={() => {
        if (!busy) onClose();
      }}
      title="Delete notebook?"
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="danger" loading={busy} onClick={confirm}>
            Delete notebook
          </Button>
        </>
      }
    >
      <p className="text-sm leading-relaxed text-ink-2">
        <span className="font-medium text-ink">{notebook.title}</span> will be deleted, along with its sources, chat
        history and generated audio. This can&apos;t be undone.
      </p>
      {error && <Notice className="mt-4">{error}</Notice>}
    </Modal>
  );
}
