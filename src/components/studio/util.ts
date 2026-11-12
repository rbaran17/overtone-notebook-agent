/** Pure helpers for the Studio panel. No React, no DOM except downloadText. */

import type { AudioScript, DialogueTurn } from "@/lib/api";

/** Chrome cuts off long utterances; keep each spoken chunk under this many characters. */
export const SPEECH_CHUNK_LIMIT = 200;

/** `m:ss`, or `--:--` when the value is not a finite, non-negative number. */
export function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${minutes}:${secs.toString().padStart(2, "0")}`;
}

/** Split one over-long sentence on word boundaries; hard-slice words longer than the limit. */
function splitLong(sentence: string, limit: number): string[] {
  const out: string[] = [];
  let current = "";
  for (const word of sentence.split(" ")) {
    let rest = word;
    while (rest.length > limit) {
      if (current) {
        out.push(current);
        current = "";
      }
      out.push(rest.slice(0, limit));
      rest = rest.slice(limit);
    }
    if (!rest) continue;
    if (!current) current = rest;
    else if (current.length + 1 + rest.length <= limit) current += ` ${rest}`;
    else {
      out.push(current);
      current = rest;
    }
  }
  if (current) out.push(current);
  return out;
}

/**
 * Break text into speakable chunks of at most `limit` characters, keeping
 * sentences whole where they fit. Whitespace is normalised; no words are dropped.
 */
export function chunkSentences(text: string, limit: number = SPEECH_CHUNK_LIMIT): string[] {
  const normalised = text.replace(/\s+/g, " ").trim();
  if (!normalised) return [];

  const pieces = normalised
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .flatMap((s) => (s.length <= limit ? [s] : splitLong(s, limit)));

  const chunks: string[] = [];
  let current = "";
  for (const piece of pieces) {
    if (!current) current = piece;
    else if (current.length + 1 + piece.length <= limit) current += ` ${piece}`;
    else {
      chunks.push(current);
      current = piece;
    }
  }
  if (current) chunks.push(current);
  return chunks;
}

function turnWeight(turn: Pick<DialogueTurn, "text">): number {
  return Math.max(1, turn.text.length);
}

/**
 * The rendered MP3 has no timestamps, so estimate the turn being spoken from
 * playback progress (0..1) mapped onto cumulative character counts.
 */
export function estimateTurnFromProgress(turns: readonly Pick<DialogueTurn, "text">[], fraction: number): number {
  if (turns.length === 0) return 0;
  const f = Number.isFinite(fraction) ? Math.min(1, Math.max(0, fraction)) : 0;
  const total = turns.reduce((sum, t) => sum + turnWeight(t), 0);
  const target = f * total;
  let acc = 0;
  for (let i = 0; i < turns.length; i++) {
    const weight = turnWeight(turns[i]);
    if (target < acc + weight) return i;
    acc += weight;
  }
  return turns.length - 1;
}

/** Estimated start of a turn as a 0..1 fraction of the whole, matching estimateTurnFromProgress. */
export function turnStartFraction(turns: readonly Pick<DialogueTurn, "text">[], index: number): number {
  if (turns.length === 0) return 0;
  const total = turns.reduce((sum, t) => sum + turnWeight(t), 0);
  const before = turns.slice(0, Math.max(0, index)).reduce((sum, t) => sum + turnWeight(t), 0);
  return before / total;
}

/** Host display name, falling back to "Host N" when the script leaves it blank. */
export function hostName(script: Pick<AudioScript, "hosts">, speaker: number): string {
  return script.hosts[speaker]?.name?.trim() || `Host ${speaker + 1}`;
}

/** Plain-text transcript: title, blank line, summary, blank line, then "Name: text" paragraphs. */
export function transcriptToText(script: AudioScript): string {
  const lines = [script.title, "", script.summary, ""];
  for (const turn of script.turns) {
    lines.push(`${hostName(script, turn.speaker)}: ${turn.text}`, "");
  }
  return lines.join("\n").trimEnd() + "\n";
}

/** Filename-safe slug, lowercase ASCII, at most 60 characters. */
export function slugify(title: string): string {
  const slug = title
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60)
    .replace(/-+$/g, "");
  return slug || "overtone";
}

/** Save text as a file through a temporary object URL. Browser-only. */
export function downloadText(filename: string, text: string, mimeType: string): void {
  const blob = new Blob([text], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
