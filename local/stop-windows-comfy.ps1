$ErrorActionPreference = 'Stop'
$root = Join-Path $PSScriptRoot 'ComfyUI_windows_portable'
$python = "$root\python_embeded\python.exe"
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
    $_.ExecutablePath -eq $python -and $_.CommandLine -like '*--port 8189*'
} | ForEach-Object { Stop-Process -Id $_.ProcessId }
Remove-Item "$root\magic-comfy.pid", "$root\magic-launcher.pid" -ErrorAction SilentlyContinue
