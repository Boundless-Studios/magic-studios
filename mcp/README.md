# Magic Studios MCP server

An MCP (Model Context Protocol) server for [Magic Studios](https://magicstudios.ai). Magic Studios is built for agents: everything in the studio is an API call, and this server turns those calls into tools. With it, an agent can:

- **Animate a character:** turn one image into game sprites, inspect frames, save loops and export PNG frames with a JSON manifest.
- **Make a movie:** write shots, generate takes, cut them together with music and release an MP4.
- **Fly through a scene:** move a camera between location images and chain the moves into one shot.

It runs on your computer and calls the hosted API at `https://api.magicstudios.ai` with your API key. There is no hosted MCP endpoint.

## Install

You need Python 3.13 or later. [uv](https://docs.astral.sh/uv/) can install a suitable Python for you.

```bash
uv tool install https://magicstudios.ai/studio/downloads/magic_studios_mcp-0.2.2-py3-none-any.whl
```

Or with pipx:

```bash
pipx install https://magicstudios.ai/studio/downloads/magic_studios_mcp-0.2.2-py3-none-any.whl
```

Either one installs the `magic-studios-mcp` command. `https://magicstudios.ai/studio/downloads` lists each file with its SHA-256.

## Get an API key

Sign in to Magic Studios and open account menu → **API keys**. Create a key; it starts with `mvs_` and is shown only once. Keep it out of files you share or commit.

## Connect your agent

Each client starts `magic-studios-mcp` and passes your key in `MAGIC_STUDIOS_API_KEY`.

**Claude Code**

```bash
claude mcp add magic-studios --env MAGIC_STUDIOS_API_KEY=mvs_your_key -- magic-studios-mcp
```

**Claude Desktop.** Add this to `claude_desktop_config.json` and restart the app:

```json
{
  "mcpServers": {
    "magic-studios": {
      "command": "magic-studios-mcp",
      "env": { "MAGIC_STUDIOS_API_KEY": "mvs_your_key" }
    }
  }
}
```

If the app can't find the command, use its full path. `which magic-studios-mcp` prints it on macOS and Linux, and `where magic-studios-mcp` on Windows.

**Codex**

```bash
codex mcp add magic-studios --env MAGIC_STUDIOS_API_KEY=mvs_your_key -- magic-studios-mcp
```

Start a new session afterwards so the agent loads the server.

## Add the skill

The Magic Studios skill tells an agent how to use all three step by step: animate a character (generate, find a loop, match body scale, export), make a movie (shots, takes, build, cut, release) and fly through a scene (images, legs, takes, chain). Download [magic-studios-skill-0.2.2.zip](https://magicstudios.ai/studio/downloads/magic-studios-skill-0.2.2.zip). It contains one folder, `magic-studios`.

- **Claude Code:** unzip it into `~/.claude/skills/`.
- **Claude Desktop:** upload the zip under Settings → Capabilities → Skills.
- **Codex:** unzip it into `~/.agents/skills/`.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `MAGIC_STUDIOS_API_KEY` | none | Your `mvs_…` key. Required. |
| `MAGIC_STUDIOS_URL` | `https://api.magicstudios.ai` | The API to call. |
| `MAGIC_STUDIOS_MCP_TRANSPORT` | `stdio` | `stdio`, or `streamable-http` for a client that connects over HTTP. |
| `MAGIC_STUDIOS_MCP_HOST`, `MAGIC_STUDIOS_MCP_PORT` | `127.0.0.1`, `8766` | Where the HTTP transport listens. It has no authentication of its own, so keep it on `127.0.0.1`. |

## What it can do

The server has about 60 tools. Ask your agent to list them; their descriptions are the reference. In outline:

- **Projects and characters:** `create_project`, `upload_subject_base64`, `upload_reference`, `generate_subject`, `approve_subject`.
- **Character animations:** `get_generation_config`, `add_recommended_motions`, `generate_motions`, `wait_for_operations`, `list_takes`, `select_take`.
- **Frames:** `inspect_candidate_frames` shows decoded frames as images. `suggest_motion_loop`, `calibrate_motion_body`, `derive_candidate_alpha` and `create_frame_revision` find and save a loop. `get_candidate_download` returns a sheet, WebM or GIF.
- **Releases:** `run_quality`, `approve_set`, `create_release`, `export_release`, `download_export`.
- **Several directions:** `get_setup_capabilities`, `get_setup`, `preview_setup`, `apply_setup`.
- **Movies:** `create_movie`, `set_movie_cast`, `attach_movie_voice`, `set_movie_story`, `upload_movie_asset`, `set_shot_opening`, `build_movie`, `generate_takes`, `select_movie_take`, `wait_for_movie`, `set_movie_cut`, `set_movie_soundtracks`, `assemble_movie`, `release_movie`, `download_movie_release`.
- **Scene flights:** `create_scene_flight`, `add_scene_flight_image`, `set_scene_flight_legs`, `generate_scene_flight_take`, `generate_scene_flight_leg` (one leg in one call), `select_scene_flight_take`, `wait_for_scene_flight`, `chain_scene_flight`, `download_scene_flight_video`.

Generation runs on a machine you attached or on your own Higgsfield key, so set one up under **Your machines** first. Some things need the browser: creating or revoking keys, attaching machines, saving a Higgsfield key and deleting your account.

The [API documentation](https://magicstudios.ai/studio/api.md) and [API reference](https://magicstudios.ai/studio/reference) describe the routes the tools call.

## Licence

MIT. Copyright © 2026 Boundless Games, LLC. See [LICENSE](LICENSE).
