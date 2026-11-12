"use client";

import { LiveVoiceSession, type LiveVoiceOptions, type LiveVoiceState } from "@overtone/client";
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

/** Everything a session needs except the callbacks the hook owns. */
export type LiveStartOptions = Omit<LiveVoiceOptions, "onState" | "onRemoteStream">;

export interface LiveVoiceControls {
  state: LiveVoiceState;
  /** Starts a session, ending any previous one. Call from a click so the microphone prompt is allowed. */
  start: (options: LiveStartOptions) => Promise<void>;
  /** Ends the current session. Resolves once it has closed. */
  stop: () => Promise<void>;
  setMuted: (muted: boolean) => void;
}

const IDLE: LiveVoiceState = { status: "idle", transcript: [], citations: [], muted: false };

/**
 * Owns at most one LiveVoiceSession and mirrors its state into React. The caller
 * renders a hidden `<audio autoPlay>` and passes its ref in; the remote voice is
 * played through it. Updates from a session that has been replaced are ignored,
 * and the session is stopped when the component unmounts.
 */
export function useLiveVoice(audioRef: RefObject<HTMLAudioElement | null>): LiveVoiceControls {
  const [state, setState] = useState<LiveVoiceState>(IDLE);
  const sessionRef = useRef<LiveVoiceSession | null>(null);

  const start = useCallback(async (options: LiveStartOptions) => {
    const previous = sessionRef.current;
    const session: LiveVoiceSession = new LiveVoiceSession({
      ...options,
      onState: (next) => {
        if (sessionRef.current === session) setState(next);
      },
      onRemoteStream: (stream) => {
        const audio = audioRef.current;
        if (!audio || sessionRef.current !== session) return;
        audio.srcObject = stream;
        audio.play().catch(() => {});
      },
    });
    sessionRef.current = session;
    setState(session.state);
    void previous?.stop();
    await session.start();
  }, [audioRef]);

  const stop = useCallback(async () => {
    await sessionRef.current?.stop();
  }, []);

  const setMuted = useCallback((muted: boolean) => {
    sessionRef.current?.setMuted(muted);
  }, []);

  useEffect(() => {
    const sessions = sessionRef;
    return () => {
      void sessions.current?.stop();
      sessions.current = null;
    };
  }, []);

  return { state, start, stop, setMuted };
}
