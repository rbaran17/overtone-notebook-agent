"use client";

import { Check, Copy, Download } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useEffect, useRef, useState } from "react";
import { Button, EmptyState, Modal } from "@/components/ui";
import type { Artifact } from "@/lib/api";
import { KIND_META } from "./kinds";
import { downloadText, slugify } from "./util";

export interface ArtifactViewerProps {
  artifact: Artifact;
  onClose: () => void;
}

/** Full-size reader for a generated document. Render it only while it should be open. */
export function ArtifactViewer({ artifact, onClose }: ArtifactViewerProps) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  const resetTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    const timer = resetTimer;
    return () => clearTimeout(timer.current);
  }, []);

  const content = artifact.content ?? "";

  async function copy() {
    try {
      await navigator.clipboard.writeText(content);
      setCopyError(null);
      setCopied(true);
      clearTimeout(resetTimer.current);
      resetTimer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopyError("Could not copy. Select the text and copy it manually.");
    }
  }

  function download() {
    downloadText(`${slugify(artifact.title)}.md`, content, "text/markdown;charset=utf-8");
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={artifact.title}
      description={KIND_META[artifact.kind].label}
      size="xl"
      footer={
        content ? (
          <>
            {copyError && <p className="mr-auto text-[13px] text-danger">{copyError}</p>}
            <Button
              variant="secondary"
              onClick={copy}
              icon={copied ? <Check className="size-4" /> : <Copy className="size-4" />}
            >
              {copied ? "Copied" : "Copy"}
            </Button>
            <Button variant="primary" onClick={download} icon={<Download className="size-4" />}>
              Download .md
            </Button>
          </>
        ) : undefined
      }
    >
      {content ? (
        <div className="prose-ot mx-auto max-w-2xl text-[15px]">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <EmptyState title="This document is empty">It has no content yet.</EmptyState>
        </div>
      )}
    </Modal>
  );
}
