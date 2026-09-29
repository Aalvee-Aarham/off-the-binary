# Run the LLM server natively on Windows (no Docker).
# Downloads llama.cpp (CPU build), uv (Python manager) and the models on first run.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run-local.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\run-local.ps1 -Backend vulkan   # use the Radeon iGPU
param(
    [ValidateSet("cpu", "vulkan")] [string]$Backend = "cpu",
    [string]$LlamaBuild = "b11246",
    [int]$Port = 8100,               # 8000 is used by the fuel simulator
    [string]$ApiKey = ""             # empty = no auth (fine on localhost)
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root ".tools"
New-Item -ItemType Directory -Force $Tools | Out-Null

# llama.cpp binaries
$LlamaDir = Join-Path $Tools "llama-$Backend"
$LlamaExe = Join-Path $LlamaDir "llama-server.exe"
if (-not (Test-Path $LlamaExe)) {
    $zip = Join-Path $Tools "llama-$Backend.zip"
    $url = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaBuild/llama-$LlamaBuild-bin-win-$Backend-x64.zip"
    Write-Host "Downloading $url"
    curl.exe -fL --retry 5 -C - -o $zip $url
    Expand-Archive -Force $zip $LlamaDir
    Remove-Item $zip
}

# uv (manages Python 3.12 + venv)
$Uv = Join-Path $Tools "uv\uv.exe"
if (-not (Test-Path $Uv)) {
    $zip = Join-Path $Tools "uv.zip"
    curl.exe -fL --retry 5 -C - -o $zip "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
    Expand-Archive -Force $zip (Join-Path $Tools "uv")
    Remove-Item $zip
}

Push-Location $Root
try {
    $Py = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $Py)) {
        & $Uv venv --python 3.12 .venv
    }
    & $Uv pip install --python $Py -r requirements.txt
    & $Py scripts\download_models.py --dest models

    $env:LLAMA_SERVER_BIN = $LlamaExe
    $env:MODELS_DIR = Join-Path $Root "models"
    $env:LOG_DIR = Join-Path $Root "logs"
    $env:API_KEY = $ApiKey
    Write-Host ""
    Write-Host "LLM server starting on http://127.0.0.1:$Port" -ForegroundColor Cyan
    Write-Host "  Chatbot : http://127.0.0.1:$Port/chat"
    Write-Host "  Test UI : http://127.0.0.1:$Port/ui"
    Write-Host "  API docs: http://127.0.0.1:$Port/docs"
    Write-Host "  Stop    : Ctrl+C"
    Write-Host ""
    & $Py -m uvicorn app.main:app --host 127.0.0.1 --port $Port --no-access-log
}
finally {
    Pop-Location
}
