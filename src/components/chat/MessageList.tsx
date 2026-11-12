"use client";

import { ArrowDown } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { ChatMessage, NotebookGuide } from "@/lib/api";
import { Button, Notice, Spinner } from "@/components/ui";
import { EmptyChat } from "./EmptyChat";
import { MessageView } from "./MessageView";
import type { HistoryStatus } from "./useChat";
import type { GuideStatus } from "./useNotebookGuide";

/** Within this many pixels of the bottom, new content keeps the view pinned to the latest message. */
const NEAR_BOTTOM_PX = 120;

interface MessageListProps {
  notebookId: string;
  historyStatus: HistoryStatus;
  loadError: string | null;
  messages: ChatMessage[];
  streamingId: string | null;
  hasSources: boolean;
  guide: { status: GuideStatus; guide: NotebookGuide | null };
  canAsk: boolean;
  /** Increments on every send, so sending always brings the view to the bottom. */
  sendCount: number;
  onAsk: (question: string) => void;
  onOpenSource: (sourceId: string, quote?: string) => void;
}

/**
 * The scrolling conversation. The scroll container is the PanelFrame body
 * (the nearest `.scroll-quiet` ancestor). It follows new content while the user
 * is near the bottom and otherwise shows a "Jump to latest" button.
 */
export function MessageList({
  notebookId,
  historyStatus,
  loadError,
  messages,
  streamingId,
  hasSources,
  guide,
  canAsk,
  sendCount,
  onAsk,
  onOpenSource,
}: MessageListProps) {
  const listRef = useRef<HTMLDivElement>(null);
  const scrollerRef = useRef<HTMLElement | null>(null);
  const followRef = useRef(true);
  const [showJump, setShowJump] = useState(false);

  // A different notebook starts pinned to the bottom.
  useEffect(() => {
    followRef.current = true;
  }, [notebookId]);

  // Sending always follows the conversation.
  useEffect(() => {
    if (sendCount > 0) followRef.current = true;
  }, [sendCount]);

  useEffect(() => {
    const list = listRef.current;
    const scroller = list?.closest<HTMLElement>(".scroll-quiet") ?? null;
    if (!list || !scroller) return;
    scrollerRef.current = scroller;

    // Recomputes whether we are near the bottom; drives both follow mode and the jump button.
    const sync = () => {
      const distance = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      const near = distance < NEAR_BOTTOM_PX;
      followRef.current = near;
      setShowJump(!near);
    };

    // Content growing (new message, streamed text, first load) is what moves the bottom.
    const observer = new ResizeObserver(() => {
      if (followRef.current) scroller.scrollTop = scroller.scrollHeight;
      sync();
    });
    observer.observe(list);
    scroller.addEventListener("scroll", sync, { passive: true });
    return () => {
      observer.disconnect();
      scroller.removeEventListener("scroll", sync);
      scrollerRef.current = null;
    };
  }, []);

  function jumpToLatest() {
    const scroller = scrollerRef.current;
    followRef.current = true;
    setShowJump(false);
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }

  let body: ReactNode;
  if (historyStatus === "loading") {
    body = (
      <div className="my-auto flex justify-center py-10">
        <Spinner className="size-5" />
      </div>
    );
  } else if (messages.length === 0) {
    body = <EmptyChat hasSources={hasSources} guide={guide} canAsk={canAsk} onAsk={onAsk} />;
  } else {
    body = messages.map((message) => (
      <MessageView
        key={message.id}
        message={message}
        streaming={message.id === streamingId}
        onOpenSource={onOpenSource}
      />
    ));
  }

  return (
    <div ref={listRef} className="flex min-h-full flex-col gap-7 px-4 py-5">
      {historyStatus === "failed" && (
        <Notice tone="danger">
          Earlier messages could not be loaded{loadError ? `: ${loadError}` : "."}
        </Notice>
      )}
      {body}
      {showJump && (
        <div className="sticky bottom-3 flex justify-center">
          <Button
            size="sm"
            variant="secondary"
            className="shadow-panel"
            onClick={jumpToLatest}
            icon={<ArrowDown className="size-4" />}
          >
            Jump to latest
          </Button>
        </div>
      )}
    </div>
  );
}
