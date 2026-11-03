"""Reference entries for account, sign-in, key, access, machine, provider, tip and
reference-data routes."""

from typing import Any

from pydantic import BaseModel, Field

from magic_video_editor.api_reference.model import RouteDoc
from magic_video_editor.studio.models import MotionGenerationSettings


class MachineFeatures(BaseModel):
    last_frame: bool = Field(description=(
        "Whether the machine reports closed-loop (last-frame) support on the standard recipe. "
        "A turnaround needs an available machine where this is true."))


class GenerationMachine(BaseModel):
    worker_id: str
    status: str = Field(description="`online`, `busy`, `offline` or `draining`.")
    available: bool = Field(description="Whether it can take a job now.")
    profile: dict[str, Any] | None = Field(description="The verified recipe it runs, or null.")
    configuration_version: str | None
    capability_ids: list[str]
    legacy_standard: bool
    legacy_available: bool
    runtime_versions: dict[str, Any]
    features: MachineFeatures = Field(description="Optional capabilities the machine itself reported.")
    reason: str | None = Field(description="Why it is not available, when it is not.")


class AccountCapabilities(BaseModel):
    tier: str = Field(description="`invited` or `public`.")
    house_compute: bool = Field(description="Whether the studio's shared machines may run your work.")
    h3: bool = Field(description="Whether you may run MiniMax H3 recipes; every machine recipe is one.")
    own_machine: bool = Field(description="Whether you have attached a machine.")


class HiggsfieldModelInfo(BaseModel):
    model: str
    label: str
    path: str
    closed_loop: bool = Field(description="Whether it can end on the same frame it starts from.")
    duration_seconds: dict[str, float] = Field(description="`min` and `max`.")
    evidence: str


class HiggsfieldOption(BaseModel):
    available: bool = Field(description="Whether provider keys are enabled on this server.")
    configured: bool = Field(description="Whether you saved a Higgsfield key.")
    key_hint: str | None = None
    billing: str | None = None
    models: list[HiggsfieldModelInfo] = []
    settings: MotionGenerationSettings | None = Field(
        default=None, description="Pass these to motion generation to run on your Higgsfield account."
    )
    reason: str | None = None


class GenerationConfig(BaseModel):
    scope: str
    selection_policy: str | None = None
    workers: list[GenerationMachine]
    selected: GenerationMachine | None = Field(description="The default machine, or null.")
    settings: MotionGenerationSettings | None = Field(
        description="The default machine's settings. Pass them unchanged to motion generation."
    )
    account: AccountCapabilities
    providers: dict[str, HiggsfieldOption] = Field(description="Keyed by provider; today only `higgsfield`.")


class Download(BaseModel):
    name: str = Field(description="The file name, which carries the version.")
    sha256: str = Field(description="Hex SHA-256 of the file. Check it after downloading.")
    size: int = Field(description="Bytes.")
    summary: str
    url: str = Field(description="Where to download it, relative to this host.")


class DownloadList(BaseModel):
    files: list[Download]


class SetupCapabilities(BaseModel):
    schema_versions: list[int]
    current_schema_version: int
    conventions: list[str] = Field(description="Sprite layouts the setup routes accept.")
    partial_matrix: bool
    atomic_reconciliation: bool
    reference_binding: str
    version_header: str = Field(description="The header to send the schema version in.")
    reference_staging: bool

_ORIGIN = "The request did not come from the studio's own pages (`csrf_origin_mismatch`)."

