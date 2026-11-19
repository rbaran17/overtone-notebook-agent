"use client";

import { useContext, useMemo, type ComponentPropsWithoutRef } from "react";
import type { Components, ExtraProps } from "react-markdown";
import { Markdown, MarkdownLink } from "@/components/Markdown";
import type { Citation } from "@/lib/api";
import { CitationChip, CitationsContext, type CitationsValue } from "./CitationChip";
import { indexSet, linkifyCitations } from "./citations";

const CITE_PREFIX = "#cite-";

/** Renders `[n](#cite-n)` links as citation chips; every other link is an ordinary Markdown link. */
function AnswerLink(props: ComponentPropsWithoutRef<"a"> & ExtraProps) {
  const value = useContext(CitationsContext);
  const { href, children } = props;
  if (href?.startsWith(CITE_PREFIX)) {
    const index = Number(href.slice(CITE_PREFIX.length));
    const citation = value?.byIndex.get(index);
    if (value && citation) {
      return <CitationChip index={index} citation={citation} onOpen={value.onOpenSource} />;
    }
    return <>{children}</>;
  }
  return <MarkdownLink {...props} />;
}

// Module-level so the component identity is stable across renders (no remounting of chips while streaming).
const COMPONENTS: Components = { a: AnswerLink };

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
        <Markdown components={COMPONENTS}>{markdown}</Markdown>
      </CitationsContext.Provider>
    </div>
  );
}
