# Mac connection over SSH

The Mac needs an MCP-capable agent with Streamable HTTP support. The GPU, ComfyUI, models, API and MCP server run on the desktop. No Mac GPU/model installation is needed.

The desktop UI and MCP bind **127.0.0.1 only**. Use your existing desktop SSH alias; the SSH endpoint may be Windows or WSL, but its remote loopback must reach both ports. Windows-to-WSL loopback was verified here. Mac SSH forwarding itself must still be tested from the Mac.

The owner’s `wsl` alias uses a private home-network address. A travel hotspot cannot reach it without an already configured route to that network. If SSH times out while travelling, retain project/job IDs and resume retrieval when home connectivity returns; do not submit replacement renders merely because the connection is unavailable.

Reuse the Mac's existing SSH alias and identity. If a login service already owns the forwards, use it instead of starting a second tunnel. On the owner's Mac, `wsl` is the verified alias and `~/Library/LaunchAgents/com.magic-studios.ssh-tunnel.plist` maintains both forwards; confirm those local files exist before relying on them.

Otherwise, keep this running in a Mac terminal (replace `desktop` with your working SSH alias):

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:5173:127.0.0.1:5173 \
  -L 127.0.0.1:8766:127.0.0.1:8766 desktop
```

Then open `http://127.0.0.1:5173` in the **Mac** browser. Configure the agent's MCP server:

- Name: `magic-studios`
- Transport: Streamable HTTP (not SSE or stdio)
- URL: `http://127.0.0.1:8766/mcp`
- MCP authentication: none for this single-user local service; SSH authenticates access.

For Codex CLI:

```bash
codex mcp add magic-studios --url http://127.0.0.1:8766/mcp
codex mcp list
```

Start a new agent session if it has not loaded the connection. The URL is client-local and requires the tunnel. Do not configure a cloud-only client to use the Mac's loopback: its network context is different.

## Load the skill

When working in this repository, the skill is in `.agents/skills/magic-studios`. To use it in another project on the Mac, copy the folder into `~/.agents/skills/magic-studios` (do not overwrite an unrelated existing skill). Over the same SSH alias, replacing `REPO` with the desktop checkout's path:

```bash
mkdir -p ~/.agents/skills
ssh desktop 'tar -C REPO/.agents/skills -cf - magic-studios' | tar -C ~/.agents/skills -xf -
```

The folder contains `SKILL.md`, its references and metadata, not executable install hooks. Other agents can read `SKILL.md` explicitly if their skill discovery convention differs.

## First Mac test prompt

> Use $magic-studios with my configured magic-studios MCP connection. This is a read-only connection test: call get_generation_config and confirm this desktop reports B, the 8-step LightX2V 768p default. Read project 661080b4-0d17-4933-b9b3-13c0834099ab and list its walk and attack takes. Inspect a few native frames from the selected walk, then obtain and download its PNG to my Mac. Report its frame geometry, pivot, timing and the downloaded file path. Do not generate, approve, select, edit or replace anything.

Existing selected Walk ID: `e263f3f204b46ede1400cfe2402f8889`; SHA-256 `c1a3063c56c465ee09e49a0dd2e3719fbabe243bb2c150f634e89ae022803708`. IDs are examples for this desktop, not portable defaults for other installations. PNG export bytes can contain different export metadata from the stored source artifact; do not assume their complete file digests must match. Compare expected download digests when supplied, and retain source provenance separately.

After this succeeds, ask the agent to create a **new test project**, upload your Mac image via base64, approve the prepared subject as part of the authorized task, generate only `walk` using the server default, extract one 12-frame stride, inspect all phases, save and download it. Keep the established project as a reference. The server's `add_recommended_motions` adds six required motions: for a walk-only test, download the candidate directly rather than approving/exporting an incomplete set.

## Troubleshooting

- `curl -f http://127.0.0.1:5173/studio/generation-config` should return JSON. A browser shell/HTML response is not a successful API test.
- The MCP endpoint requires a protocol handshake. A plain browser GET/405 is not proof of failure; discover tools from the agent or use `scripts/mcp_smoke.py` in the repository.
- If forwarding fails, check the SSH alias and remote `curl`/PowerShell requests to loopback first. Keep the services on loopback; no LAN firewall opening is required.
- Both tunnel ports must remain open: MCP calls can work while download links fail if only 8766 is forwarded.
- Generated download URLs use `MAGIC_STUDIOS_PUBLIC_URL=http://127.0.0.1:5173`. If Mac ports are occupied, forward different local ports and update the MCP server's public URL for that session so returned links resolve through the asset tunnel. Do not silently substitute a LAN address.
- Desktop start: run `local/start.sh` in WSL. MCP install/recreate: `local/install-mcp-service.sh`. Network-only loopback enforcement: `local/secure-local-access.sh`. A preset switch requires an idle GPU queue; do not restart the API during active jobs.
- Production account API keys and hosted worker pairing tokens are separate from this SSH-local connection. Enrollment is not implied by local availability.

Codex client conventions checked against the [official MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) and [skill documentation](https://learn.chatgpt.com/docs/build-skills). `codex mcp add --help` was also verified locally.
