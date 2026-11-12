/**
 * Pure helpers for the `[n]` citation markers in assistant answers.
 * No React here, so they can be checked in isolation.
 */

import type { Citation } from "@/lib/api";

/** Fenced blocks (an unterminated fence runs to the end, as it does while streaming) and inline code spans. */
const CODE = /(```[\s\S]*?(?:```|$)|`[^`\n]+`)/;

/** Applies `change` to the prose parts of `text`, leaving code untouched. */
function mapOutsideCode(text: string, change: (prose: string) => string): string {
  // With a capture group, split() alternates prose (even) and code (odd).
  return text
    .split(CODE)
    .map((part, i) => (i % 2 === 1 ? part : change(part)))
    .join("");
}

/** The citation indices as a set, for the helpers below. */
export function indexSet(citations: Citation[] | undefined): Set<number> {
  return new Set((citations ?? []).map((c) => c.index));
}

/**
 * Rewrites each `[n]` whose n is a known citation into the Markdown link
 * `[n](#cite-n)`, which the answer renderer turns into a chip. Unknown markers,
 * markers inside code, and a partial marker still streaming in (`[1`) are left alone.
 */
export function linkifyCitations(text: string, indices: ReadonlySet<number>): string {
  if (indices.size === 0 || !text.includes("[")) return text;
  return mapOutsideCode(text, (prose) =>
    prose.replace(/\[(\d+)\](?!\()/g, (marker, digits: string) =>
      indices.has(Number(digits)) ? `[${digits}](#cite-${digits})` : marker,
    ),
  );
}

/** Removes the known citation markers (with the space before them), for copying an answer as plain Markdown. */
export function stripCitationMarkers(text: string, indices: ReadonlySet<number>): string {
  if (indices.size === 0 || !text.includes("[")) return text;
  return mapOutsideCode(text, (prose) =>
    prose.replace(/[ \t]*\[(\d+)\](?!\()/g, (marker, digits: string) =>
      indices.has(Number(digits)) ? "" : marker,
    ),
  );
}
