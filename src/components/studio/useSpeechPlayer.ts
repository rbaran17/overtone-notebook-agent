"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { AudioScript, DialogueTurn } from "@/lib/api";
import { chunkSentences } from "./util";

/** Playback-speed options for browser voices (applied to the next utterances). */
export const SPEECH_RATES: number[] = [0.9, 1, 1.15, 1.3];

export interface SpeechPlayerControls {
  supported: boolean;
  /** Speaking now (false while paused or stopped). */
  playing: boolean;
  paused: boolean;
  /** Index of the turn being spoken, or the turn to resume from. */
  currentTurn: number;
  play(): void;
  pause(): void;
  resume(): void;
  stop(): void;
  jumpTo(turnIndex: number): void;
  rate: number;
  setRate(rate: number): void;
}

function getSynth(): SpeechSynthesis | null {
  if (typeof window === "undefined" || !("speechSynthesis" in window)) return null;
  return window.speechSynthesis;
}

const noopSubscribe = () => () => {};

/** Hydration-safe: the server render reports "unsupported", the client reports the truth. */
function useSpeechSupported(): boolean {
  return useSyncExternalStore(
    noopSubscribe,
    () => getSynth() !== null,
    () => false,
  );
}

const FEMALE_HINT = /female|samantha|zira|karen|susan|hazel|victoria|moira|tessa|allison|\bava\b|fiona|serena|kathy|jenny|aria|nicole|emma|joanna|salli|sara/i;
const MALE_HINT = /\bmale\b|david|mark|daniel|alex|fred|\btom\b|george|james|guy|ryan|sean|liam|brian|justin|matthew|thomas/i;

/**
 * Two voices for the two hosts, preferring the browser language. When names
 * hint at gender, host 1 gets a female-sounding voice and host 2 a male one.
 * The second entry is null when only one voice exists.
 */
function pickHostVoices(all: SpeechSynthesisVoice[], language: string): [SpeechSynthesisVoice | null, SpeechSynthesisVoice | null] {
  const prefix = (language || "en").split("-")[0].toLowerCase();
  const byLang = (p: string) => all.filter((v) => v.lang.toLowerCase().startsWith(p));
  let pool = byLang(prefix);
  if (pool.length === 0) pool = byLang("en");
  if (pool.length === 0) pool = all;
  if (pool.length === 0) return [null, null];

  const first = pool.find((v) => FEMALE_HINT.test(v.name)) ?? pool[0];
  const second = pool.find((v) => v !== first && MALE_HINT.test(v.name)) ?? pool.find((v) => v !== first) ?? null;
  return [first, second];
}

/**
 * Reads an audio overview aloud with window.speechSynthesis, one turn at a
 * time. Each turn is split into sentence chunks; the next turn starts when the
 * last chunk of the current one ends. A generation counter stops cancelled
 * utterances from advancing the queue.
 */
export function useSpeechPlayer(script: AudioScript | undefined): SpeechPlayerControls {
  const supported = useSpeechSupported();

  const turnsRef = useRef<DialogueTurn[]>([]);
  const voicesRef = useRef<SpeechSynthesisVoice[]>([]);
  const generation = useRef(0);
  const queueRef = useRef<SpeechSynthesisUtterance[]>([]);
  const rateRef = useRef(1);

  const [playing, setPlaying] = useState(false);
  const [paused, setPaused] = useState(false);
  const [currentTurn, setCurrentTurn] = useState(0);
  const [rate, setRateState] = useState(1);

  useEffect(() => {
    turnsRef.current = script?.turns ?? [];
  }, [script]);

  useEffect(() => {
    const synth = getSynth();
    if (!synth) return;
    const load = () => {
      voicesRef.current = synth.getVoices();
    };
    load();
    synth.addEventListener("voiceschanged", load);
    return () => synth.removeEventListener("voiceschanged", load);
  }, []);

  // Cancel speech when the player unmounts (the panel keys the player by artifact).
  useEffect(() => {
    const gen = generation;
    return () => {
      gen.current += 1;
      getSynth()?.cancel();
    };
  }, []);

  function speakTurn(index: number, gen: number) {
    const synth = getSynth();
    if (!synth || gen !== generation.current) return;
    const turns = turnsRef.current;
    if (index >= turns.length) {
      queueRef.current = [];
      setPlaying(false);
      setPaused(false);
      setCurrentTurn(0);
      return;
    }
    setCurrentTurn(index);

    const turn = turns[index];
    const chunks = chunkSentences(turn.text);
    if (chunks.length === 0) {
      speakTurn(index + 1, gen);
      return;
    }

    const all = synth.getVoices();
    const [first, second] = pickHostVoices(all.length ? all : voicesRef.current, navigator.language);
    const slot = turn.speaker % 2;
    const voice = slot === 0 ? first : (second ?? first);
    const distinct = second !== null && second !== first;
    const pitch = slot === 1 && !distinct ? 0.85 : 1;

    const utterances = chunks.map((text, j) => {
      const utterance = new SpeechSynthesisUtterance(text);
      if (voice) {
        utterance.voice = voice;
        utterance.lang = voice.lang;
      }
      utterance.rate = rateRef.current;
      utterance.pitch = pitch;
      if (j === chunks.length - 1) {
        const next = () => {
          if (gen === generation.current) speakTurn(index + 1, gen);
        };
        utterance.onend = next;
        utterance.onerror = next;
      }
      return utterance;
    });

    // Keep references so the browser does not garbage-collect pending utterances.
    queueRef.current = utterances;
    for (const utterance of utterances) synth.speak(utterance);
  }

  function speakFrom(index: number) {
    const synth = getSynth();
    if (!synth) return;
    generation.current += 1;
    synth.cancel();
    synth.resume();
    setPaused(false);
    setPlaying(true);
    speakTurn(index, generation.current);
  }

  function resume() {
    const synth = getSynth();
    if (!synth || !paused) return;
    synth.resume();
    setPaused(false);
    setPlaying(true);
  }

  function play() {
    if (playing) return;
    if (paused) {
      resume();
      return;
    }
    speakFrom(currentTurn);
  }

  function pause() {
    const synth = getSynth();
    if (!synth || !playing) return;
    synth.pause();
    setPlaying(false);
    setPaused(true);
  }

  function stop() {
    generation.current += 1;
    getSynth()?.cancel();
    queueRef.current = [];
    setPlaying(false);
    setPaused(false);
    setCurrentTurn(0);
  }

  function jumpTo(index: number) {
    const last = Math.max(0, turnsRef.current.length - 1);
    speakFrom(Math.min(last, Math.max(0, index)));
  }

  function setRate(next: number) {
    rateRef.current = next;
    setRateState(next);
  }

  return { supported, playing, paused, currentTurn, play, pause, resume, stop, jumpTo, rate, setRate };
}
