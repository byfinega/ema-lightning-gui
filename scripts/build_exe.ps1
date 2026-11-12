<#
.SYNOPSIS
    Builds the EMA Lightning Desktop GUI for Windows with PyInstaller.

.DESCRIPTION
    Creates a local build environment, installs a CPU PyTorch by default, and
    produces either dist\EMA-Lightning-Desktop-GUI\EMA-Lightning-Desktop-GUI.exe (folder build) or
    dist\EMA-Lightning-Desktop-GUI.exe (single file). Run it from PowerShell:

        powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1

    Python 3.11 or newer must be available as "python" in the current shell.
    Activate a suitable environment first, or pass -Python with a full path.

.PARAMETER OneFile
    Build one self-contained .exe. Startup is slower because PyInstaller
    unpacks the bundled runtime to a temporary folder on every launch.

.PARAMETER WithWeights
    Download ema.pt and decoder.pt into weights\ and bundle them, so the app
    works without an internet connection on first use. About 34 MB extra.

.PARAMETER Cuda
    Install the default CUDA-enabled PyTorch instead of the CPU build. The
    result is much larger and only works on machines with a compatible GPU.

.PARAMETER Python
    Python 3.11+ executable to use. Defaults to "python" from PATH.
#>
[CmdletBinding()]
param(
    [switch]$OneFile,
    [switch]$WithWeights,
    [switch]$Cuda,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    & $Python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.11 or newer is required. Activate such an environment, or pass -Python <full path>."
    }

    $Venv = Join-Path $Root ".venv-build"
    $VenvPython = Join-Path $Venv "Scripts\python.exe"
    if (-not (Test-Path $VenvPython)) {
        Write-Host "Creating the build environment in .venv-build ..."
        & $Python -m venv $Venv
        if ($LASTEXITCODE -ne 0) { throw "Could not create .venv-build." }
    }

    Write-Host "Installing PyTorch ..."
    if ($Cuda) {
        & $VenvPython -m pip install --disable-pip-version-check torch
    } else {
        & $VenvPython -m pip install --disable-pip-version-check torch --index-url https://download.pytorch.org/whl/cpu
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not install PyTorch." }

    Write-Host "Installing EMA Lightning and PyInstaller ..."
    & $VenvPython -m pip install --disable-pip-version-check -e ".[build]"
    if ($LASTEXITCODE -ne 0) { throw "Could not install the package." }

    if ($WithWeights) {
        Write-Host "Downloading model weights into weights\ ..."
        $Download = @'
from pathlib import Path
import shutil
from huggingface_hub import hf_hub_download

folder = Path("weights")
folder.mkdir(exist_ok=True)
for name in ("ema.pt", "decoder.pt"):
    shutil.copy2(hf_hub_download("canberkkkkkk/ema-lightning", name), folder / name)
print("weights ready:", folder.resolve())
'@
        $Download | & $VenvPython -
        if ($LASTEXITCODE -ne 0) { throw "Could not download the model weights." }
    }

    $env:EMA_ONEFILE = if ($OneFile) { "1" } else { "0" }
    Write-Host "Running PyInstaller ..."
    & $VenvPython -m PyInstaller --noconfirm --clean packaging/ema-lightning.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    if ($OneFile) {
        $Output = Join-Path $Root "dist\EMA-Lightning-Desktop-GUI.exe"
    } else {
        $Output = Join-Path $Root "dist\EMA-Lightning-Desktop-GUI\EMA-Lightning-Desktop-GUI.exe"
    }
    if (-not (Test-Path $Output)) { throw "The build finished but $Output is missing." }
    $Size = [math]::Round((Get-Item $Output).Length / 1MB, 1)
    Write-Host ""
    Write-Host "Ready: $Output ($Size MB)" -ForegroundColor Green
    if (-not $WithWeights) {
        Write-Host "The first launch downloads the model weights from Hugging Face."
    }
}
finally {
    Pop-Location
}


