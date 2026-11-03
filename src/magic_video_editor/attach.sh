#!/bin/sh
# Attach this machine to Magic Studios.
#
# The studio shows you one command to paste. It installs the worker, attaches
# this machine to your account, and starts it. After this the machine renews its
# own credential -- there is nothing to keep, copy, or refresh by hand.
#
#   curl -fsSL https://magicstudios.ai/studio/attach.sh | sh -s -- <code>
#
# Everything it writes lives under ~/.magic-video-worker and ~/.config; it
# installs nothing system-wide and needs no root.

set -eu

CODE="${1:-}"
STUDIO_URL="${MVS_STUDIO_URL:-https://magicstudios.ai}"
HOME_DIR="${HOME:?HOME must be set}"
ROOT="$HOME_DIR/.magic-video-worker"
CONFIG="$HOME_DIR/.config/magic-video-studio/worker.env"

if [ -z "$CODE" ]; then
    echo "Usage: attach.sh <code from magicstudios.ai/account/machines>" >&2
    exit 2
fi

say() { printf '  %s\n' "$1"; }

# `set -e` plus a silenced command exits with no explanation at all, which is
# exactly how the first run of this script failed: it printed "Preparing the
# worker…" and stopped. Every step that can fail says so.
fail() {
    printf '\nAttaching failed: %s\n' "$1" >&2
    printf 'Setup stopped at this step. See the details above before retrying.\n\n' >&2
    exit 1
}

printf '\nAttaching this machine to Magic Studios.\n\n'

# uv brings its own Python, so a machine on an older one needs nothing upgraded.
if ! command -v uv >/dev/null 2>&1; then
    if [ -x "$HOME_DIR/.local/bin/uv" ]; then
        PATH="$HOME_DIR/.local/bin:$PATH"
        export PATH
    else
        say "Installing uv (a Python installer; nothing system-wide)…"
        curl -fsSL https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
            || fail "could not install uv (see https://astral.sh/uv)"
        PATH="$HOME_DIR/.local/bin:$PATH"
        export PATH
    fi
fi

say "Preparing the worker…"
# --allow-existing so re-attaching a machine, or retrying after a failure, works
# rather than stopping on the environment left by the previous attempt.
uv venv --allow-existing --python 3.13 "$ROOT/venv" \
    || fail "could not create a Python 3.13 environment in $ROOT/venv"
# The shared installer downloads an authorized wheel and installs [agent,comfy].
# No Git installation or GitHub credentials are needed on this machine.
curl -fsSL "$STUDIO_URL/studio/worker-install.py" -o "$ROOT/worker-install.py" \
    || fail "could not download the installer from $STUDIO_URL"
MVS_CODE="$CODE" "$ROOT/venv/bin/python" "$ROOT/worker-install.py" "$STUDIO_URL" "$ROOT" \
    || exit 1
# Import the entry point rather than trusting the install: a missing extra is
# invisible until systemd starts it, and by then the script has said "Done".
"$ROOT/venv/bin/python" -c "import outbound_gpu_worker_pool.agent_main" \
    || fail "the worker installed but cannot start (missing dependency)"
# The runner is served rather than installed: it is the same file the studio's
# own tests exercise, and it needs only the standard library plus the agent.
curl -fsSL "$STUDIO_URL/studio/cpu-worker.py" -o "$ROOT/cpu_worker.py" || fail "could not download CPU worker"
curl -fsSL "$STUDIO_URL/studio/worker.py" -o "$ROOT/worker.py" \
    || fail "could not download the worker from $STUDIO_URL"
# It is served by the app rather than the CDN precisely so this is a script and
# not the studio's HTML; check, because that failure is silent otherwise.
head -1 "$ROOT/worker.py" | grep -q '"""' \
    || fail "$STUDIO_URL/studio/worker.py did not return the worker"

curl -fsSL "$STUDIO_URL/studio/worker-runtime.py" -o "$ROOT/worker_runtime.py" \
    || fail "could not download runtime setup"

# Check an explicit profile before spending the one-use pairing code.
"$ROOT/venv/bin/python" "$ROOT/worker_runtime.py" > "$ROOT/profile-settings.json" \
    || fail "the configured ComfyUI profile is not ready; pairing code was not spent"

