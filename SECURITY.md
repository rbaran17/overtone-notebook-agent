# Security Policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please do not report security vulnerabilities in a public issue.

Use GitHub's private vulnerability reporting instead: open the repository's
**Security** tab and choose **Report a vulnerability**. Include the affected
version, the steps to reproduce, and the impact you expect.

## Deployment notes

Overtone is designed to run as a single-user service. Before exposing it to
other people:

- The API has one optional bearer key (`OVERTONE_API_KEY` or `--api-key`) and
  no user model. For multi-user use, put it behind your own authentication.
- Without a key the API is for this machine only. It answers when `Host` is
  `localhost`, an IP address or a name in `OVERTONE_ALLOWED_HOSTS`, and refuses
  writes whose `Origin` is another website, so a page you visit cannot drive it
  (cross-site requests, DNS rebinding). `overtone serve` warns when it listens
  beyond loopback without a key and refuses `--cors` for every origin without
  one. Anyone who can connect directly can still use a keyless API: set a key
  before listening on a network.
- The web UI (`npm run dev`, `npm start`) listens on 127.0.0.1 only; pass
  `-H <address>` to change that on purpose. It has no login, and its `/api`
  proxy presents `OVERTONE_API_KEY` to the engine on behalf of whoever reaches
  it. For anything other than loopback, set `OVERTONE_API_KEY` for both the
  engine and the UI server instead of widening host lists, and put the UI
  behind your own authentication.
- The UI's `/api` proxy answers only when `Host` is `localhost`, an IP address
  or a name in `OVERTONE_UI_ALLOWED_HOSTS`, and refuses requests from other
  origins. A reverse proxy in front of it must forward `Host` and
  `X-Forwarded-Proto` unchanged.
- `OVERTONE_API_URL` is reported to UI users in a response header
  (`x-middleware-rewrite`). Do not put credentials in it; the key belongs in
  `OVERTONE_API_KEY`.
- Model output is untrusted: a source can steer what the model writes. The UI
  renders it through one component (`src/components/Markdown.tsx`) that never
  loads images, and sends a Content-Security-Policy limiting images and media
  to its own origin. Keep new rendering paths on that component.
- Keep provider keys on the server. The live-voice flow is designed so the
  OpenAI key never reaches the browser.
- URL ingestion blocks every address that is not on the public internet and
  connects to the address it checked, but run the service with least-privilege
  network access anyway. When `HTTP_PROXY`/`HTTPS_PROXY` is set the proxy
  resolves names, so the address check cannot bind the connection: restrict
  what the proxy may reach.
- Uploaded files are parsed with pypdf and python-docx. Keep these dependencies
  updated. A request is capped at four times the upload limit and a `.docx`
  that unpacks far larger than it is gets refused, but parsing a hostile PDF
  can still use a lot of CPU and memory: run the service with resource limits.
