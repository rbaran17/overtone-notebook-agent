import type { Source } from "./api";
import { MAX_UPLOAD_BYTES } from "./limits";

const MB = 1024 * 1024;

/** A file that was not added, and why, in words to show the user. */
export interface UploadFailure {
  name: string;
  error: string;
}

/** The per-file upload limit as shown to the user, e.g. "50 MB". */
export const MAX_UPLOAD_LABEL = `${Math.round(MAX_UPLOAD_BYTES / MB)} MB`;

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < MB) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / MB).toFixed(1)} MB`;
}

/**
 * Splits files into those small enough to upload and those over the engine's limit.
 *
 * Checked here, before anything is sent, because the /api proxy does not refuse an
 * oversized request: it forwards the first part of the body, the engine waits for the
 * rest, and the upload ends in a bare 500 when the proxy gives up.
 */
export function splitBySize(files: File[]): { sendable: File[]; tooLarge: UploadFailure[] } {
  const sendable: File[] = [];
  const tooLarge: UploadFailure[] = [];
  for (const file of files) {
    if (file.size <= MAX_UPLOAD_BYTES) {
      sendable.push(file);
    } else {
      // Rounded up, so a file just over the limit never reads as exactly the limit.
      const size = (Math.ceil((file.size / MB) * 10) / 10).toFixed(1);
      tooLarge.push({ name: file.name, error: `Too large (${size} MB). The limit is ${MAX_UPLOAD_LABEL} per file.` });
    }
  }
  return { sendable, tooLarge };
}

/** `addFiles` attaches per-file failures as a non-enumerable `errors` array. */
export function uploadFailuresOf(result: Source[]): UploadFailure[] {
  return (result as unknown as { errors?: UploadFailure[] }).errors ?? [];
}