ROUTES: dict[tuple[str, str], RouteDoc] = {
    # Account
    ("GET", "/studio/auth/session"): RouteDoc(
        tag="Account",
        summary="Get the current account",
        description=(
            "Returns who you are: `actor_id`, `email`, `display_name`, how you signed in, "
            "and whether you are an admin.\n\n"
            "`usage` reports your stored bytes, storage allowance, running jobs and how "
            "many may run at once. A limit of `0` means unlimited. `account_tier` is "
            "`invited` or `public`.\n\n"
            "Works with an API key, so it is a good first call to check a key."
        ),
    ),
    ("GET", "/studio/auth/login"): RouteDoc(
        tag="Account",
        summary="Start signing in",
        description=(
            "Redirects the browser to the Auth0 sign-in page. After signing in, the "
            "browser returns to `return_to`, a path on this site. With `signup=true` "
            "the page opens on its sign-up form instead.\n\n"
            "For browsers only. Scripts and agents use an API key instead."
        ),
        errors={503: "Sign-in is not configured on this server."},
        redirect=(302, "Redirects to the Auth0 sign-in page."),
    ),
    ("GET", "/studio/auth/callback"): RouteDoc(
        tag="Account",
        summary="Finish signing in",
        description=(
            "Where Auth0 sends the browser back after sign-in. It sets the session "
            "cookie and redirects to the path the sign-in started from.\n\n"
            "A failed sign-in redirects to `/login?error=auth0`. An address without "
            "access redirects to `/login?error=not_allowed`, and an email address not "
            "verified yet to `/login?error=verify_email`. You never call this yourself."
        ),
        errors={503: "Sign-in is not configured on this server."},
        redirect=(303, "Redirects to the page sign-in started from, or to `/login?error=…`."),
    ),
    ("GET", "/studio/auth/bootstrap"): RouteDoc(
        tag="Account",
        summary="Redirect a signed-in caller to a page",
        description=(
            "Checks that you are signed in, then redirects to `return_to`, a path on "
            "this site."
        ),
        errors={400: "`return_to` is not a path on this site."},
        redirect=(303, "Redirects to `return_to`."),
    ),
    ("POST", "/studio/auth/sign-out"): RouteDoc(
        tag="Account",
        summary="Sign out",
        description=(
            "Ends the browser session and clears its cookie. Returns `logout_url`, "
            "which the browser visits to end the Auth0 session too.\n\n"
            "Signing out without a session is not an error. API keys are not affected; "
            "revoke them separately."
        ),
        errors={403: _ORIGIN},
    ),
    ("DELETE", "/studio/account"): RouteDoc(
        tag="Account",
        summary="Delete your account",
        description=(
            "Permanently deletes your account and everything in it: projects, movies "
            "and scene flights with their files, saved provider keys, API keys, "
            "attached machines and sessions.\n\n"
            "Send your account email in `confirm_email`. Browser session only; an API "
            "key cannot delete an account. Returns `logout_url` for the browser to visit."
        ),
        errors={422: "`confirm_email` does not match your account email (`deletion_not_confirmed`)."},
    ),
    # API keys
    ("POST", "/studio/api-keys"): RouteDoc(
        tag="API keys",
        summary="Create an API key",
        description=(
            "Creates a key that acts as you. The response's `key` is the only time the "
            "plaintext is shown; store it then.\n\n"
            "Browser session only, so a leaked key can never create another one. You "
            "can hold up to 50 live keys."
        ),
        errors={409: "You already hold 50 live keys (`api_key_limit_reached`)."},
    ),
    ("GET", "/studio/api-keys"): RouteDoc(
        tag="API keys",
        summary="List your API keys",
        description=(
            "Lists your keys with their names, creation time, `last_used_at` and "
            "`revoked_at`. The plaintext is never returned.\n\n"
            "`last_used_at` updates at most once a minute per key. Browser session only."
        ),
    ),
    ("DELETE", "/studio/api-keys/{key_id}"): RouteDoc(
        tag="API keys",
        summary="Revoke an API key",
        description=(
            "Revokes the key at once. Its next request gets `401`. Returns the key with "
            "`revoked_at` set. Browser session only."
        ),
        errors={404: "No such key of yours."},
    ),
    # Machines and providers
    ("POST", "/studio/workers/pairing-tokens"): RouteDoc(
        tag="Machines and providers",
        summary="Create a machine pairing code",
        description=(
            "Creates a single-use code for attaching one machine. It expires after an "
            "hour (`expires_at`).\n\n"
            "Browser session only. The studio's **Your machines** page wraps this in "
            "the one command you run on the machine."
        ),
        errors={503: "Attaching machines is not configured on this server."},
    ),
    ("GET", "/studio/workers"): RouteDoc(
        tag="Machines and providers",
        summary="List your machines",
        description=(
            "Lists the machines you have attached, with their labels and status. "
            "Browser session only; with a key, use `GET /studio/generation-config`."
        ),
    ),
    ("PATCH", "/studio/workers/{worker_id}"): RouteDoc(
        tag="Machines and providers",
        summary="Rename a machine",
        description="Changes the label of one of your machines. Browser session only.",
        errors={404: "No such machine of yours."},
    ),
    ("DELETE", "/studio/workers/{worker_id}"): RouteDoc(
        tag="Machines and providers",
        summary="Remove a machine",
        description=(
            "Detaches one of your machines. Its credential stops working. Returns the "
            "machine as removed. Browser session only."
        ),
        errors={404: "No such machine of yours."},
    ),
    ("GET", "/studio/generation-config"): RouteDoc(
        tag="Machines and providers",
        summary="Get generation settings",
        description=(
            "Lists the machines you may generate on, with each one's verified recipe, "
            "bounds and whether it is available now. `selected` and `settings` are the "
            "default. Pass `settings` unchanged to motion generation to bind that "
            "machine and recipe. `worker_id` narrows the list to one machine.\n\n"
            "`account` reports what your account may run. `providers.higgsfield` "
            "reports whether you saved a Higgsfield key and, once you have, ready "
            "`settings` that run the motion on your Higgsfield account. Higgsfield is "
            "used only when a request carries those settings.\n\n"
            "Call this before generating."
        ),
        response=GenerationConfig,
    ),
    ("GET", "/studio/providers"): RouteDoc(
        tag="Machines and providers",
        summary="List provider keys",
        description=(
            "Reports, per provider, whether keys are enabled on this server, whether "
            "you saved one, and its last characters (`key_hint`). The key itself is "
            "never returned."
        ),
    ),
    ("PUT", "/studio/providers/{provider}"): RouteDoc(
        tag="Machines and providers",
        summary="Save a provider key",
        description=(
            "Saves your key for `provider` (`higgsfield`), encrypted. The provider bills "
            "that account directly for work you send to it.\n\n"
            "Browser session only, so a leaked studio key cannot swap in another "
            "billing account."
        ),
        errors={
            404: "No such provider.",
            422: "The provider rejected the key.",
            503: "Provider keys are not enabled on this server.",
        },
    ),
    ("DELETE", "/studio/providers/{provider}"): RouteDoc(
        tag="Machines and providers",
        summary="Remove a provider key",
        description="Deletes your saved key for `provider`. Browser session only.",
        errors={404: "No such provider."},
    ),
    # Downloads
    ("GET", "/studio/downloads"): RouteDoc(
        tag="Downloads",
        summary="List the downloads",
        description=(
            "Lists the MCP server's wheel and the agent skill with each file's SHA-256, "
            "size and download path. Needs no sign-in.\n\n"
            "Install the server with `uv tool install` or `pipx install` and the wheel's "
            "full URL."
        ),
        response=DownloadList,
    ),
    ("GET", "/studio/downloads/{name}"): RouteDoc(
        tag="Downloads",
        summary="Download a file",
        description=(
            "Returns one file the list names, as an attachment. Names carry the version, "
            "and the `ETag` is the file's SHA-256 from the list. Needs no sign-in."
        ),
        errors={404: "No download by that name."},
        file="The wheel or the skill zip.",
    ),
    # Reference data
    ("GET", "/studio/presets"): RouteDoc(
        tag="Reference data",
        summary="List motion presets",
        description=(
            "Lists the built-in animations with their `semantic_slot`, `loop_policy` "
            "and `animation_type`. Use a `preset` name when adding motions. Needs no "
            "sign-in."
        ),
    ),
    ("GET", "/studio/targets"): RouteDoc(
        tag="Reference data",
        summary="List export targets",
        description=(
            "Lists the installed target systems a project can bind to or a release can "
            "be delivered to, and whether delivery is configured for each. Needs no "
            "sign-in."
        ),
    ),
    ("GET", "/studio/setup-capabilities"): RouteDoc(
        tag="Setup",
        summary="Get setup capabilities",
        description=(
            "Reports the setup schema versions this server supports and its "
            "conventions. Send the version back as `X-Studio-Setup-Version` on setup "
            "routes."
        ),
        response=SetupCapabilities,
    ),
}
