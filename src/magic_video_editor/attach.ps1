# Attach this Windows machine to Magic Studios.
#
# The studio shows you one command to paste into PowerShell. It installs the
# worker, attaches this machine to your account, and starts it. After this the
# machine renews its own credential -- nothing to keep, copy, or refresh by hand.
#
#   $env:MVS_CODE="<code>"; irm https://magicstudios.ai/studio/attach.ps1 | iex
#
# The code comes through the environment because a piped script cannot take
# arguments: `iex -Args` is not a thing, and `iex` has no such parameter.
#
# Everything it writes lives under your user profile; it installs nothing
# machine-wide and needs no administrator rights.

param(
    [Parameter(Position = 0)]
    [string]$Code = $env:MVS_CODE,
    [string]$StudioUrl = "https://magicstudios.ai"
)

$ErrorActionPreference = "Stop"

if (-not $Code) {
    Write-Host ""
    Write-Host "No pairing code. Copy the command from your machines page:" -ForegroundColor Red
    Write-Host '  $env:MVS_CODE="<code>"; irm https://magicstudios.ai/studio/attach.ps1 | iex'
    Write-Host ""
    exit 2
}
$StudioUrl = $StudioUrl.TrimEnd("/")
$Root = Join-Path $env:USERPROFILE ".magic-video-worker"
$ConfigDir = Join-Path $env:APPDATA "magic-video-studio"
$Config = Join-Path $ConfigDir "worker.env"

function Fail($message) {
    Write-Host ""
    Write-Host "Attaching failed: $message" -ForegroundColor Red
    Write-Host "Setup stopped at this step. See the details above before retrying."
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "Attaching this machine to Magic Studios."
Write-Host ""

# uv brings its own Python, so a machine on an older one needs nothing upgraded.
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    $local = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $local) {
        $env:PATH = "$(Split-Path $local);$env:PATH"
    } else {
        Write-Host "  Installing uv (a Python installer; nothing machine-wide)..."
        try {
            Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        } catch {
            Fail "could not install uv (see https://astral.sh/uv)"
        }
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    }
}

Write-Host "  Preparing the worker..."
# --allow-existing so re-attaching, or retrying after a failure, works rather
# than stopping on the environment the previous attempt left behind.
& uv venv --allow-existing --python 3.13 "$Root\venv" | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "could not create a Python 3.13 environment in $Root\venv" }

$python = "$Root\venv\Scripts\python.exe"
# The shared installer installs [agent,comfy] from an authorized Studio wheel.
# No Git installation or GitHub credentials are needed on this machine.
try {
    Invoke-WebRequest -UseBasicParsing "$StudioUrl/studio/worker-install.py" -OutFile "$Root\worker-install.py"
} catch {
    Fail "could not download the installer from $StudioUrl"
}
$env:MVS_CODE = $Code
try {
    & $python "$Root\worker-install.py" $StudioUrl $Root
    $installExit = $LASTEXITCODE
} finally {
    Remove-Item Env:MVS_CODE -ErrorAction SilentlyContinue
}
if ($installExit -ne 0) { exit 1 }

& $python -c "import outbound_gpu_worker_pool.agent_main" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "the worker installed but cannot start (missing dependency)" }

try {
    Invoke-WebRequest -UseBasicParsing "$StudioUrl/studio/cpu-worker.py" -OutFile "$Root\cpu_worker.py"
    Invoke-WebRequest -UseBasicParsing "$StudioUrl/studio/worker-runtime.py" -OutFile "$Root\worker_runtime.py"
    Invoke-WebRequest -UseBasicParsing "$StudioUrl/studio/worker.py" -OutFile "$Root\worker.py"
} catch {
    Fail "could not download the worker from $StudioUrl"
}

Write-Host "  Attaching..."
# The code is spent here, machine to server. It never reaches a file, and the
# credential that comes back never passes through a person.
# Validate before redeeming; the helper returns only non-secret runtime settings.
$profileOutput = & $python "$Root\worker_runtime.py"
if ($LASTEXITCODE -ne 0) { Fail "the configured ComfyUI profile is not ready; pairing code was not spent" }
$profileSettings = $profileOutput | ConvertFrom-Json

try {
    $redeemed = Invoke-RestMethod -Method Post -ContentType "application/json" `
        -Uri "$StudioUrl/studio/workers/redeem" `
        -Body (@{ token = $Code; label = $env:COMPUTERNAME } | ConvertTo-Json)
} catch {
    Fail "the studio refused this code. Codes work once and last an hour -- ask for another on the machines page."
}

New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
$lines = @(
    "# Written by attach.ps1. The credential below renews itself.",
    "OGWP_WORKER_COORDINATOR_URL=$StudioUrl/pool",
    "OGWP_WORKER_ID=$($redeemed.worker_id)",
    "OGWP_WORKER_TOKEN=$($redeemed.credential)",
    "OGWP_WORKER_TENANT=$($redeemed.tenant_id)",
    "OGWP_WORKER_AUTH=static",
    "MVS_STUDIO_URL=$StudioUrl"
)
foreach ($property in $profileSettings.PSObject.Properties) {
    if ("$($property.Value)" -match "[\r\n]") { Fail "invalid worker configuration" }
    $lines += "$($property.Name)=$($property.Value)"
}
if ($redeemed.credential_expires_at) {
    $lines += "MVS_CREDENTIAL_EXPIRES_AT=$($redeemed.credential_expires_at)"
}
Set-Content -Path $Config -Value $lines -Encoding utf8

# Readable only by this user: the default inherits permissions from the profile
# directory, which is usually fine but is not guaranteed on a shared machine.
$acl = Get-Acl $Config
$acl.SetAccessRuleProtection($true, $false)
$acl.SetAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    "$env:USERDOMAIN\$env:USERNAME", "FullControl", "Allow")))
Set-Acl -Path $Config -AclObject $acl

# A scheduled task so it survives sign-out and reboots, registered for this user
# only -- no administrator rights, and it cannot touch anyone else's session.
$taskName = "MagicVideoStudioWorker"
$action = New-ScheduledTaskAction -Execute $python -Argument "$Root\worker.py" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Force -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 5
    $state = (Get-ScheduledTask -TaskName $taskName).State
    if ($state -eq "Running") {
        Write-Host ""
        Write-Host "Done. This machine is attached and running." -ForegroundColor Green
        Write-Host "It appears on your machines page, and renews itself from here on."
        Write-Host ""
        exit 0
    }
    Write-Host ""
    Write-Host "The machine is attached, but the worker did not stay running." -ForegroundColor Yellow
} catch {
    Write-Host "  Could not register the scheduled task."
}

Write-Host ""
Write-Host "This machine is attached. Start the worker with:"
Write-Host ""
Write-Host "  Get-Content '$Config' | ForEach-Object { if (`$_ -match '^([^#=]+)=(.*)`$') { [Environment]::SetEnvironmentVariable(`$Matches[1], `$Matches[2]) } }"
Write-Host "  & '$python' '$Root\worker.py'"
Write-Host ""
