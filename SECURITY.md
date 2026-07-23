# Security Policy

## Supported Version

QueryForge is currently an alpha project. Security fixes target the latest `0.1.x`
development line.

## Reporting a Vulnerability

Do not disclose suspected vulnerabilities in a public issue. Use GitHub Private
Vulnerability Reporting when it is enabled for the repository. If it is unavailable,
contact the repository owner through a private channel listed on their GitHub profile.

Include the affected entry point, a minimal reproduction, expected impact, and any
suggested mitigation. Do not include real credentials, private datasets, or personal
information.

## Security Boundary

QueryForge provides read-only SQLite execution, AST-level SQL policy enforcement,
semantic visibility controls, and bounded workflow behavior. It does not currently
provide production authentication, tenant isolation, public-network hardening, or
rate limiting. REST and MCP interfaces must remain in a controlled environment until
those controls are added.
