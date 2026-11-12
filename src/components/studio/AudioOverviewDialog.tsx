"use client";

import { Check } from "lucide-react";
import { useState } from "react";
import { Button, Field, Input, Modal, Notice, Segmented, Textarea, cn, errorMessage } from "@/components/ui";
import {
  api,
  type Artifact,
  type AudioFormat,
  type AudioHost,
  type AudioLength,
  type Capabilities,
} from "@/lib/api";

const FORMATS: { value: AudioFormat; name: string; description: string }[] = [
  { value: "deep_dive", name: "Deep dive", description: "Two hosts unpack the material in a lively conversation" },
  { value: "brief", name: "Brief", description: "A fast two-host rundown of the key takeaways" },
  { value: "debate", name: "Debate", description: "Two hosts argue opposing readings of the sources" },
  { value: "critique", name: "Critique", description: "Two reviewers assess strengths, gaps and weak claims" },
  { value: "solo", name: "Solo", description: "One narrator walks through it — a voice overview" },
];

const LENGTHS: { value: AudioLength; label: string }[] = [
  { value: "short", label: "Short" },
  { value: "medium", label: "Medium" },
  { value: "long", label: "Long" },
];

const HOST_PLACEHOLDERS = ["Maya", "Jonah"];

const SELECT_CLASS =
  "h-10 w-full rounded-xl border border-line bg-surface px-3 text-sm text-ink transition-colors hover:border-line-strong focus:border-accent focus:outline-none";

export interface AudioOverviewDialogProps {
  notebookId: string;
  /** Ids of the sources ticked in the Sources panel. */
  sourceIds: string[];
  capabilities: Capabilities | null;
  onClose: () => void;
  onCreated: (artifact: Artifact) => void;
}

/**
 * Customize-and-generate form for an audio overview. Mount it only while it
 * should be open: its state starts fresh each time.
 */
export function AudioOverviewDialog({ notebookId, sourceIds, capabilities, onClose, onCreated }: AudioOverviewDialogProps) {
  const tts = capabilities?.tts ?? null;
  const defaults = tts?.defaultVoices ?? [];
  const voiceOptions = tts?.voices ?? [];
  const defaultVoice = (i: number) => defaults[i] ?? voiceOptions[i]?.id ?? voiceOptions[0]?.id ?? "";

  const [format, setFormat] = useState<AudioFormat>("deep_dive");
  const [length, setLength] = useState<AudioLength>("medium");
  const [names, setNames] = useState<string[]>(["", ""]);
  const [voices, setVoices] = useState<string[]>(() => [defaultVoice(0), defaultVoice(1)]);
  const [language, setLanguage] = useState("");
  const [instructions, setInstructions] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hostCount = format === "solo" ? 1 : 2;
  const customized =
    names.some((n) => n.trim() !== "") ||
    (tts !== null && voices.some((v, i) => v !== defaultVoice(i)));
  const canSubmit = sourceIds.length > 0 && !submitting;

  function buildHosts(): AudioHost[] | undefined {
    if (!customized) return undefined;
    // Hosts are matched by position, so send every host once any of them is customized.
    return Array.from({ length: hostCount }, (_, i) => ({
      name: names[i]?.trim() ?? "",
      ...(tts && voices[i] ? { voice: voices[i] } : {}),
    }));
  }

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      const artifact = await api.artifacts.create(notebookId, {
        kind: "audio_overview",
        sourceIds,
        options: {
          format,
          length,
          hosts: buildHosts(),
          language: language.trim() || undefined,
          instructions: instructions.trim() || undefined,
        },
      });
      onCreated(artifact);
      onClose();
    } catch (err) {
      setError(errorMessage(err));
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="Customize audio overview"
      size="lg"
      footer={
        <>
          {sourceIds.length === 0 && <p className="mr-auto text-[13px] text-ink-3">Select sources to generate.</p>}
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={submitting} disabled={!canSubmit} onClick={submit}>
            Generate
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-6">
        {error && <Notice onDismiss={() => setError(null)}>{error}</Notice>}

        <fieldset className="flex flex-col gap-2">
          <legend className="mb-2 text-[13px] font-medium text-ink">Format</legend>
          <div role="radiogroup" aria-label="Format" className="grid gap-2 sm:grid-cols-2">
            {FORMATS.map((f) => {
              const selected = f.value === format;
              return (
                <button
                  key={f.value}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  onClick={() => setFormat(f.value)}
                  className={cn(
                    "flex items-start gap-3 rounded-xl border p-3 text-left transition-colors",
                    selected ? "border-accent bg-accent-soft" : "border-line bg-surface hover:border-line-strong hover:bg-surface-2",
                  )}
                >
                  <span
                    aria-hidden
                    className={cn(
                      "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border",
                      selected ? "border-accent bg-accent text-accent-ink" : "border-line-strong",
                    )}
                  >
                    {selected && <Check className="size-3" strokeWidth={3} />}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-[14px] font-medium text-ink">{f.name}</span>
                    <span className="mt-0.5 block text-[12px] leading-relaxed text-ink-2">{f.description}</span>
                  </span>
                </button>
              );
            })}
          </div>
        </fieldset>

        <div className="flex flex-col gap-2">
          <span className="text-[13px] font-medium text-ink">Length</span>
          <Segmented value={length} onChange={setLength} options={LENGTHS} label="Length" />
          <span className="text-xs text-ink-3">Roughly 3, 10 or 20 minutes of audio.</span>
        </div>

        <div className="flex flex-col gap-3">
          <span className="text-[13px] font-medium text-ink">Hosts</span>
          {Array.from({ length: hostCount }, (_, i) => (
            <div key={i} className="grid gap-3 rounded-xl border border-line p-3 sm:grid-cols-2">
              <Field label={`Host ${i + 1} name`} hint="Optional">
                <Input
                  value={names[i] ?? ""}
                  placeholder={HOST_PLACEHOLDERS[i]}
                  maxLength={40}
                  onChange={(e) => setNames((prev) => prev.map((n, j) => (j === i ? e.target.value : n)))}
                  className="mt-0"
                />
              </Field>
              {tts && (
                <label className="block">
                  <span className="mb-1.5 block text-[13px] font-medium text-ink">Host {i + 1} voice</span>
                  <select
                    value={voices[i] ?? ""}
                    onChange={(e) => setVoices((prev) => prev.map((v, j) => (j === i ? e.target.value : v)))}
                    className={SELECT_CLASS}
                  >
                    {tts.voices.map((v) => (
                      <option key={v.id} value={v.id}>
                        {v.label}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
          ))}
        </div>

        <Field label="Language" hint="Optional">
          <Input value={language} onChange={(e) => setLanguage(e.target.value)} placeholder="English" maxLength={40} />
        </Field>

        <Field label="Focus">
          <Textarea
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="What should the hosts focus on? Who is listening?"
            rows={3}
            maxLength={1000}
          />
        </Field>
      </div>
    </Modal>
  );
}
