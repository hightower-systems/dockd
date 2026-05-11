# Security Policy

## Reporting a Vulnerability

If you believe you've found a security vulnerability in Dockd, please report
it privately so we can address it before public disclosure.

**Do not open a public GitHub issue for security reports.** Open a GitHub
Security Advisory at
https://github.com/hightower-systems/dockd/security/advisories/new
instead, or email the maintainer if you cannot use the advisory flow.

Include:

- A description of the issue and the impact you observed
- Reproduction steps (request shape, configuration, version)
- Whether the vulnerability is exposed at the network boundary, requires an
  authenticated session, or requires admin role

We aim to acknowledge reports within 72 hours and to issue a fix within
30 days for confirmed high-severity issues. Public credit is offered (and
defaults to the reporter's GitHub handle) unless you prefer to remain
anonymous.

## Scope

In scope:

- Authentication / authorization bypass on dockd routes
- Privilege escalation between `user` and `admin` roles
- Secret leakage through logs, HTTP responses, or settings file permissions
- Server-side request forgery in the order-backend or ShipRush call paths
- Injection vulnerabilities (XSS, header injection, etc.)
- Idempotency bypass that could cause double-ship or double-void

Out of scope:

- Findings that require an already-compromised station laptop
- Findings against unmaintained branches (only `main` is supported)
- Social engineering of warehouse operators
- Physical attacks against scale or printer hardware

## Disclosure

After a fix is shipped we will publish a CHANGELOG entry under `### Security`
and, where appropriate, a GitHub Security Advisory describing the issue, the
affected versions, and the upgrade path.
