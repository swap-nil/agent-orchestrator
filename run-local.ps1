# Build and run the whole test environment on this machine (no Docker, no Entra ID).
#
#   powershell -ExecutionPolicy Bypass -File run-local.ps1 [-Reinstall] [-NoBrowser]
#
# VS Code: Ctrl+Shift+B runs it (task "Local: build and run", .vscode/tasks.json).
# First run: creates .venv (Python 3.12 via uv), installs the project, downloads the
# LiveKit and Temporal binaries and the voice models. Later runs start in seconds;
# dependencies are reinstalled only when pyproject.toml or the agent lock changes.
# Needs .env.local with speech keys (copy .env.local.example). Ctrl+C stops everything.
param([switch]$Reinstall, [switch]$NoBrowser)

Set-Location $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv is required: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path .env.local)) {
    Copy-Item .env.local.example .env.local
    Write-Host "Created .env.local: add your speech keys there, then run again." -ForegroundColor Yellow
    exit 1
}

$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    uv venv --python 3.12 .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$stamp = ".venv\.local-install"
$hash = (Get-FileHash pyproject.toml).Hash + (Get-FileHash requirements\agent.lock).Hash
if ($Reinstall -or -not (Test-Path $stamp) -or (Get-Content $stamp) -ne $hash) {
    Write-Host "Installing dependencies..." -ForegroundColor Cyan
    # agent.lock pins LiveKit to the version the test environment runs.
    uv pip install --python $py -e ".[orchestrator,agent,token-service,mock]" -c requirements\agent.lock
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Set-Content $stamp $hash
}

$runArgs = @("scripts\local\run.py")
if ($NoBrowser) { $runArgs += "--no-browser" }
& $py @runArgs
exit $LASTEXITCODE
