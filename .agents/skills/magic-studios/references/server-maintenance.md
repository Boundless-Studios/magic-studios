# Authorized server fixes

A Mac checkout can contain the same Magic Studios source as the desktop server. Confirm both checkouts and revisions through the existing SSH alias before transferring a fix; client and server filesystem paths differ. Use that alias and loopback tunnels, keeping services off the LAN.

Reproduce the fault at the public API/MCP boundary, inspect the relevant server implementation and logs, and make a focused fix with a regression test locally. Prefer validation errors at input boundaries to malformed manifests or client workarounds. Reuse the server prompt compiler, native decoding, loop analysis and normalization. Update this skill only when the supported caller workflow changes or an observed failure reveals missing guidance.

Before pulling or applying a tested change and restarting services, inspect both the persistent Studio operation ledger and the live ComfyUI queue. Wait for active jobs to settle; a failed queue probe does not establish idleness. Keep unrelated desktop changes and runtime state intact. Restart only affected services after the safe point, then repeat the actual MCP journey from the Mac. Interrupted jobs must be failed explicitly, never silently replayed.

Keep `.agents/skills/magic-studios` authoritative, rebuild the downloadable skill ZIP from its current files, and synchronize the installed Mac copy at `~/.agents/skills/magic-studios`. Verify archive entries match source. Server fixes do not automatically refresh an installed skill or an already-running MCP process.
