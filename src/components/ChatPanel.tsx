"use client";

import { Mic, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Capabilities, type Source } from "@/lib/api";
import { LivePanel } from "./live/LivePanel";
import { useLiveVoice } from "./live/useLiveVoice";
import { Composer } from "./chat/Composer";
import { MessageList } from "./chat/MessageList";
import { useChat } from "./chat/useChat";
import { useNotebookGuide } from "./chat/useNotebookGuide";
import { Button, IconButton, Modal, Notice, PanelFrame, errorMessage } from "./ui";

export interface ChatPanelProps {
  notebookId: string;
  sources: Source[];
  /** Sources ticked in the Sources panel; chat is grounded on these. */
  selectedIds: Set<string>;
  /** Open the source viewer at a cited passage (owned by the page). */
  onOpenSource: (sourceId: string, quote?: string) => void;
  /** Engine capabilities; `live` is null when voice is not configured. Null while loading. */
  capabilities: Capabilities | null;
  className?: string;
}

export function ChatPanel({ notebookId, sources, selectedIds, onOpenSource, capabilities, className }: ChatPanelProps) {
  const chat = useChat(notebookId);

  const selected = useMemo(
    () => sources.filter((s) => selectedIds.has(s.id)).map((s) => s.id),
    [sources, selectedIds],
  );
  const selectedCount = selected.length;
  const sourceKey = useMemo(() => sources.map((s) => s.id).sort().join("\n"), [sources]);

  // A voice conversation belongs to one notebook: switching notebooks ends it.
  const liveAudioRef = useRef<HTMLAudioElement>(null);
  const live = useLiveVoice(liveAudioRef);
  const { start: startLive, stop: stopLive } = live;
  const [liveOpen, setLiveOpen] = useState(false);
  const [liveNotebook, setLiveNotebook] = useState(notebookId);
  if (liveNotebook !== notebookId) {
    setLiveNotebook(notebookId);
    setLiveOpen(false);
  }
  useEffect(() => () => void stopLive(), [notebookId, stopLive]);

  // The guide is shown only in the empty state, so it is fetched only then.
  const pristine = chat.status !== "loading" && chat.messages.length === 0;
  const guide = useNotebookGuide(notebookId, sourceKey, pristine && sources.length > 0);

  // The draft belongs to one notebook; switching notebooks starts a blank one.
  const [draftState, setDraftState] = useState({ notebookId, text: "" });
  const draft = draftState.notebookId === notebookId ? draftState.text : "";
  const setDraft = (text: string) => setDraftState({ notebookId, text });

  const [sendCount, setSendCount] = useState(0);
  const [clearOpen, setClearOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);

  const canSend = selectedCount > 0 && chat.status !== "loading" && !chat.streaming;

  function submit(text: string): boolean {
    if (!text.trim() || !canSend) return false;
    chat.send(text, selected);
    setSendCount((n) => n + 1);
    return true;
  }

  function handleSend() {
    if (submit(draft)) setDraft("");
  }

  function retry() {
    if (chat.error) setDraft(chat.error.question);
    chat.dismissError();
  }

  function openClear() {
    setClearError(null);
    setClearOpen(true);
  }

  function closeClear() {
    if (!clearing) setClearOpen(false);
  }

  async function confirmClear() {
    setClearing(true);
    setClearError(null);
    try {
      await chat.clear();
      setClearOpen(false);
    } catch (err) {
      setClearError(errorMessage(err));
    } finally {
      setClearing(false);
    }
  }

  const placeholder =
    sources.length === 0
      ? "Add sources to start chatting"
      : selectedCount === 0
        ? "Select at least one source to chat"
        : "Ask about your sources";

  // Why "Talk" is unavailable, or null when it can start.
  const talkBlocked = liveOpen
    ? "A voice conversation is already open"
    : capabilities === null
      ? "Checking voice support…"
      : !capabilities.live
        ? "Live voice needs an OPENAI_API_KEY on the server"
        : selectedCount === 0
          ? "Select at least one source to talk"
          : null;

  function startTalk() {
    setLiveOpen(true);
    void startLive({ client: api, notebookId, mode: "conversation", sourceIds: [...selected] });
  }

  /** Ends the call, then hands the composer back once the session has closed. */
  function endTalk() {
    void stopLive().finally(() => setLiveOpen(false));
  }

  return (
    <PanelFrame
      title="Chat"
      meta={sources.length > 0 ? `${selectedCount} of ${sources.length} sources` : undefined}
      className={className}
      actions={
        <>
          <Button
            size="sm"
            icon={<Mic className="size-4" />}
            disabled={talkBlocked !== null}
            title={talkBlocked ?? "Talk to your notebook"}
            onClick={startTalk}
          >
            Talk
          </Button>
          {chat.messages.length > 0 && (
            <IconButton label="Clear chat" onClick={openClear}>
              <Trash2 className="size-4" />
            </IconButton>
          )}
        </>
      }
      footer={
        liveOpen ? (
          <LivePanel
            state={live.state}
            title="Talk to your notebook"
            hint="Voice conversations aren't saved to the chat."
            onStop={endTalk}
            onToggleMute={() => live.setMuted(!live.state.muted)}
            onOpenSource={onOpenSource}
          />
        ) : (
          <>
            {chat.error && (
              <div className="px-3 pt-3">
                <Notice onDismiss={chat.dismissError}>
                  <div className="flex items-center justify-between gap-3">
                    <span>Could not finish that answer. {chat.error.message}</span>
                    <Button size="sm" onClick={retry}>
                      Try again
                    </Button>
                  </div>
                </Notice>
              </div>
            )}
            <Composer
              value={draft}
              onChange={setDraft}
              onSend={handleSend}
              onStop={chat.stop}
              streaming={chat.streaming}
              disabled={selectedCount === 0 || chat.status === "loading"}
              placeholder={placeholder}
            />
          </>
        )
      }
    >
      <audio ref={liveAudioRef} autoPlay className="hidden" />
      <MessageList
        notebookId={notebookId}
        historyStatus={chat.status}
        loadError={chat.loadError}
        messages={chat.messages}
        streamingId={chat.streamingId}
        hasSources={sources.length > 0}
        guide={guide}
        canAsk={canSend}
        sendCount={sendCount}
        onAsk={(question) => submit(question)}
        onOpenSource={onOpenSource}
      />
      <Modal
        open={clearOpen}
        onClose={closeClear}
        title="Clear this chat?"
        description="Removes every message in this notebook's chat. Your sources are not affected."
        footer={
          <>
            <Button onClick={closeClear} disabled={clearing}>
              Cancel
            </Button>
            <Button variant="danger" loading={clearing} onClick={confirmClear}>
              Clear chat
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">This cannot be undone.</p>
        {clearError && (
          <Notice className="mt-4" onDismiss={() => setClearError(null)}>
            {clearError}
          </Notice>
        )}
      </Modal>
    </PanelFrame>
  );
}
