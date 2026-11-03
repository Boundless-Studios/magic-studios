# Attach your own machine

Your generations normally run on the shared pool. If you have a machine with a
suitable GPU, you can attach it and your work will run there instead. A machine
you attach only ever receives *your* work — never anyone else's, and never the
shared queue's.

What the agent can and cannot reach on your machine is answered separately, in
[the worker FAQ](worker-faq.md). Read that first if you are weighing whether to
do this at all.

## What you need

- A provisioned ComfyUI with the models/nodes for an explicit machine profile.
- H3 motion supports the verified W4A8/LightX2V **16GB** profile or the existing
  standard **24GB** profile; VRAM alone does not select a recipe.
- The attach script installs the lightweight Python worker; ComfyUI can run as
  Windows portable, a normal Windows installation, or Linux/WSL.

See [hosted machine setup](hosted-machine-setup.md) for exact recipes, this desktop's
WSL-to-Windows setup, preflight/binding behavior and post-deployment acceptance.
Local Mac access over SSH does not require hosted enrollment.

## Attaching it

**1. In the studio**, open the account menu, choose *Your machines*, then *Add a
machine*. Select its explicit configuration and local ComfyUI URL, then copy the
command. Keep the existing/CPU choice for machines without a configured H3 profile.

**2. Paste that command on the machine.** It looks like this:

```
curl -fsSL https://magicstudios.ai/studio/attach.sh | sh -s -- mvsp_xxxxxxxx
```

It installs the worker under `~/.magic-video-worker`, attaches the machine to
your account, and starts it as a service that survives reboots. Nothing is
installed system-wide and it does not need root.

That is the whole of it. **There is no credential for you to keep.** The machine
receives one directly from the studio — it never passes through your clipboard or
your terminal — and renews it on its own from then on. The command works once and
expires in an hour; if it lapses, ask for another.

The machine appears under *Your machines* as soon as it asks for work.

## Renewal, and why it matters

A machine's credential lasts thirty days and the worker replaces it about a week
before it lapses. You never see this happen.

It is not only tidiness. A credential that never expires stays useful forever
once it leaks; a rotating one is useful only briefly. And because only the
*current* credential may renew itself, a stolen copy cannot take the machine's
place: the moment the real machine renews, the copy is refused. The old
credential keeps working for a day afterwards, so an interrupted renewal or a
restart at the wrong moment never locks a machine out of its own account.

If a machine is off for more than a month it will fall outside its window. Remove
it in the studio and attach it again — the same one command.

## If the code does not work

Codes are single-use and last an hour, and a code that has already been redeemed
is refused in exactly the same way as one that never existed — deliberately, so
that guessing a code tells an attacker nothing. If redemption fails, ask for
another code rather than retrying the same one.

## Removing a machine

*Remove* in the studio revokes the credential immediately: the machine stops
being given new work at once. A job it was already running is not lost — it
returns to the queue once its lease expires, and is picked up by another machine
that meets the job's requirements. Jobs pinned to that exact worker require
cancellation and a new resolution to move to another machine. Removing also frees a slot; you can have up to
twenty machines attached at a time.

To move a machine to a different person, remove it and let them attach it with
their own code. Ownership is fixed when a machine first registers and cannot be
reassigned afterwards, which is what stops a machine being quietly pointed at
someone else's work.

## Running it as a service

Once it works by hand, keep it running across reboots with a user unit:

```ini
# ~/.config/systemd/user/magic-video-worker.service
[Unit]
Description=Magic Video Studio worker
After=network-online.target

[Service]
EnvironmentFile=%h/.config/magic-video-studio/worker.env
ExecStart=/usr/bin/python3 -m outbound_gpu_worker_pool.agent_main
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

```
systemctl --user enable --now magic-video-worker
loginctl enable-linger "$USER"   # so it survives logout
```