say "Attaching…"
# The code is spent here, machine to server. It never reaches a file, and the
# credential that comes back never passes through a human.
REDEEMED=$("$ROOT/venv/bin/python" - "$STUDIO_URL" "$CODE" <<'PY'
import json, sys, urllib.error, urllib.request

studio_url, code = sys.argv[1].rstrip("/"), sys.argv[2]
request = urllib.request.Request(
    f"{studio_url}/studio/workers/redeem",
    data=json.dumps({"token": code, "label": None}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        print(response.read().decode())
except urllib.error.HTTPError as error:
    sys.stderr.write(
        f"The studio refused this code ({error.code}). Codes work once and last\n"
        "an hour -- ask for another on the machines page.\n"
    )
    raise SystemExit(1)
except urllib.error.URLError as error:
    sys.stderr.write(f"Could not reach {studio_url}: {error.reason}\n")
    raise SystemExit(1)
PY
)

mkdir -p "$(dirname "$CONFIG")"
umask 077
"$ROOT/venv/bin/python" - "$CONFIG" "$STUDIO_URL" <<PY
import json, os, stat, sys
from pathlib import Path
config_path, studio_url = sys.argv[1], sys.argv[2].rstrip("/")
redeemed = json.loads('''$REDEEMED''')
lines = [
    "# Written by attach.sh. The credential below renews itself.",
    f"OGWP_WORKER_COORDINATOR_URL={studio_url}/pool",
    f"OGWP_WORKER_ID={redeemed['worker_id']}",
    f"OGWP_WORKER_TOKEN={redeemed['credential']}",
    f"OGWP_WORKER_TENANT={redeemed['tenant_id']}",
    "OGWP_WORKER_AUTH=static",
    f"MVS_STUDIO_URL={studio_url}",
    "",
]
prepared = json.loads((Path("$ROOT") / "profile-settings.json").read_text())
for key, value in prepared.items():
    if "\n" in value or "\r" in value:
        raise ValueError("invalid worker configuration")
    lines.insert(-1, f"{key}={value}")
if redeemed.get("credential_expires_at"):
    lines.insert(-1, f"MVS_CREDENTIAL_EXPIRES_AT={redeemed['credential_expires_at']}")
descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
with os.fdopen(descriptor, "w") as handle:
    handle.write("\n".join(lines))
print(redeemed["label"])
PY

if command -v systemctl >/dev/null 2>&1 && [ -d "$HOME_DIR/.config" ]; then
    say "Installing a service so it survives reboots…"
    mkdir -p "$HOME_DIR/.config/systemd/user"
    cat > "$HOME_DIR/.config/systemd/user/magic-video-worker.service" <<UNIT
[Unit]
Description=Magic Video Studio worker
After=network-online.target

[Service]
EnvironmentFile=$CONFIG
ExecStart=$ROOT/venv/bin/python $ROOT/worker.py
Restart=always
RestartSec=10
TimeoutStopSec=1500

[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    if systemctl --user enable --now magic-video-worker >/dev/null 2>&1; then
        # `enable --now` returns before the process has proved it can run, so a
        # worker that starts and immediately dies still looked like success.
        sleep 3
        if systemctl --user is-active --quiet magic-video-worker; then
            printf '\nDone. This machine is attached and running.\n'
            printf 'It appears on your machines page, and renews itself from here on.\n'
            printf 'WSL: Windows must start this distro (for example through a sign-in Startup entry).\n'
            printf 'ComfyUI restart recovery requires OGWP_COMFY_START_COMMAND; see the setup guide.\n\n'
            exit 0
        fi
        printf '\nThe machine is attached, but the worker did not stay running.\n' >&2
        printf 'Its last words:\n\n' >&2
        journalctl --user -u magic-video-worker --no-pager -n 5 2>/dev/null >&2 || true
        printf '\n' >&2
        exit 1
    fi
    say "Could not start the service automatically."
fi

printf '\nDone. This machine is attached. Start it with:\n\n'
printf '  set -a; . %s; set +a\n' "$CONFIG"
printf '  %s/venv/bin/python $ROOT/worker.py\n\n' "$ROOT"
