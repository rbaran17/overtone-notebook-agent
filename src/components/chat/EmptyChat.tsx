"use client";

import { FileText, MessageSquare } from "lucide-react";
import ReactMarkdown from "react-markdown";
import type { NotebookGuide } from "@/lib/api";
import { EmptyState, Eyebrow } from "@/components/ui";
import { REMARK_PLUGINS } from "./AnswerText";
import type { GuideStatus } from "./useNotebookGuide";

interface EmptyChatProps {
  hasSources: boolean;
  guide: { status: GuideStatus; guide: NotebookGuide | null };
  canAsk: boolean;
  onAsk: (question: string) => void;
}

/** What the panel shows before the first message: a prompt to add sources, the notebook guide, or a fallback. */
export function EmptyChat({ hasSources, guide, canAsk, onAsk }: EmptyChatProps) {
  if (!hasSources) {
    return (
      <EmptyState
        className="my-auto"
        icon={<FileText className="size-5" />}
        title="Add a source to start"
      >
        Chat answers only from the sources in this notebook. Add a file, link or pasted text in Sources. On phones,
        Sources is the tab at the bottom of the screen.
      </EmptyState>
    );
  }

  if (guide.status === "loading" || guide.status === "idle") {
    return (
      <div role="status" aria-label="Loading notebook guide" className="my-auto space-y-5">
        <div className="space-y-2.5">
          <div className="h-3 w-28 animate-pulse rounded bg-surface-2" />
          <div className="h-3.5 w-full animate-pulse rounded bg-surface-2" />
          <div className="h-3.5 w-11/12 animate-pulse rounded bg-surface-2" />
          <div className="h-3.5 w-3/4 animate-pulse rounded bg-surface-2" />
        </div>
        <div className="flex flex-wrap gap-2">
          <div className="h-8 w-40 animate-pulse rounded-xl bg-surface-2" />
          <div className="h-8 w-52 animate-pulse rounded-xl bg-surface-2" />
          <div className="h-8 w-36 animate-pulse rounded-xl bg-surface-2" />
        </div>
      </div>
    );
  }

  const data = guide.guide;
  if (guide.status !== "ready" || !data || (!data.summary.trim() && data.questions.length === 0)) {
    return <EmptyState className="my-auto" icon={<MessageSquare className="size-5" />} title="Ask anything about your sources" />;
  }

  return (
    <section aria-label="Notebook guide" className="my-auto space-y-6">
      {data.summary.trim() && (
        <div className="space-y-3">
          <Eyebrow>Notebook guide</Eyebrow>
          <div className="prose-ot text-[15px]">
            <ReactMarkdown remarkPlugins={REMARK_PLUGINS}>{data.summary}</ReactMarkdown>
          </div>
        </div>
      )}
      {data.questions.length > 0 && (
        <div className="space-y-3">
          <Eyebrow>Try asking</Eyebrow>
          <div className="flex flex-wrap gap-2">
            {data.questions.map((question, i) => (
              <button
                key={`${i}:${question}`}
                type="button"
                disabled={!canAsk}
                onClick={() => onAsk(question)}
                className="rounded-xl border border-line bg-surface px-3 py-2 text-left text-[13px] leading-snug text-ink-2 transition-colors hover:border-line-strong hover:bg-surface-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
              >
                {question}
              </button>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
