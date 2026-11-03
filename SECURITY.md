# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open this repository's
**Security** tab and choose **Report a vulnerability**. Please do not open a
public issue, pull request or discussion for a security problem.

Include what you found, how to reproduce it, and what an attacker could do with
it. If the issue affects the hosted service at magicstudios.ai, say so. We aim
to acknowledge a report within a week and will keep you updated until it is
fixed. Tell us if you would like to be credited.

Test only against your own self-hosted Studio, your own accounts and your own
machines. Do not access other people's projects, run load or denial-of-service
tests, or try to reach the hosted service's infrastructure.

## Supported versions

Only the latest commit on `main` is supported. There are no maintained release
branches.

## What we treat as security issues

These are the boundaries Studio relies on. A way around any of them is a
vulnerability:

- **Verified actor identity.** Every Studio, pool and MCP route acts for a
  signed-in actor, an API key or an enrolled machine credential. A request must
  never act as someone else or reach another owner's projects, files, jobs,
  keys or machines.
- **Tenant isolation for workers.** A machine someone enrolled runs only its
  owner's work. Discovery, leasing and job results must never cross tenants.
- **Fenced job completion and provider attestation.** Only the worker holding
  a job's current lease can complete it, and it must attest the recipe and
  inputs it actually ran.
- **Immutable publication.** A published release, export or revision never
  changes after the fact.
- **Secrets.** Provider keys, API keys, pairing codes and worker credentials
  are never returned, logged or written to project files.

Bugs in third-party models, ComfyUI or other upstream programs belong with
their own projects (see `NOTICE`), unless Studio's use of them creates the
problem.
