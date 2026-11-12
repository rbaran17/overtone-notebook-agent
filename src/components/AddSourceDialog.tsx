"use client";

import { FileText, FileUp, X } from "lucide-react";
import { useId, useRef, useState, type DragEvent, type FormEvent } from "react";
import { api, type Source } from "@/lib/api";
import {
  Button,
  cn,
  errorMessage,
  Field,
  IconButton,
  Input,
  Modal,
  Notice,
  Segmented,
  type SegmentedOption,
  Textarea,
} from "./ui";

export interface AddSourceDialogProps {
  notebookId: string;
  open: boolean;
  onClose: () => void;
  /** Called after any source was added, so the page can refetch. */
  onAdded: () => void | Promise<void>;
}

type Mode = "upload" | "url" | "text";

interface Failure {
  name: string;
  error: string;
}

const MODE_OPTIONS: SegmentedOption<Mode>[] = [
  { value: "upload", label: "Upload" },
  { value: "url", label: "Website" },
  { value: "text", label: "Paste text" },
];

const ACCEPT = ".pdf,.docx,.txt,.md,.markdown,.html,.htm,.csv,.json";
const ACCEPTED_EXTENSIONS = ACCEPT.split(",");

function isAcceptedFile(file: File): boolean {
  const name = file.name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => name.endsWith(ext));
}

