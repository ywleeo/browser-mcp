# Security Policy

## Supported Versions

| Version | Supported |
| --- | --- |
| 0.12.x | ✅ |
| < 0.12.0 | ❌ |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security problems.

Instead, report privately via a
[GitHub security advisory](https://github.com/ywleeo/browser-mcp/security/advisories/new), or email the
maintainers directly.

When reporting, please include:

- The affected version and how it was installed (`pip`, `uvx`, or from source).
- A description of the issue and the steps to reproduce it.
- Any relevant screenshots or logs, with secrets redacted (in particular the local
  `pairing-token`, `pairing.json`, or any `PYPI_API_TOKEN`).

## Local pairing credentials

The extension and MCP server pair over localhost. `pairing.json` and the `pairing-token` are local
connection credentials — **never share them**. They grant local control of the bridge and should only
ever exist on your own machine.

We acknowledge reports within 3 business days and aim to fix validated security issues promptly.
