"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ChatMessage } from "@/lib/api";
import { errorMessage } from "@/components/ui";

export type HistoryStatus = "loading" | "ready" | "failed";

export interface ChatStreamError {
  message: string;
  /** The question that failed, so "Try again" can put it back in the composer. */
  question: string;
}

export interface ChatController {
  status: HistoryStatus;
  loadError: string | null;
  messages: ChatMessage[];
  streaming: boolean;
  /** Id of the assistant message currently being written, if any. */
  streamingId: string | null;
  error: ChatStreamError | null;
  /** Sends a question grounded on `sourceIds`. Ignored while a reply is streaming. */
  send: (text: string, sourceIds: string[]) => void;
  stop: () => void;
  /** Deletes the notebook's chat history. Rejects if the server refuses. */
  clear: () => Promise<void>;
  dismissError: () => void;
}

interface ChatState {
  notebookId: string;
  status: HistoryStatus;
  loadError: string | null;
  messages: ChatMessage[];
  streamingId: string | null;
  error: ChatStreamError | null;
}

function emptyState(notebookId: string): ChatState {
  return { notebookId, status: "loading", loadError: null, messages: [], streamingId: null, error: null };
}

/** The state if it belongs to `notebookId`; otherwise a fresh one, so a previous notebook never leaks in. */
function forNotebook(state: ChatState, notebookId: string): ChatState {
  return state.notebookId === notebookId ? state : emptyState(notebookId);
}

let localCounter = 0;
function localId(prefix: string): string {
  localCounter += 1;
  return `local-${prefix}-${Date.now().toString(36)}-${localCounter}`;
}

/**
 * Chat history and streaming for one notebook. State is tagged with the
 * notebook it belongs to, so a late response from a previous notebook can
 * never write into the current one.
 */
export function useChat(notebookId: string): ChatController {
  const [state, setState] = useState<ChatState>(() => emptyState(notebookId));
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.chat.history(notebookId).then(
      (messages) => {
        if (!cancelled) {
          setState((prev) => ({ ...forNotebook(prev, notebookId), status: "ready", loadError: null, messages }));
        }
      },
      (err: unknown) => {
        if (!cancelled) {
          setState((prev) => ({ ...forNotebook(prev, notebookId), status: "failed", loadError: errorMessage(err) }));
        }
      },
    );
    return () => {
      cancelled = true;
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [notebookId]);

  const send = useCallback(
    (text: string, sourceIds: string[]) => {
      const message = text.trim();
      if (!message || sourceIds.length === 0 || abortRef.current) return;

      const controller = new AbortController();
      abortRef.current = controller;
      const now = new Date().toISOString();
      const assistantId = localId("assistant");
      const userMessage: ChatMessage = { id: localId("user"), notebookId, role: "user", content: message, createdAt: now };
      const assistantMessage: ChatMessage = {
        id: assistantId,
        notebookId,
        role: "assistant",
        content: "",
        citations: [],
        createdAt: now,
      };

      setState((prev) => {
        const base = forNotebook(prev, notebookId);
        return {
          ...base,
          messages: [...base.messages, userMessage, assistantMessage],
          streamingId: assistantId,
          error: null,
        };
      });

      const patchAssistant = (change: (m: ChatMessage) => ChatMessage) =>
        setState((prev) =>
          prev.notebookId !== notebookId
            ? prev
            : { ...prev, messages: prev.messages.map((m) => (m.id === assistantId ? change(m) : m)) },
        );

      void (async () => {
        let finished: ChatMessage | null = null;
        let failure: string | null = null;
        try {
          for await (const event of api.chat.stream(notebookId, { message, sourceIds }, { signal: controller.signal })) {
            if (event.type === "text") {
              const delta = event.delta;
              patchAssistant((m) => ({ ...m, content: m.content + delta }));
            } else if (event.type === "citation") {
              const citation = event.citation;
              patchAssistant((m) => ({ ...m, citations: [...(m.citations ?? []), citation] }));
            } else if (event.type === "done") {
              finished = event.message;
            } else {
              failure = event.error;
              break;
            }
          }
        } catch (err) {
          if (!controller.signal.aborted) failure = errorMessage(err);
        }
        if (!finished && !failure && !controller.signal.aborted) failure = "The answer was interrupted.";

        const aborted = controller.signal.aborted;
        if (abortRef.current === controller) abortRef.current = null;
        const doneMessage = finished;
        const problem = failure;

        setState((prev) => {
          if (prev.notebookId !== notebookId) return prev;
          let messages = prev.messages;
          if (doneMessage) {
            messages = messages.map((m) => (m.id === assistantId ? doneMessage : m));
          } else {
            // A reply that failed before any text arrived leaves nothing behind; partial text is kept.
            const current = messages.find((m) => m.id === assistantId);
            if (current && !current.content && !current.citations?.length) {
              messages = messages.filter((m) => m.id !== assistantId);
            }
          }
          return {
            ...prev,
            messages,
            streamingId: null,
            error: problem && !aborted ? { message: problem, question: message } : prev.error,
          };
        });
      })();
    },
    [notebookId],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const clear = useCallback(async () => {
    abortRef.current?.abort();
    abortRef.current = null;
    await api.chat.clear(notebookId);
    setState((prev) =>
      prev.notebookId === notebookId ? { ...prev, messages: [], streamingId: null, error: null } : prev,
    );
  }, [notebookId]);

  const dismissError = useCallback(() => {
    setState((prev) => (prev.notebookId === notebookId ? { ...prev, error: null } : prev));
  }, [notebookId]);

  const view = forNotebook(state, notebookId);
  return {
    status: view.status,
    loadError: view.loadError,
    messages: view.messages,
    streamingId: view.streamingId,
    streaming: view.streamingId !== null,
    error: view.error,
    send,
    stop,
    clear,
    dismissError,
  };
}
