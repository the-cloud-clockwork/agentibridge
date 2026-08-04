# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.2.x   | :white_check_mark: |
| < 0.2   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in AgentiBridge, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please email the maintainers or use [GitHub's private vulnerability reporting](https://github.com/The-Cloud-Clockwork/agentibridge/security/advisories/new).

### What to include

- A description of the vulnerability
- Steps to reproduce the issue
- The potential impact
- Any suggested fixes (optional)

### Response timeline

- **Acknowledgment**: Within 48 hours of receipt
- **Assessment**: Within 7 days
- **Fix or mitigation**: Target within 30 days for confirmed vulnerabilities

### Disclosure

We follow responsible disclosure practices:

1. The reporter is credited (unless they prefer anonymity)
2. A fix is developed and tested before public disclosure
3. A security advisory is published with the fix release

## Security Best Practices

When deploying AgentiBridge:

- **Always set `AGENTIBRIDGE_API_KEYS`** when exposing the SSE/HTTP transport to a network
- **Use HTTPS** via a reverse proxy (nginx, Caddy) or Cloudflare Tunnel for production
- **Restrict network access** using firewalls or Cloudflare Access policies
- **Keep dependencies updated** — enable Dependabot for automated security updates
- **Review OAuth configuration** if using OAuth 2.1 — ensure redirect URIs are tightly scoped
- **Bind to localhost** by default (`AGENTIBRIDGE_HOST=localhost`) and only open to networks when needed — spell it `localhost`, not `127.0.0.1`: some enterprise Claude Code policies silently drop client entries whose url names the dotted quad

## Scope

This security policy covers the `agentibridge` Python package and its official Docker image. Third-party integrations, plugins, or deployment configurations are outside the scope of this policy.
