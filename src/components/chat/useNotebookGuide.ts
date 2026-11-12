"use client";

import { useEffect, useState } from "react";
import { api, type NotebookGuide } from "@/lib/api";

export type GuideStatus = "idle" | "loading" | "ready" | "failed";

interface GuideState {
  key: string;
  guide: NotebookGuide | null;
}

/**
 * The notebook guide (an LLM call the engine caches until the sources change).
 * Fetched while `enabled`; refetched when `sourceKey` changes. Failures are
 * reported as "failed" rather than thrown, because the guide is optional.
 */
export function useNotebookGuide(
  notebookId: string,
  sourceKey: string,
  enabled: boolean,
): { status: GuideStatus; guide: NotebookGuide | null } {
  const key = `${notebookId}\u0000${sourceKey}`;
  const [state, setState] = useState<GuideState | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    api.notebooks.guide(notebookId).then(
      (guide) => {
        if (!cancelled) setState({ key, guide });
      },
      () => {
        if (!cancelled) setState({ key, guide: null });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [enabled, key, notebookId]);

  if (!enabled) return { status: "idle", guide: null };
  if (!state || state.key !== key) return { status: "loading", guide: null };
  return state.guide ? { status: "ready", guide: state.guide } : { status: "failed", guide: null };
}
