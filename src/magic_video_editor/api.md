# Magic Studios API

Magic Studios is built for agents: everything in the studio is an API call. The web
app uses the same routes, so a script or an AI agent can do what you do with your work
in the browser. A few account settings need a browser; they are listed below. We host
and run the API. Generation runs on a machine you attach or on your own Higgsfield key.

The API does three things:

- **Animate a character.** One character image becomes sprite animations, exported as
  PNG frames with a JSON manifest. Main routes: `POST /studio/projects`,
  `POST …/subject/uploads`, `POST …/motions/generate-batch`,
  `POST …/motions/{motion_id}/candidates/{candidate_id}/select`,
  `POST /studio/projects/{project_id}/releases` and
  `POST /studio/releases/{release_id}/exports`. See [Animate a character](#animate-a-character).
- **Make a movie.** Shots generated from starting images are cut together, with music
  and narration, into one MP4. Main routes: `POST /studio/movies`, `PUT …/story`,
  `POST …/shots/{sid}/opening`, `POST …/build`, `POST …/assemble` and
  `POST …/release`. See [Make a movie](#make-a-movie).
- **Fly through a scene.** Camera moves run between location images and chain into one
  continuous shot. Main routes: `POST /studio/scene-flights`, `POST …/images`,
  `PUT …/legs`, `POST …/legs/{leg_id}/takes` and `POST …/chain`. See
  [Fly through a scene](#fly-through-a-scene).

The [guide](https://magicstudios.ai/guide#agents) covers the studio itself and how to
connect an agent.

## Start here

**Base URL.** `https://api.magicstudios.ai` for scripts and agents. It is the same
service as `https://magicstudios.ai`. Every route starts with `/studio`.

**Authentication.** Create a key in the web app: account menu → **API keys**. It is
shown once. Send it on every request:

```
Authorization: Bearer mvs_…
```

A key acts as you and nothing more. It sees only your projects.

**What needs a browser.** A key cannot do these. Use the web app:

- create, list or revoke API keys;
- delete your account;
- attach, list, rename or remove machines;
- save or remove a Higgsfield key;
- dismiss past access requests (admins, on magicstudios.ai).

Everything else works with a key, including reading your machines' settings
(`GET /studio/generation-config`) and whether a Higgsfield key is saved
(`GET /studio/providers`).

**Limits.** The same as in the browser. `GET /studio/auth/session` reports your
storage allowance and how many jobs may run at once under `usage`. Going over refuses
new work with `507` (storage) or `429` (jobs). Every prompt is screened before
anything runs. Work nobody opens for 90 days is deleted unless it has a release.

**A minimal run.** Upload a character image, approve it, generate a walk and download
the first take's sprite sheet. Python with `requests`:

```python
import time

import requests

HOST = "https://api.magicstudios.ai"
API = HOST + "/studio"
s = requests.Session()
s.headers["Authorization"] = "Bearer mvs_…"  # account menu → API keys

def call(method, path, **body):
    r = s.request(method, API + path, json=body or None)
    r.raise_for_status()
    return r.json()

print(call("GET", "/auth/session")["usage"])  # your limits

# A project, with your finished character image as its subject.
p = call("POST", "/projects", name="Ember Knight")
pid, rev = p["project_id"], p["revision"]
up = call("POST", f"/projects/{pid}/subject/uploads", content_type="image/png", expected_revision=rev)
with open("knight.png", "rb") as f:
    requests.put(up["upload_url"], data=f, headers={"Content-Type": "image/png", **up["upload_headers"]}).raise_for_status()
p = call("POST", f"/projects/{pid}/subject/settle", expected_revision=rev, asset_key=up["key"])
p = call("POST", f"/projects/{pid}/subject/approve", expected_revision=p["revision"])

# Add a walk and generate it with your machine's default settings.
p = call("POST", f"/projects/{pid}/motions/batch", expected_revision=p["revision"],
         motions=[{"motion_id": "walk", "name": "Walk", "kind": "preset", "preset": "walk"}])
batch = call("POST", f"/projects/{pid}/motions/generate-batch", expected_revision=p["revision"],
             request_id="walk-1", motion_ids=["walk"])
if batch["failed"]:  # a refused prompt, no machine, or the job limit
    raise SystemExit(batch["failed"])

# Wait for the job to settle into the project, then download its take.
job = batch["queued"][0]["operation"]["job_id"]
deadline = time.monotonic() + 30 * 60
while True:
    call("POST", f"/projects/{pid}/activity/reconcile")  # moves finished work forward
    op = next((o for o in call("GET", f"/projects/{pid}/operations") if o["job_id"] == job), None)
    if op is None:  # settled: the take is on the project
        break
    if op["stage"] in ("failed", "cancelled"):
        raise SystemExit(op["failure_explanation"] or op["stage"])
    if op["settlement_blocked"]:
        raise SystemExit(op["settlement_blocked"])
    if time.monotonic() > deadline:
        raise SystemExit("the walk is still running after 30 minutes")
    time.sleep(15)
walk = next(m for m in call("GET", f"/projects/{pid}")["manifest"]["motions"] if m["motion_id"] == "walk")
take = walk["candidates"][0]["candidate_id"]
sheet = call("GET", f"/projects/{pid}/motions/walk/candidates/{take}/sheet.png")
png = s.get(HOST + sheet["upload_url"])
png.raise_for_status()
with open("walk.png", "wb") as f:
    f.write(png.content)
```

Writes that change an existing project carry `expected_revision`, the revision you
last saw; a stale one is `409` (see [Concepts](#concepts)). Most of them return the
project at its new revision. Creating a project, upload targets, generation and
reconcile return their own shapes, as the example shows. To run on Higgsfield instead of a machine, pass `settings` from
`providers.higgsfield.settings` in `GET /studio/generation-config`.
[Animate a character](#animate-a-character) below goes on to quality checks, a
release and an export.

**Where MCP fits.** The [MCP server](#mcp-server) wraps this API as tools for an AI
agent, so the agent does not write HTTP. It runs on your computer and calls this API
with your key. There is no hosted MCP endpoint.

**Reference.** Every route, with its fields, responses and errors:
[`/studio/reference`](https://magicstudios.ai/studio/reference). Try requests with
your key at [`/studio/docs`](https://magicstudios.ai/studio/docs). The OpenAPI schema
is at `/studio/openapi.json`, for client generators. This document explains the
concepts; the reference is authoritative for field-level detail.

---

## Concepts

**Project.** One subject (a character, a creature, an object) plus the set of
motions animated from it, plus the releases cut from that set. Projects are owned
by the actor who created them; nobody else can read or write one, and a request
for a stranger's project answers `404`, never `403` — existence is not leaked.

**Revision and `expected_revision`.** A project is an append-only chain of manifest
revisions. Every mutating request carries `expected_revision`, the revision the
caller believes is current. If it is stale the write is refused with `409` and the
body tells you the real number:

```json
{"detail": {"message": "expected revision is stale", "current_revision": 5}}
```

The fix is always the same: re-read the project (`GET /studio/projects/{project_id}`),
take its `revision`, and retry. Every successful mutation returns the new project
state, so a client that threads the returned `revision` into its next call rarely
sees a `409` at all. The MCP server in `mcp/` does exactly this, and retries once on
`409` automatically.

**Operations vs the activity ledger vs the manifest.** These are three different
views and confusing them is the usual source of "my generation vanished":

- **Operations** (`GET /studio/projects/{id}/operations`) are what is *in flight
  right now*, plus finished work not yet settled and up to 10 recent failed or
  cancelled operations. Read each one's `stage`: `queued`, `generating` and
  `awaiting_settlement` are still running; `failed` and `cancelled` are history.
- **The activity ledger** (`GET /studio/projects/{id}/activity`) is the paged
  history of operations, terminal ones included, newest first. It is how you find
  out that a job failed and why.
- **The manifest** (inside every `StudioProjectResponse`) is the *durable state*:
  the subject, its revisions and approval, the motions and their candidates. A
  finished operation does not change the manifest until it is **settled**.

`POST /studio/projects/{id}/activity/reconcile` is the bridge: it settles every
finished-but-unsettled operation into the manifest and returns the refreshed
activity page. A client that polls should reconcile before deciding it is done.

**Findings are warnings, never blockers.** Subject assessments and set quality
assessments produce `findings` with a `severity` of `warning` or `info`. They never
prevent an approval. Approving records the findings that were visible at the time
as acknowledged (`waived_finding_ids`, `waiver_reason`), so the audit trail shows
what the approver saw. Send your own narrower `waived_finding_ids` (the finding
`code` values) only if you want to acknowledge a subset explicitly.

**Assets are keys, not bytes.** The API never accepts a file body. You ask for an
upload target, `PUT` the bytes to the returned `upload_url`, and then hand the
returned `key` back to the API.

---

## Authentication

There are two ways to be a studio actor, and they resolve to the *same* actor id,
so every route behaves identically either way.

### Browsers: sign in through Auth0

The studio delegates identity to an **Auth0** tenant and keeps the session itself. A
browser signs in with OAuth Authorization Code + PKCE, and the round trip ends with a
server-side session in an httpOnly `mvs_session` cookie (Secure, `SameSite=Lax`,
30 days, renewed as it is used). No header a browser can set is trusted for identity.

* `GET /studio/auth/login?return_to=/…` → `302` to
  `https://{AUTH0_DOMAIN}/authorize` with `response_type=code`, the client id, the
  redirect uri, `scope=openid profile email`, `state`, an S256 `code_challenge` and
  `prompt=select_account`; with `signup=true` it sends `screen_hint=signup` instead, so
  the hosted page opens on its sign-up form. The state, the PKCE verifier and the return path travel in a
  10-minute httpOnly cookie, never in the URL.
* `GET /studio/auth/callback?code&state` verifies the state, exchanges the code at
  `https://{AUTH0_DOMAIN}/oauth/token`, verifies the id token against the tenant's JWKS
  (issuer, audience, `exp`/`iat`), requires `email_verified`, applies the allowlist,
  upserts the user, creates the session and `303`s to the stored return path or `/`.
  Any failure `303`s to `/login?error=auth0`; an address that is not on the allowlist
  `303`s to `/login?error=not_allowed`, and one whose email is not verified yet (a new
  password account) `303`s to `/login?error=verify_email`.

**Google is the only connection at launch.** Auth0's passwordless email connection
needs a custom SMTP provider on the tenant, which is not set up yet, so no
email/passwordless door is enabled.

`GET /studio/auth/session` answers
`{actor_id, email, display_name, sign_in_method, usage, admin}` or `401`; `admin` is
true for a verified studio admin. `usage` carries the
signed-in operator's own
`{stored_bytes, storage_limit_bytes, running_jobs, concurrent_job_limit, account_tier}`;
`0` on a limit means unlimited, and `account_tier` is `invited` or `public`. `POST /studio/auth/sign-out` revokes the session row, clears the cookie, and
answers `{"logout_url": "https://{AUTH0_DOMAIN}/v2/logout?client_id=…&returnTo=…/login"}`
so the web app can end the Auth0 session too.

The shared machines go only to the explicit allowlist and the operators
(`MAGIC_VIDEO_EDITOR_ADMIN_EMAILS`). On magicstudios.ai, the old access-request and
invitation routes are closed; its API reference documents them. Every session and API
key is rechecked against the allowlist when it is used (`401` once access is gone), so
taking an address off the list also ends a sign-in or stops a key minted while it was
being removed.

Only allowlisted addresses may sign in (`MAGIC_VIDEO_EDITOR_ALLOWED_EMAILS`,
`MAGIC_VIDEO_EDITOR_ALLOWED_EMAIL_DOMAINS`). With both unset, nobody can.
`MAGIC_VIDEO_EDITOR_PUBLIC_SIGNUP_ENABLED=true` (default `false`) also admits any
verified email. Such an account is *public*; allowlisted addresses and operators
stay *invited*, re-evaluated on every request. Public accounts never use house
compute or the house director: motion, subject, composition and movie work needs
their own enrolled machine (or, for motions, a Higgsfield key), MiniMax H3 work
(every GPU motion recipe, movie takes and auditions) also needs
`MAGIC_VIDEO_EDITOR_PUBLIC_H3_ENABLED=true`, and storage is capped by
`MAGIC_VIDEO_EDITOR_PUBLIC_STORAGE_LIMIT_BYTES` (default 5 GiB). Refusals answer `403`
with `detail.code` `house_compute_unavailable`, `minimax_h3_unavailable` or
`director_unavailable`; `POST /studio/movies/{id}/plan` is refused with the last,
and clip import, voice-clip upload and voice auditions with the first, since those
steps run FFmpeg on the service. `POST /pool/jobs` (untenanted house work) is refused
for public accounts too, and every pool job route requires a signed-in caller.

**Actor ids.** An Auth0 subject of the form `google-oauth2|<id>` becomes
`iap:accounts.google.com:<id>` — byte-for-byte what IAP produced, so projects created
under IAP keep their owner. Any other subject is `email:<lowercased address>`; the
first time that person signs in through Google, their projects move to the Google actor
id once and stay there.

**CSRF.** A non-`GET` request authenticated *by cookie* must carry an `Origin` (or
`Referer`) whose host matches the request host, otherwise it is `403`
(`csrf_origin_mismatch`). API-key requests are exempt — a bearer token is not sent
automatically by a browser.

While the migration off IAP is in flight, a verified IAP assertion is still accepted:
it auto-provisions the user from the email claim and hands back a session cookie, so
nobody is signed out by the switch.

### Clients: API keys

An API key is a bearer credential that acts *as its owner and only as its owner*.
It carries no extra scope: anything the owner can do with the web app the key can
do, and nothing the owner cannot.

Send it as a normal bearer token:

```
Authorization: Bearer mvs_EXAMPLE_KEY_NOT_A_REAL_CREDENTIAL0
```

A key is `mvs_` followed by 32 url-safe characters. Only its SHA-256 is stored, so
the plaintext is shown exactly once, at creation, and cannot be recovered. Losing it
means minting a new one.

`last_used_at` is updated at most once a minute per key — it is an activity hint for
spotting an unused or leaked key, not an audit log.

#### Mint a key

Key management is **browser-authenticated only**. A key can never mint or revoke
keys, so revoking a leaked key is always sufficient.

```http
POST /studio/api-keys
Content-Type: application/json

{"name": "laptop cli"}
```

```json
{
  "key_id": "82fd1ede-e73e-49c9-a519-609f4e689d82",
  "actor_id": "iap:112233445566778899",
  "name": "laptop cli",
  "created_at": "2026-09-06T15:45:31.532865Z",
  "last_used_at": null,
  "revoked_at": null,
  "key": "mvs_EXAMPLE_KEY_NOT_A_REAL_CREDENTIAL0"
}
```

`key` appears in this response and nowhere else, ever again.

#### List keys

```http
GET /studio/api-keys
```

```json
[
  {
    "key_id": "82fd1ede-e73e-49c9-a519-609f4e689d82",
    "actor_id": "iap:112233445566778899",
    "name": "laptop cli",
    "created_at": "2026-09-06T15:45:31.532865Z",
    "last_used_at": "2026-09-06T16:02:11.900412Z",
    "revoked_at": null
  }
]
```

#### Revoke a key

```http
DELETE /studio/api-keys/82fd1ede-e73e-49c9-a519-609f4e689d82
```

Returns the key with `revoked_at` set. Revocation is immediate; the next request
using that key gets `401`. Revoking a key you do not own answers `404`.

#### Rejections

Every bad bearer credential is a `401` with a plain-text `detail`:

| Situation | `detail` |
|---|---|
| Not `mvs_` + 32 url-safe characters | `malformed API key` |
| Well-formed but not in the store | `unknown API key` |
| Revoked | `revoked API key` |

### Where API keys work

Because the application now authenticates users itself, there is no IAP edge in front
of it turning an API request into a sign-in redirect. **API keys work on the main host,
`https://magicstudios.ai`**, and on **`https://api.magicstudios.ai`**, a host alias to
the same backend that gives non-browser clients a stable hostname whose posture will not
change when the browser host's does. Both are the same service and the same actor ids.

The service is fail-closed either way: with no session cookie and no valid API key,
`studio_actor_id` answers `401` before any route runs, and the deployment canary probes
exactly that on the public host.

---

## Animate a character

The worked example below takes a project from nothing to a downloaded export. Every
request assumes the `Authorization: Bearer …` header; it is omitted for brevity.
Responses are trimmed to the fields that matter at each step — the full shape is in
the OpenAPI schema.

### 1. List projects

```http
GET /studio/projects
```

```json
[
  {
    "project_id": "6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d",
    "name": "Ember Knight",
    "revision": 1,
    "created_at": "2026-09-06T15:45:31.536757Z",
    "updated_at": "2026-09-06T15:45:31.536757Z",
    "created_by": "iap:112233445566778899",
    "subject_state": "none",
    "motion_count": 0,
    "release_count": 0,
    "external_subject": null,
    "activity": {"running": 0, "failed": 0, "completed": 0, "last_activity_at": null},
    "unreadable": false
  }
]
```

### 2. Create a project

```http
POST /studio/projects
Content-Type: application/json

{"name": "Ember Knight"}
```

```json
{
  "project_id": "6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d",
  "revision": 1,
  "manifest_digest": "3dd541d58b1825ea7ef08a57433f1e4dafa6c1520c416089ec4cb0caf02144d1",
  "updated_at": "2026-09-06T15:45:31.536757Z",
  "manifest": {
    "project_id": "6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d",
    "name": "Ember Knight",
    "owner_id": "iap:112233445566778899",
    "external_subject": null,
    "subject": {
      "current_revision": null,
      "revisions": [],
      "assessment": null,
      "approval": {"state": "not_ready", "waived_finding_ids": [], "waiver_reason": null}
    },
    "composition": {"current": null},
    "motions": [],
    "set_assessment": null,
    "set_approval": {"state": "not_ready"},
    "set_approval_fresh": false
  },
  "active_operations": [],
  "primary_action": "add_subject"
}
```

`primary_action` is the studio's own answer to "what next?" — one of `add_subject`,
`approve_subject`, `add_motion`, `generate_motion`, `select_candidates`, `assess_set`,
`approve_set`, `create_release`, `export`. A client can drive the whole flow off it.

`POST /studio/projects` also accepts `preset_motions` (a list of preset names, added
at creation) and `external_subject` (a `{system, entity_kind, entity_id, display_name}`
binding to a record in another system; see `GET /studio/targets` for what is installed).

### 3. Request a reference upload target and PUT the file

```http
POST /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/subject/references/uploads
Content-Type: application/json

{"content_type": "image/png", "expected_revision": 1}
```

```json
{
  "key": "sources/studio/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/subject-references/ef104c67ed804fac8019f0651ecc87d6.png",
  "upload_url": "https://storage.googleapis.com/…signed…"
}
```

`upload_url` is a short-lived signed URL. Send the bytes straight to it, with every
header in `upload_headers` exactly as given:

```bash
curl -X PUT --upload-file reference.png \
  -H 'Content-Type: image/png' \
  -H "x-goog-content-length-range: $RANGE" \
  "$UPLOAD_URL"
```

`upload_headers` is empty for invited accounts. For public accounts it carries a signed
`x-goog-content-length-range` ceiling (20 MB for images, 100 MB for motion clips; movie
uploads 20 MiB images, 256 MiB video, 50 MiB audio). Storage refuses a PUT that omits it
or exceeds it.

Nothing is recorded in the manifest by the upload itself — the `key` is what you
pass on. Use `.../subject/uploads` (same body, same response shape) instead when you
are supplying the finished subject image directly rather than a reference to
generate from; in that case settle it with
`POST /studio/projects/{id}/subject/settle` with `{"expected_revision": n, "asset_key": key}`.

### 4. Generate the subject

```http
POST /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/subject/generate
Content-Type: application/json

{
  "expected_revision": 1,
  "reference_keys": ["sources/studio/6cb6c9d6-…/subject-references/ef104c67….png"],
  "direction": "full body, facing camera, neutral stance",
  "request_id": "subject-1"
}
```

`request_id` is the caller's idempotency key (`[A-Za-z0-9][A-Za-z0-9_-]*`, ≤100
chars). Re-sending the same `request_id` will not start a second generation, which
is what makes a retry after a network timeout safe. An identical retry at the same
revision returns the original operation. Re-sending a `request_id` with any other
body, or after the project has moved to a new revision, answers `409` with
`{"detail": {"code": "request_id_reused", ...}}` and starts nothing: send a new
`request_id` for each new request. Opening-pose compose and regenerate follow the
same rule and share one `request_id` space. The response is an operation:

```json
{
  "job_id": "3b0f0d0c-...",
  "operation": "generate_subject",
  "status": "queued",
  "motion_id": null,
  "output_key": "renders/studio/…",
  "progress_percent": null,
  "stage": "queued",
  "failure_code": null,
  "failure_explanation": null,
  "retryable": false,
  "created_at": "2026-09-06T15:46:02.101992Z",
  "updated_at": "2026-09-06T15:46:02.101992Z",
  "settlement_blocked": null
}
```

`503` here means subject generation is not enabled on this deployment.

### 5. Poll `/operations` and reconcile

```http
GET /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/operations
```

```json
[]
```

Nothing is in flight when no operation's `stage` is `queued`, `generating` or
`awaiting_settlement`. Settle finished work into the manifest:

```http
POST /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/activity/reconcile
```

```json
{"operations": [], "next_cursor": null}
```

The returned page is the activity ledger, newest first; `next_cursor` pages backwards
(`GET .../activity?cursor=…&limit=50`, and `&motion_id=…` to scope it to one motion).
Pass `next_cursor` back unchanged: a cursor the API did not issue answers `422`
(`invalid_cursor`).
A failed operation carries `failure_code`, a human-readable `failure_explanation`, and
`retryable`.

The polling loop that works: `POST /activity/reconcile` → `GET /operations` → if any
operation is still queued, generating or awaiting settlement, sleep and repeat →
otherwise re-read the project and check the manifest. Reconcile on every pass, not
only at the end: for work on an attached machine or Higgsfield, reconciling is what
moves a finished job forward. A job that stays `awaiting_settlement` with
`settlement_blocked` set will not settle on its own; report that reason rather than
waiting. A settled job leaves the list, and a failed one stays in it with
`failure_code` and `failure_explanation`.

### 6. Approve the subject

```http
POST /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/subject/approve
Content-Type: application/json

{"expected_revision": 2}
```

Returns the project at its new revision, with the subject assessment attached:

```json
{
  "revision": 3,
  "manifest": {
    "subject": {
      "current_revision": {
        "revision_id": "82ae2ead-a903-41d2-a0e2-ee21fc019d89",
        "artifact_key": "sources/studio/…/subjects/3097e42f….png",
        "sha256": "3b112b3966ed5964df6c07ee962e72af01d2338e649ca478a78e837f913586d2",
        "width": 1024,
        "height": 1024,
        "lifecycle": "ready",
        "source": "uploaded"
      },
      "assessment": {
        "assessment_id": "2b09389c7739230661f6d146e807e595b2cf3a8fd2888674fdbd6b81e20cce8a",
        "policy_version": "subject-policy-v1",
        "artifact_sha256": "3b112b3966ed5964df6c07ee962e72af01d2338e649ca478a78e837f913586d2",
        "width": 1024,
        "height": 1024,
        "occupancy_ratio": 0.371337890625,
        "findings": []
      },
      "approval": {"state": "approved", "approver_id": "iap:112233445566778899"}
    }
  },
  "primary_action": "add_motions"
}
```

To acknowledge a subset of findings explicitly, send
`{"expected_revision": n, "waived_finding_ids": ["subject_occupancy_too_low"], "waiver_reason": "stylised silhouette"}`.
The ids are the finding `code` values; an unknown code is a `422`.

### 7. Add motions (the recommended set)

The recommended set is `idle`, `walk`, `attack`, `guard`, `cast`, `hurt` — the six the
studio's own "add the recommended set" button adds. `GET /studio/presets` lists all
ten with their `semantic_slot`, `loop_policy` and `animation_type`:

```json
[
  {"preset": "idle", "display_name": "Idle", "description": "Resting loop with subtle breathing motion.",
   "semantic_slot": "idle", "loop_policy": "loop", "animation_type": "movement"},
  {"preset": "attack", "display_name": "Attack", "description": "One decisive weapon swing.",
   "semantic_slot": "attack", "loop_policy": "once", "animation_type": "attack"}
]
```

Add them in one call:

```http
POST /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/motions/batch
Content-Type: application/json

{
  "expected_revision": 3,
  "motions": [
    {"motion_id": "idle",   "name": "Idle",   "kind": "preset", "preset": "idle",   "semantic_slot": "idle",   "loop_policy": "loop"},
    {"motion_id": "walk",   "name": "Walk",   "kind": "preset", "preset": "walk",   "semantic_slot": "walk",   "loop_policy": "loop"},
    {"motion_id": "attack", "name": "Attack", "kind": "preset", "preset": "attack", "semantic_slot": "attack", "loop_policy": "once"},
    {"motion_id": "guard",  "name": "Guard",  "kind": "preset", "preset": "guard",  "semantic_slot": "guard",  "loop_policy": "once"},
    {"motion_id": "cast",   "name": "Cast",   "kind": "preset", "preset": "cast",   "semantic_slot": "cast",   "loop_policy": "once"},
    {"motion_id": "hurt",   "name": "Hurt",   "kind": "preset", "preset": "hurt",   "semantic_slot": "hurt",   "loop_policy": "once"}
  ]
}
```

Returns the project at the next revision with the six motions in `manifest.motions`.
Re-using a `motion_id` already on the project is a `409` listing the duplicates.

A custom motion is `{"kind": "custom", "prompt": "…", "motion_id": "…", "name": "…"}` —
`kind: "custom"` requires a non-empty `prompt` and forbids `preset`; `kind: "preset"`
requires a `preset`.

### 8. Generate motions (batch)

```http
POST /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/motions/generate-batch
Content-Type: application/json

{
  "expected_revision": 4,
  "request_id": "batch-1",
  "motion_ids": ["idle", "walk", "attack", "guard", "cast", "hurt"]
}
```

```json
{
  "queued": [
    {"motion_id": "idle", "operation": {"job_id": "…", "operation": "motion_generate", "status": "queued", "stage": "queued"}}
  ],
  "failed": [
    {"motion_id": "cast", "code": "motion_reference_missing", "message": "…"}
  ]
}
```

The call is partial-success by design: `queued` and `failed` are both returned and a
motion in `failed` costs nothing — fix it and re-send just that one. Then poll and
reconcile exactly as in step 5; each finished job adds a **candidate** ("take") to its
motion.

Optional `settings` tunes the generation, and `shared_starting_pose: true` makes every
dispatched motion start from the approved composed opening frame.

### 9. List takes for a motion

Takes live on the project, so read them from the project:

```http
GET /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d
```

`manifest.motions[]` each carry `candidates[]` and `selected_candidate_id`:

```json
{
  "motion_id": "idle",
  "name": "Idle",
  "kind": "preset",
  "preset": "idle",
  "loop_policy": "loop",
  "required": true,
  "candidates": [
    {
      "candidate_id": "cand-7f2a…",
      "artifact_key": "renders/studio/…/idle/cand-7f2a….png",
      "sha256": "…",
      "duration_ms": 1200,
      "frame_duration_ms": 100,
      "loop_policy": "loop",
      "facing": "right",
      "source": "generated",
      "assessment": {"policy_version": "motion-policy-v1", "findings": []}
    }
  ],
  "selected_candidate_id": null
}
```

A candidate can be previewed without downloading the project's assets:
`GET .../motions/{motion_id}/candidates/{candidate_id}/sheet.png`, `…/preview.webm`,
`…/preview.gif`.

### 10. Select a take

```http
POST /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/motions/idle/candidates/cand-7f2a…/select
Content-Type: application/json

{"expected_revision": 9}
```

Returns the project with `selected_candidate_id` set on that motion. Every **required**
motion needs a selected take before the set can be approved.

### 11. Run set quality

```http
POST /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/quality/set
Content-Type: application/json

{"expected_revision": 15}
```

The response carries `manifest.set_assessment`:

```json
{
  "policy_version": "set-policy-v1",
  "findings": [
    {"code": "set_canvas_mismatch", "severity": "warning",
     "message": "Selected motions must share a frame canvas.", "action_id": null}
  ]
}
```

`manifest.set_assessment_fresh` tells you whether the assessment still matches the
current set; it goes `false` the moment anything in the set changes.

### 12. Approve the set

```http
POST /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/set/approve
Content-Type: application/json

{"expected_revision": 16}
```

Findings never block this. Omitting `waived_finding_ids` records **every** finding
present at approval time as acknowledged, with `waiver_reason: "acknowledged"` —
which is what you want unless you are deliberately acknowledging a subset.

Two `409`s are possible and they are different problems:

```json
{"detail": {"message": "expected revision is stale", "current_revision": 5}}
```

```json
{"detail": "every required motion needs a selected candidate: ['idle', 'walk']"}
```

The first is optimistic concurrency — re-read and retry. The second is real work
missing — go back to step 10.

### 13. Create a release

A release freezes the approved set: it pins the subject revision, the selected take
per motion, the quality policy versions and the approval, and it is immutable.

```http
POST /studio/projects/6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d/releases
Content-Type: application/json

{"expected_revision": 17, "name": "v1"}
```

```json
{
  "release_id": "9a1c2f30-…",
  "project_id": "6cb6c9d6-e78b-48c1-a6ba-fcd5b669b47d",
  "name": "v1",
  "source_revision": 17,
  "manifest_digest": "c49153b7…",
  "release_digest": "40f6a1c9…",
  "subject_revision_id": "82ae2ead-a903-41d2-a0e2-ee21fc019d89",
  "motion_revision_ids": {"idle": "cand-7f2a…", "walk": "cand-11bd…"},
  "quality_policy_versions": ["subject-policy-v1", "set-policy-v1"],
  "set_approval": {"state": "approved", "approver_id": "iap:112233445566778899"},
  "deliveries": [],
  "created_at": "2026-09-06T16:10:44.201188Z"
}
```

Release names are unique per project — reusing one is a `409`.
`GET /studio/projects/{id}/releases` lists them.

### 14. Request an export

```http
POST /studio/releases/9a1c2f30-…/exports
Content-Type: application/json

{"profile": "png-json", "expected_revision": 17}
```

```json
{
  "export_id": "9a1c2f30-…-png-json-2",
  "release_id": "9a1c2f30-…",
  "profile_name": "png-json",
  "profile_version": "2",
  "artifact_key": "exports/studio/…/v1-png-json-2.zip",
  "sha256": "6c1f9e…",
  "size_bytes": 4718592,
  "created_at": "2026-09-06T16:11:02.664301Z",
  "completed_at": "2026-09-06T16:11:07.118920Z"
}
```

`profile` defaults to `png-json` (sprite sheets plus a `manifest.json`). `gaia` is the
other installed profile; `GET /studio/targets` lists installed target adapters.
Exports are immutable and content-addressed: asking again for the same profile on the
same release returns the same record rather than re-running the work.
`GET /studio/exports/{export_id}` re-reads one, and
`GET /studio/releases/{release_id}/exports` lists a release's exports.

### 15. Download the export

```http
GET /studio/exports/9a1c2f30-…-png-json-2/download
```

```json
{
  "key": "exports/studio/…/v1-png-json-2.zip",
  "upload_url": "/studio/exports/9a1c2f30-…-png-json-2/file"
}
```

The field is named `upload_url` because the shape is shared with the upload routes;
for a download it is an API path. `GET` it with the same API key and check the bytes
against the export's `sha256`.

```bash
curl -sS -H "Authorization: Bearer $MVS_KEY" -o release.zip "https://api.magicstudios.ai$DOWNLOAD_PATH"
shasum -a 256 release.zip   # must equal the export's sha256
```

**Every file link is yours alone.** Previews, downloads, candidate sheets and
exports are API paths (`/studio/projects/{id}/files?key=…`,
`/studio/exports/{id}/file`, and the movie and scene-flight file routes) that check
the caller's own session or API key on every request and stream the bytes. Nothing
hands out a bearer storage URL, so a copied link answers `404` to anyone else.
Range requests are supported for video seeking.


## Make a movie

A movie is a list of shots. Each shot starts from an opening image and becomes a
video take. The cut joins the selected takes, with music and narration, and a release
freezes the result as an MP4. `GET /studio/movies/capabilities` reports, in
`generation_available`, whether this account can start takes: a public account needs
its own machine, and an invited account can also use the shared machines. Most writes carry
`expected_revision` and return the movie at its new `revision`; the exceptions are
listed under [Revisions in movies and scene flights](#revisions-in-movies-and-scene-flights).

### 1. Create a movie

```http
POST /studio/movies
Content-Type: application/json

{"name": "Heist"}
```

The response is the movie, with `movie_id` and `revision` 1. `GET
/studio/movies/capabilities` reports what this account can run: takes, opening images,
dialogue and the shot length limits.

### 2. Write the shots

```http
PUT /studio/movies/{movie_id}/story
Content-Type: application/json

{"expected_revision": 1, "brief": "A night-time heist on a rooftop",
 "shots": [
   {"shot_id": "s1", "name": "Rooftop", "prompt": "The thief crouches at the edge and looks down"},
   {"shot_id": "s2", "name": "Jump", "prompt": "The thief leaps to the next roof", "continuation_of": "s1"}
 ]}
```

Keep each shot's `prompt` to one physical action. A shot with `continuation_of`
starts from the previous shot's last frame. `PUT …/characters` sets the cast, and a
shot with `dialogue` becomes a voiced take. `POST …/plan` drafts shots from the brief.

### 3. Give each opening shot an image

```http
POST /studio/movies/{movie_id}/uploads
Content-Type: application/json

{"content_type": "image/png"}
```

PUT the image bytes to the returned `upload_url`, with any `upload_headers`. Then bind
the returned `asset_key` to the shot, using the `revision` the grant returned:

```http
POST /studio/movies/{movie_id}/shots/s1/opening
Content-Type: application/json

{"expected_revision": 3, "asset_key": "…"}
```

`POST …/shots/{sid}/compose` generates an opening image instead. A shot with `dialogue`
needs its own opening image (its plate) even when it continues another shot.

### 4. Build

```http
POST /studio/movies/{movie_id}/build
Content-Type: application/json

{"expected_revision": 4, "request_id": "build-1"}
```

The build generates a take for every shot, cuts them in story order and assembles
the movie. It moves forward only while something reconciles, so poll:

```http
POST /studio/movies/{movie_id}/reconcile
```

Repeat until `build.status` is `succeeded` or `failed`; `build.error` says why a
build failed. Each reconcile can save a new revision, so send the `revision` from the
latest response with every later write.

To retry a shot, `POST …/generate` with its `shot_ids` (at most `batch_size` takes run
at once), reconcile until it finishes, then `POST …/shots/{sid}/select` with a
`take_id`. Selecting a take doesn't change the saved cut. Assembly renders the cut, so
after a retry `POST …/build` again with a new `request_id`: it reuses each shot's
selected take, saves a new story-order cut and assembles it. `GET …/selected-cut`
returns the cut from the selected takes, if you'd rather edit and save it yourself.

### 5. Cut, assemble and release

A build saves a cut and assembles it. To change it, `PUT …/cut` with `clips` (shot,
take, trims, transitions, mute), `PUT …/soundtracks` for music and narration, then:

```http
POST /studio/movies/{movie_id}/assemble
Content-Type: application/json

{"expected_revision": REVISION, "request_id": "assemble-1"}
```

`REVISION` here and below is the `revision` from the latest response. Reconcile until
`assembly.status` is `succeeded`, then release:

```http
POST /studio/movies/{movie_id}/release
Content-Type: application/json

{"expected_revision": REVISION, "name": "v1"}
```

The movie's `releases` lists it. `GET …/releases/{release_id}/download` returns the
MP4.

## Fly through a scene

A scene flight is a set of legs. Each leg is a camera move that starts on an origin
image and ends exactly on a destination image. When one leg ends on the image the
next begins from, the chain plays as one continuous move. Scene flights run under the
same account rules as movies. Check both capabilities before creating a flight:
`generation_available` in `GET /studio/scene-flights/capabilities` says whether this
server can make flights at all, and in `GET /studio/movies/capabilities` whether this
account can start takes. Most
writes carry `expected_revision`, and a stale one answers `409` with
`current_revision`; the exceptions are listed under
[Revisions in movies and scene flights](#revisions-in-movies-and-scene-flights).

### 1. Create a flight and add images

```http
POST /studio/scene-flights
Content-Type: application/json

{"name": "Canyon"}
```

Add each location image as base64:

```http
POST /studio/scene-flights/{flight_id}/images
Content-Type: application/json

{"content_base64": "…", "content_type": "image/png"}
```

Each response has the image's `image_key`. `GET /studio/scene-flights/capabilities`
reports whether this server can pin a last frame, and the recipe: 768×448, 24 fps,
20 steps, 5–15 s.

### 2. Define the legs

```http
PUT /studio/scene-flights/{flight_id}/legs
Content-Type: application/json

{"expected_revision": 3, "legs": [
  {"leg_id": "a", "origin_image_key": "…", "destination_image_key": "…",
   "prompt": "The camera flies low along the river toward the arch"}
]}
```

Each leg needs a `prompt`, or both `origin_name` and `destination_name`.

### 3. Generate and choose takes

```http
POST /studio/scene-flights/{flight_id}/legs/a/takes
Content-Type: application/json

{"expected_revision": 4, "request_id": "a-take-1"}
```

A flight runs at most three takes at once, so start up to three legs, then poll
before starting more. Poll `GET /studio/scene-flights/{flight_id}`. Reading the flight
settles finished takes and can save a new revision, so send the `revision` from the
latest read with each write. Send a new `request_id` to re-roll a leg. The newest
successful take is selected; to choose another:

```http
POST /studio/scene-flights/{flight_id}/legs/a/select
Content-Type: application/json

{"expected_revision": REVISION, "take_id": "…"}
```

Every take records its seed, steps, frame count, output digest and the measured
difference between its first and last frames and the pinned images.
`GET …/legs/{leg_id}/takes/{take_id}/video?download=true` downloads one take.

### 4. Chain the legs

```http
POST /studio/scene-flights/{flight_id}/chain
Content-Type: application/json

{"expected_revision": REVISION, "request_id": "chain-1"}
```

The chain joins the selected takes in leg order. Poll the flight until
`chain.status` is `succeeded`, then download it with
`GET /studio/scene-flights/{flight_id}/chain/video?download=true`.

---


## Gather audio

An audio set holds cues: a sound effect, an ambience bed, a music cue or a voice
line. Each cue collects candidates from Freesound or from your own uploads; you keep
or reject each, select the variants to ship, and release the selection as a zip with
a licence manifest and credits. `GET /studio/audio/capabilities` says which sources
and processing this account can use. Every write carries `expected_revision`, and a
stale one answers `409` with `current_revision`.

### 1. Create a set and its cues

```http
POST /studio/audio
Content-Type: application/json

{"name": "Glass"}
```

```http
PUT /studio/audio/{id}
Content-Type: application/json

{"expected_revision": 1, "name": "Glass", "licence_policy": "cc0",
 "cues": [{"cue_id": "crack", "name": "Glass crack", "kind": "sfx", "prompt": "glass crack", "keep": 2}]}
```

`licence_policy` is `cc0` (nothing you ship carries an attribution duty) or
`cc0_cc_by`, which also admits CC-BY sounds and credits them in each release.
Non-commercial and Sampling+ sounds are never offered.

### 2. Gather candidates

`GET /studio/audio/{id}/free-sounds?q=glass%20crack&max_seconds=5` searches
Freesound within the set's licence policy. Import a result into a cue:

```http
POST /studio/audio/{id}/cues/{cue_id}/import
Content-Type: application/json

{"expected_revision": 2, "source_id": "101"}
```

The candidate records the licence, author and source URL as Freesound reported them,
and is marked a `preview`: Freesound serves its high-quality MP3 preview without each
user's OAuth. To use the original, download it from Freesound yourself, upload it, and
call `POST …/candidates/{candidate_id}/original`.

Your own files go through `POST /studio/audio/{id}/uploads` (a signed PUT, as for
movies) and then `POST …/cues/{cue_id}/upload` with the granted `asset_key`.
Invited accounts' candidates get a loudness rendition (-16 LUFS, 44.1 kHz WAV) and
measurements; public accounts' keep the original only.

### 3. Review, select and release

```http
POST /studio/audio/{id}/candidates/{candidate_id}/review
Content-Type: application/json

{"expected_revision": 3, "outcome": "keep", "note": "clean tail"}
```

A review is bound to the file's hash and records whether a person or an API key gave
it. Send the `sha256` you listened to, and a verdict on a file replaced since is refused. `PUT …/cues/{cue_id}/selection` takes kept candidates, at most the cue's `keep`.
`GET /studio/audio/{id}/readiness` lists what blocks a release; then:

```http
POST /studio/audio/{id}/release
Content-Type: application/json

{"expected_revision": 6, "name": "v1"}
```

A release never changes. `GET /studio/audio/{id}/releases/{release_id}/download`
returns the zip: `audio/`, `originals/`, `manifest.json` and `CREDITS.md`.
`POST …/candidates/{candidate_id}/send-to-movie` adds a kept candidate to one of your
movies as a soundtrack cue. CC-BY sounds are refused, because movie releases carry no
credits yet.

## Revisions in movies and scene flights

Most writes to an existing movie or scene flight carry `expected_revision` and return
the movie or flight at its new `revision`. A stale one answers `409` with
`current_revision`. These writes take no `expected_revision`. Sending one is a `422`,
except to reconcile, which takes no body and ignores one:

<!-- writes-without-revision -->
| Write | Returns |
|---|---|
| `POST /studio/movies` | The new movie, at revision 1 |
| `DELETE /studio/movies/{id}` | `204`. Send `confirm_name`. |
| `POST /studio/movies/{id}/reconcile` | The movie, at a new revision if anything settled |
| `POST /studio/movies/{id}/uploads` | An upload grant, with the movie's new `revision` |
| `POST /studio/scene-flights` | The new flight, at revision 1 |
| `DELETE /studio/scene-flights/{id}` | `204`. Send `confirm_name`. |
| `POST /studio/scene-flights/{id}/images` | The image's `image_key`, with the flight's new `revision` |
<!-- /writes-without-revision -->

`POST /studio/movies/{id}/plan` takes `expected_revision` but saves nothing. It
returns a draft of shots, and the movie keeps its revision.

## Deleting projects, movies and your account

Deletion is permanent. Each request repeats the name of what it deletes:

```http
DELETE /studio/projects/{project_id}        {"confirm_name": "Ember Knight"}
DELETE /studio/movies/{movie_id}            {"confirm_name": "Heist"}
DELETE /studio/scene-flights/{flight_id}    {"confirm_name": "Canyon"}
```

Each answers `204`, and removes the item's files (every stored version), jobs,
releases, exports and history; a wrong name is `422`
(`deletion_not_confirmed`), and someone else's item is `404`.

`DELETE /studio/account` with `{"confirm_email": "<your account email>"}` deletes the
whole account: every project, movie and scene flight, saved provider keys, API keys,
attached machines, sessions and the user record. It accepts a browser session only,
never an API key, and answers with the Auth0 `logout_url` the browser should visit.

## Automatic deletion of inactive work

A project, movie or scene flight that nobody opens for 90 days is deleted, unless it
has a release; anything it still shows as running is cancelled. Any owner request on the item (reading it
included) counts as opening it. During the last 14 days, `GET /studio/projects`,
`GET /studio/movies` and `GET /studio/scene-flights` carry `expires_at` on that item;
otherwise it is `null`. While the item is being deleted, owner requests on it answer
`410` with code `item_expiring`.

## Prompt screening

Every prompt is checked against the acceptable-use rules
(https://magicstudios.ai/terms) before any generation is queued: motion actions and
prompts, subject directions, opening-pose prompts, movie shot text, dialogue, cast
descriptions, planner briefs and scene-flight legs. A refused prompt answers `422`
with `{"detail": {"code": "prompt_refused", "category": "...", "message": "..."}}`
and nothing is saved or started; `category` is one of `minors`, `sexual`,
`real_people`, `harassment`, `extremism`, `self_harm`. In a batch, a refused stored
prompt fails that motion with code `prompt_refused` while the rest run. If screening
itself is unavailable the request answers `503` (`prompt_screen_unavailable`) and
nothing runs unscreened; retry later.

## Status codes

| Code | Meaning |
|---|---|
| `401` | Bad or revoked API key |
| `403` | Work this account may not start, such as house compute for a public account (`detail.code` says which) |
| `404` | Not found, or not yours — the two are deliberately indistinguishable |
| `409` | Stale `expected_revision` (with `detail.current_revision`), a reused `request_id` (`request_id_reused`), a duplicate name/id, or a precondition not met |
| `410` | The item is being deleted for inactivity (`item_expiring`) |
| `422` | The request body or a referenced id is invalid, a prompt was refused (`prompt_refused`), or a missing browser identity |
| `429` | Too many jobs running at once; wait for one to finish |
| `503` | A generation backend or storage is unavailable; retry later |
| `507` | Your storage allowance is full (`owner_storage_limit_reached`); delete something before adding more |

A stale-revision `409` is retryable once you re-read the project and send its current
`revision`. A `request_id_reused` `409` never succeeds as sent: send a new `request_id`.
Any other `409` means a precondition is missing or a name is taken; fix that first.
`429` and `503` are retryable as sent, after a wait. `401`, `403`, `404`, `410` and `422`
are not retryable, and `507` is not until space is freed.

---

## MCP server

The MCP (Model Context Protocol) server wraps this API as 63 tools, so an agent can
drive the studio without writing HTTP. It threads `expected_revision` for you and
retries once on a stale one.

It runs on your computer, over stdio or Streamable HTTP, and calls this API with your
key. There is no hosted MCP endpoint. Install it with uv or pipx (it needs Python 3.13
or later, which uv can install):

```bash
uv tool install https://magicstudios.ai/studio/downloads/magic_studios_mcp-0.2.2-py3-none-any.whl
```

That gives you the `magic-studios-mcp` command. Add it to your agent with your key:

```bash
claude mcp add magic-studios --env MAGIC_STUDIOS_API_KEY=mvs_… -- magic-studios-mcp
codex mcp add magic-studios --env MAGIC_STUDIOS_API_KEY=mvs_… -- magic-studios-mcp
```

For Claude Desktop, put the same command and `env` under `mcpServers` in
`claude_desktop_config.json`. `MAGIC_STUDIOS_URL` defaults to
`https://api.magicstudios.ai`.

The agent skill, which explains the sprite workflow step by step, is
`https://magicstudios.ai/studio/downloads/magic-studios-skill-0.2.2.zip`. Unzip it into
`~/.claude/skills/` for Claude Code or `~/.agents/skills/` for Codex. `GET
/studio/downloads` lists both files with their SHA-256. The
[guide](https://magicstudios.ai/guide#agents) has the full steps.

The tools cover projects and subjects, motions and takes (including native frame
inspection, loop suggestion, body-scale matching and saved frame revisions), quality,
releases and exports, movies and scene flights. Start with `get_generation_config`.

## Movie character voice clips

Attach an authorized WAV or MP3 voice reference to an existing movie character:

1. `POST /studio/movies/{movie_id}/uploads` with
   `{"content_type":"audio/wav"}` (or `audio/mpeg`).
2. PUT the raw clip bytes to the returned `upload_url`, using that content type.
   The signed storage URL needs no Studio authorization header.
3. `PUT /studio/movies/{movie_id}/characters/{character_id}/voice` with
   `{"expected_revision":<revision from upload grant>,"asset_key":"<granted asset_key>"}`.

The attachment returns the updated movie. It validates the clip's 2–15 second
duration and 50 MiB size limit, freezes the bytes under an immutable asset key,
preserves the remaining cast and portraits, and invalidates affected dialogue takes.
The upload grant advances the movie revision. Attachment returns 409 on concurrent
changes; inspect the current movie before retrying. Unknown movies/characters return
404, ungranted asset keys return 403, and invalid clips return 422.

The MCP tool `attach_movie_voice(movie_id, character_id, path)` performs all three
steps for a local file. It requires an owner API key and does not retry conflicting
attachments automatically. It does not create the character or generate video.

## Machine defaults and execution provenance

Authenticated `GET /studio/generation-config` lists authorized machines and their
verified recipes/bounds. Optional `worker_id` narrows selection. Copy returned
`settings` into motion generation requests to bind worker, recipe and configuration
version; omitted steps resolve before submission. A stale/offline/incompatible
selection is rejected. The controller owns selection and tenant authorization.
Operation responses include `requested_settings`, `resolved_recipe`, resolved
`generation_settings` and `execution_provenance` after completion. Takes retain
that provenance through alpha derivation and frame revisions. Legacy standard
workers without fingerprints retain their old route and are reported unverified.
The response also carries `account: {tier, house_compute, h3, own_machine}`; when
`h3` is false no machine is `selected` and `settings` is null, since every machine
recipe is MiniMax H3.

## Bring your own Higgsfield key

An owner can run motions on their own Higgsfield account instead of a GPU
worker; Higgsfield bills that account directly. The key is saved and removed
from a browser session only (`PUT`/`DELETE /studio/providers/higgsfield` with
`{"api_key": "<the key exactly as Higgsfield shows it>"}`), stored encrypted, and never returned:
`GET /studio/providers` reports only `configured` and a `key_hint` such as
`…9876`. API keys can read that status but cannot set or remove a provider key.

`GET /studio/generation-config` includes `providers.higgsfield`: whether keys
are enabled on this server, whether one is saved, the available models with
their duration ranges, and ready `settings` once a key exists. Generation uses
Higgsfield only when the request's `settings.provider_parameters` carry
`execution_provider: "higgsfield"` (and optionally `higgsfield_model`); machine
binding fields are rejected on such a request, and nothing falls back between
Higgsfield and GPU workers. Studio sends the approved subject's opening frame
(and, with `closed_loop`, the same frame as the last frame), the compiled prompt
and the duration. Steps and seed are not sent.

Operations reconcile through the same `/operations` reads as pool jobs. A
completed take's `execution_provenance` records `completion_path:
"higgsfield_api"`, the Higgsfield request ID and model, and the SHA-256 of the
downloaded video; it is provider-reported, not worker-attested. A submission
Higgsfield rejects fails immediately; one whose outcome is unknown after ten
minutes fails without being resubmitted. Cancelling an operation also asks
Higgsfield to cancel a request that has not started.