function fileKey(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Splits pasted lines into normalised http(s) URLs (https:// is assumed when no scheme is given). */
function parseUrls(text: string): { urls: string[]; invalid: string[] } {
  const urls = new Set<string>();
  const invalid: string[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const candidate = /^[a-z][a-z0-9+.-]*:\/\//i.test(line) ? line : `https://${line}`;
    try {
      const url = new URL(candidate);
      const hostOk = url.hostname.includes(".") || url.hostname === "localhost";
      if ((url.protocol === "http:" || url.protocol === "https:") && hostOk) {
        urls.add(url.href);
      } else {
        invalid.push(line);
      }
    } catch {
      invalid.push(line);
    }
  }
  return { urls: Array.from(urls), invalid };
}

/** `addFiles` attaches per-file failures as a non-enumerable `errors` array. */
function uploadFailuresOf(result: Source[]): Failure[] {
  return (result as unknown as { errors?: Failure[] }).errors ?? [];
}

export function AddSourceDialog({ notebookId, open, onClose, onAdded }: AddSourceDialogProps) {
  const formId = useId();
  const hintId = useId();
  const fileInput = useRef<HTMLInputElement>(null);

  const [mode, setMode] = useState<Mode>("upload");
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [urlText, setUrlText] = useState("");
  const [pasteTitle, setPasteTitle] = useState("");
  const [pasteText, setPasteText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failures, setFailures] = useState<Failure[]>([]);
  const [skipped, setSkipped] = useState<string[]>([]);
  const [wasOpen, setWasOpen] = useState(open);

  // Reset whenever the dialog is reopened. State is adjusted during render (the
  // React-documented pattern) rather than in an effect.
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setMode("upload");
      setFiles([]);
      setDragging(false);
      setUrlText("");
      setPasteTitle("");
      setPasteText("");
      setSubmitting(false);
      setError(null);
      setFailures([]);
      setSkipped([]);
    }
  }

  const { urls, invalid } = parseUrls(urlText);

  const canSubmit =
    !submitting &&
    (mode === "upload"
      ? files.length > 0
      : mode === "url"
        ? urls.length > 0 && invalid.length === 0
        : pasteText.trim().length > 0);

  const submitLabel =
    mode === "upload"
      ? files.length > 0
        ? `Add ${files.length} ${files.length === 1 ? "file" : "files"}`
        : "Add files"
      : mode === "url"
        ? urls.length > 1
          ? `Add ${urls.length} websites`
          : "Add website"
        : "Add text";

  function close() {
    if (!submitting) onClose();
  }

  function addFiles(list: FileList | File[]) {
    const incoming = Array.from(list);
    const accepted = incoming.filter(isAcceptedFile);
    setSkipped(incoming.filter((f) => !isAcceptedFile(f)).map((f) => f.name));
    setFiles((prev) => {
      const seen = new Set(prev.map(fileKey));
      return [...prev, ...accepted.filter((f) => !seen.has(fileKey(f)))];
    });
    setError(null);
  }

  function removeFile(key: string) {
    setFiles((prev) => prev.filter((f) => fileKey(f) !== key));
  }

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
    setFailures([]);
  }

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    setFailures([]);
    try {
      if (mode === "upload") {
        // One call for the whole batch; it throws only when every file failed.
        const result = await api.sources.addFiles(notebookId, files);
        const failed = uploadFailuresOf(result);
        await onAdded();
        if (failed.length === 0) {
          onClose();
        } else {
          setFailures(failed);
          setFiles((prev) => prev.filter((f) => failed.some((x) => x.name === f.name)));
        }
      } else if (mode === "url") {
        const added: string[] = [];
        const failedUrls: Failure[] = [];
        for (const url of urls) {
          try {
            await api.sources.add(notebookId, { type: "url", url });
            added.push(url);
          } catch (err) {
            failedUrls.push({ name: url, error: errorMessage(err) });
          }
        }
        if (added.length > 0) await onAdded();
        if (failedUrls.length === 0) {
          onClose();
        } else {
          setFailures(failedUrls);
          if (added.length === 0) setError("None of the websites could be added.");
          // Keep only the lines that failed so they can be fixed and retried.
          setUrlText(failedUrls.map((f) => f.name).join("\n"));
        }
      } else {
        const title = pasteTitle.trim();
        await api.sources.add(notebookId, { type: "text", text: pasteText, title: title || undefined });
        await onAdded();
        onClose();
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={close}
      size="lg"
      title="Add sources"
      description="Files, web pages or pasted text."
      footer={
        <>
          <Button onClick={close} disabled={submitting}>
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="primary" loading={submitting} disabled={!canSubmit}>
            {submitLabel}
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} className="space-y-5">
        <Segmented label="Source type" value={mode} onChange={switchMode} options={MODE_OPTIONS} />

        {error && <Notice>{error}</Notice>}

        {failures.length > 0 && (
          <Notice>
            <p className="font-medium">
              {failures.length === 1 ? "One source could not be added." : `${failures.length} sources could not be added.`}
            </p>
            <ul className="mt-1.5 space-y-1">
              {failures.map((f, i) => (
                <li key={`${f.name}-${i}`} className="break-words">
                  <span className="font-medium">{f.name}</span>: {f.error}
                </li>
              ))}
            </ul>
          </Notice>
        )}

        {mode === "upload" && (
          <div className="space-y-4">
            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e: DragEvent<HTMLButtonElement>) => {
                e.preventDefault();
                setDragging(false);
                addFiles(e.dataTransfer.files);
              }}
              aria-describedby={hintId}
              className={cn(
                "flex w-full flex-col items-center justify-center rounded-2xl border border-dashed px-6 py-12 text-center transition-colors",
                dragging ? "border-accent bg-accent-soft" : "border-line-strong bg-surface-2 hover:border-accent",
              )}
            >
              <span className="pointer-events-none flex flex-col items-center gap-2">
                <span className="flex size-11 items-center justify-center rounded-2xl bg-surface text-ink-2">
                  <FileUp className="size-5" aria-hidden />
                </span>
                <span className="text-sm font-medium text-ink">Drop files here, or browse</span>
                <span id={hintId} className="text-xs text-ink-3">
                  PDF, Word, text, Markdown, HTML, CSV or JSON
                </span>
              </span>
            </button>
            <input
              ref={fileInput}
              type="file"
              multiple
              accept={ACCEPT}
              className="hidden"
              onChange={(e) => {
                addFiles(e.target.files ?? []);
                e.target.value = "";
              }}
            />

            {skipped.length > 0 && (
              <Notice tone="info" onDismiss={() => setSkipped([])}>
                Skipped unsupported files: {skipped.join(", ")}
              </Notice>
            )}

            {files.length > 0 && (
              <ul className="divide-y divide-line rounded-xl border border-line">
                {files.map((file) => {
                  const key = fileKey(file);
                  return (
                    <li key={key} className="flex items-center gap-3 px-3 py-2">
                      <FileText className="size-4 shrink-0 text-ink-3" aria-hidden />
                      <span className="min-w-0 flex-1 truncate text-sm text-ink">{file.name}</span>
                      <span className="shrink-0 font-mono text-[11px] text-ink-3">{formatBytes(file.size)}</span>
                      <IconButton label={`Remove ${file.name}`} onClick={() => removeFile(key)}>
                        <X className="size-4" aria-hidden />
                      </IconButton>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}

        {mode === "url" && (
          <div className="space-y-2">
            <Field label="Web addresses" hint="One per line">
              <Textarea
                value={urlText}
                onChange={(e) => setUrlText(e.target.value)}
                rows={5}
                spellCheck={false}
                placeholder={"https://example.com/article\nen.wikipedia.org/wiki/Topic"}
                aria-invalid={invalid.length > 0 ? true : undefined}
              />
            </Field>
            {invalid.length > 0 ? (
              <p className="text-[13px] text-danger">
                Not a web address: {invalid.slice(0, 3).join(", ")}
                {invalid.length > 3 ? ` and ${invalid.length - 3} more` : ""}
              </p>
            ) : (
              <p className="text-xs text-ink-3">If you leave off the scheme, https:// is assumed.</p>
            )}
          </div>
        )}

        {mode === "text" && (
          <div className="space-y-4">
            <Field label="Title" hint="Optional">
              <Input
                value={pasteTitle}
                onChange={(e) => setPasteTitle(e.target.value)}
                maxLength={200}
                placeholder="For example, meeting notes, 12 March"
              />
            </Field>
            <Field label="Text">
              <Textarea
                value={pasteText}
                onChange={(e) => setPasteText(e.target.value)}
                rows={12}
                placeholder="Paste or type the text Overtone should work from"
              />
            </Field>
          </div>
        )}
      </form>
    </Modal>
  );
}
