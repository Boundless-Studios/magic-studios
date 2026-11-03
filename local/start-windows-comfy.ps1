param([ValidateSet('pytorch','sage')][string]$Attention = 'pytorch', [switch]$OpenBrowser, [switch]$WatchStartup)
$ErrorActionPreference = 'Stop'
$root = Join-Path $PSScriptRoot 'ComfyUI_windows_portable'
$python = "$root\python_embeded\python.exe"
# This watcher shares the runtime console and exits after reporting readiness.
if ($WatchStartup) {
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8189/system_stats' -TimeoutSec 2
            if ($health.StatusCode -eq 200) {
                [Console]::Title = 'ComfyUI Server - ready'
                Write-Host ''
                Write-Host 'ComfyUI ready - http://localhost:8189'
                exit 0
            }
        } catch {}
        Start-Sleep -Seconds 1
    }
    Write-Host ''
    Write-Host 'ComfyUI has not passed its health check after 120 seconds. Check the startup logs above.'
    exit 1
}
try {
    $health = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8189/system_stats' -TimeoutSec 2
    if ($health.StatusCode -eq 200) {
        if ($OpenBrowser) { Start-Process 'http://localhost:8189' }
        Write-Output 'Windows ComfyUI is already running.'
        exit 0
    }
} catch {}
$arguments = '-s ComfyUI\main.py --windows-standalone-build --disable-auto-launch --listen 127.0.0.1 --port 8189 --enable-dynamic-vram --fast-disk --disable-pinned-memory --reserve-vram 0 --preview-method none'
if ($Attention -eq 'sage') { $arguments += ' --use-sage-attention' }
# WMI starts the runtime independently of the calling WSL terminal.
$banner = 'title ComfyUI Server & echo ComfyUI Server - starting & echo Address: http://localhost:8189 & echo Attention: ' + $Attention + ' & echo. & echo Startup and generation logs: & echo ' + $root + '\magic-stderr.log & echo ' + $root + '\magic-stdout.log & echo. & echo Keep this window open while using Magic Studios. & echo Starting runtime; initial startup may take a minute... & '
$watcher = 'start "" /b powershell.exe -NoProfile -NonInteractive -File "' + $PSCommandPath + '" -WatchStartup & '
$inner = $banner + $watcher + '"' + $python + '" ' + $arguments + ' >"' + $root + '\magic-stdout.log" 2>"' + $root + '\magic-stderr.log"'
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=('cmd.exe /d /s /c "' + $inner + '"'); CurrentDirectory=$root}
if ($result.ReturnValue -ne 0) { throw "Windows ComfyUI start failed: $($result.ReturnValue)" }
$result.ProcessId | Set-Content "$root\magic-launcher.pid"
Write-Output "Started Windows ComfyUI ($Attention) on http://localhost:8189"

if ($OpenBrowser) {
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8189/system_stats' -TimeoutSec 1
            if ($health.StatusCode -eq 200) { Start-Process 'http://localhost:8189'; break }
        } catch {}
        Start-Sleep -Seconds 1
    }
}
