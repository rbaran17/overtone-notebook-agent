"use client";

import { ArrowLeft, AudioLines, FileText, MessageSquare } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChatPanel } from "@/components/ChatPanel";
import { SourcesPanel } from "@/components/SourcesPanel";
import { SourceViewer } from "@/components/SourceViewer";
import { StudioPanel } from "@/components/StudioPanel";
import { Button, cn, EmptyState, errorMessage, Logo, Spinner } from "@/components/ui";
import { api, type Capabilities, type Notebook, type Source } from "@/lib/api";

type Tab = "sources" | "chat" | "studio";

const TABS: { id: Tab; label: string; icon: typeof FileText }[] = [
  { id: "sources", label: "Sources", icon: FileText },
  { id: "chat", label: "Chat", icon: MessageSquare },
  { id: "studio", label: "Studio", icon: AudioLines },
];

export default function NotebookPage() {
  const { id } = useParams<{ id: string }>();

  const [notebook, setNotebook] = useState<Notebook | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [viewer, setViewer] = useState<{ sourceId: string; quote?: string } | null>(null);
  const [tab, setTab] = useState<Tab>("chat");
  const [title, setTitle] = useState("");

  // Ids we have already seen, so newly added sources start out selected
  // without re-selecting ones the user deliberately unticked.
  const seen = useRef<Set<string>>(new Set());

  const applySources = useCallback((list: Source[]) => {
    // Work out which ids are new before recording them, so the state updater stays pure.
    const fresh = new Set(list.filter((s) => !seen.current.has(s.id)).map((s) => s.id));
    for (const s of list) seen.current.add(s.id);
    setSources(list);
    setSelectedIds((prev) => new Set(list.filter((s) => prev.has(s.id) || fresh.has(s.id)).map((s) => s.id)));
    setSourcesLoading(false);
  }, []);

  const refreshSources = useCallback(async () => {
    try {
      applySources(await api.sources.list(id));
    } catch (err) {
      setLoadError(errorMessage(err));
      setSourcesLoading(false);
    }
  }, [id, applySources]);

  useEffect(() => {
    let cancelled = false;
    seen.current = new Set();
    api.notebooks
      .get(id)
      .then((nb) => {
        if (cancelled) return;
        setNotebook(nb);
        setTitle(nb.title);
        // A fresh notebook has nothing to chat about yet: start on Sources (mobile).
        if (!nb.sourceCount) setTab("sources");
      })
      .catch((err) => {
        if (!cancelled) setLoadError(errorMessage(err));
      });
    api.sources
      .list(id)
      .then((list) => {
        if (!cancelled) applySources(list);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(errorMessage(err));
        setSourcesLoading(false);
      });
    api
      .health()
      .then((c) => {
        if (!cancelled) setCapabilities(c);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [id, applySources]);

  useEffect(() => {
    if (notebook) document.title = `${notebook.title} · Overtone`;
  }, [notebook]);

  async function commitTitle() {
    const next = title.trim();
    if (!notebook || !next || next === notebook.title) {
      setTitle(notebook?.title ?? "");
      return;
    }
    try {
      setNotebook(await api.notebooks.update(notebook.id, { title: next }));
    } catch {
      setTitle(notebook.title);
    }
  }

  if (loadError && !notebook) {
    return (
      <main className="flex flex-1 items-center justify-center p-6">
        <EmptyState
          title="This notebook could not be opened"
          action={
            <Link href="/">
              <Button icon={<ArrowLeft className="size-4" />}>All notebooks</Button>
            </Link>
          }
        >
          {loadError}
        </EmptyState>
      </main>
    );
  }

  if (!notebook) {
    return (
      <main className="flex flex-1 items-center justify-center">
        <Spinner className="size-5" />
      </main>
    );
  }

  return (
    <div className="flex h-dvh flex-col">
      <header className="flex h-14 shrink-0 items-center gap-3 px-4">
        <Link
          href="/"
          aria-label="All notebooks"
          title="All notebooks"
          className="flex size-9 shrink-0 items-center justify-center rounded-xl text-ink hover:bg-surface-2"
        >
          <Logo />
        </Link>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onBlur={commitTitle}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
            if (e.key === "Escape") {
              setTitle(notebook.title);
              e.currentTarget.blur();
            }
          }}
          aria-label="Notebook title"
          className="min-w-0 flex-1 truncate rounded-lg bg-transparent px-2 py-1 font-display text-[22px] leading-tight text-ink hover:bg-surface-2 focus:bg-surface focus:outline-none focus:ring-1 focus:ring-line-strong"
        />
        {capabilities && (
          <span className="hidden shrink-0 font-mono text-[11px] text-ink-3 sm:block">
            {capabilities.model}
            {capabilities.tts ? ` · ${capabilities.tts.label} voices` : " · browser voices"}
          </span>
        )}
      </header>

      <main className="grid min-h-0 flex-1 grid-cols-1 gap-3 px-3 pb-3 lg:grid-cols-[minmax(260px,320px)_minmax(0,1fr)_minmax(320px,400px)]">
        <SourcesPanel
          notebookId={id}
          sources={sources}
          loading={sourcesLoading}
          selectedIds={selectedIds}
          onSelectedChange={setSelectedIds}
          onSourcesChanged={refreshSources}
          onOpenSource={(sourceId) => setViewer({ sourceId })}
          className={cn(tab !== "sources" && "hidden lg:flex")}
        />
        <ChatPanel
          notebookId={id}
          sources={sources}
          selectedIds={selectedIds}
          onOpenSource={(sourceId, quote) => setViewer({ sourceId, quote })}
          capabilities={capabilities}
          className={cn(tab !== "chat" && "hidden lg:flex")}
        />
        <StudioPanel
          notebookId={id}
          sources={sources}
          selectedIds={selectedIds}
          capabilities={capabilities}
          className={cn(tab !== "studio" && "hidden lg:flex")}
        />
      </main>

      {/* Below the lg breakpoint the three panels become tabs. */}
      <nav aria-label="Panels" className="grid shrink-0 grid-cols-3 border-t border-line bg-surface lg:hidden">
        {TABS.map(({ id: tabId, label, icon: Icon }) => (
          <button
            key={tabId}
            type="button"
            onClick={() => setTab(tabId)}
            aria-current={tab === tabId ? "page" : undefined}
            className={cn(
              "flex h-14 flex-col items-center justify-center gap-0.5 text-[11px] font-medium",
              tab === tabId ? "text-accent" : "text-ink-2",
            )}
          >
            <Icon className="size-5" />
            {label}
          </button>
        ))}
      </nav>

      <SourceViewer sourceId={viewer?.sourceId ?? null} quote={viewer?.quote} onClose={() => setViewer(null)} />
    </div>
  );
}
