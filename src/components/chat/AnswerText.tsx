"use client";

import { useContext, useMemo, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown, { type Components, type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Citation } from "@/lib/api";
import { CitationChip, CitationsContext, type CitationsValue } from "./CitationChip";
import { indexSet, linkifyCitations } from "./citations";

export const REMARK_PLUGINS = [remarkGfm];

const CITE_PREFIX = "#cite-";

/** Renders `[n](#cite-n)` links as citation chips; every other link opens in a new tab. */
function MarkdownLink({ href, children }: ComponentPropsWithoutRef<"a"> & ExtraProps) {
  const value = useContext(CitationsContext);
  if (href?.startsWith(CITE_PREFIX)) {
    const index = Number(href.slice(CITE_PREFIX.length));
    const citation = value?.byIndex.get(index);
    if (value && citation) {
      return <CitationChip index={index} citation={citation} onOpen={value.onOpenSource} />;
    }
    return <>{children}</>;
  }
  return (
    <a href={href} target="_blank" rel="noreferrer">
      {children}
    </a>
  );
}

// Module-level so the component identity is stable across renders (no remounting of chips while streaming).
const COMPONENTS: Components = { a: MarkdownLink };

/** An answer's Markdown with its citation markers turned into chips. */
export function AnswerText({
  text,
  citations,
  onOpenSource,
}: {
  text: string;
  citations: Citation[] | undefined;
  onOpenSource: (sourceId: string, quote?: string) => void;
}) {
  const byIndex = useMemo(() => new Map((citations ?? []).map((c) => [c.index, c] as const)), [citations]);
  const indices = useMemo(() => indexSet(citations), [citations]);
  const markdown = useMemo(() => linkifyCitations(text, indices), [text, indices]);
  const value = useMemo<CitationsValue>(() => ({ byIndex, onOpenSource }), [byIndex, onOpenSource]);

  return (
    <div className="prose-ot">
      <CitationsContext.Provider value={value}>
        <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={COMPONENTS}>
          {markdown}
        </ReactMarkdown>
      </CitationsContext.Provider>
    </div>
  );
}
