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
- Keep provider keys on the server. The live-voice flow is designed so the
  OpenAI key never reaches the browser.
- URL ingestion blocks private and loopback addresses, but run the service with
  least-privilege network access anyway.
- Uploaded files are parsed with pypdf and python-docx. Keep these dependencies
  updated.
