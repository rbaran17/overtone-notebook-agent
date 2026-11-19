/**
 * Largest file the engine accepts: DEFAULT_MAX_UPLOAD in backend/overtone/api.py.
 *
 * The UI refuses bigger files before sending them, and next.config.ts sizes the
 * /api proxy's request buffer from this number, so keep it in step with the engine.
 * next.config.ts imports this file: keep it free of other imports.
 */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;
