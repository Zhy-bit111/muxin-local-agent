# Setup venv and install dependencies for desk-companion
# Usage: powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PythonExe = "E:\Python313\python.exe"
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Output "ERROR: python not found at $PythonExe"
    exit 1
}
Write-Output "[1/6] base python: $PythonExe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Output "[2/6] creating venv at $Root\.venv"
    & $PythonExe -m venv (Join-Path $Root ".venv")
} else {
    Write-Output "[2/6] venv already exists"
}

Write-Output "[3/6] upgrading pip"
& $VenvPython -m pip install --upgrade pip setuptools wheel

Write-Output "[4/6] installing torch (cuda 12.4)"
& $VenvPython -m pip install torch --index-url https://download.pytorch.org/whl/cu124
if ($LASTEXITCODE -ne 0) {
    Write-Output "cu124 failed, try cu126"
    & $VenvPython -m pip install torch --index-url https://download.pytorch.org/whl/cu126
}
if ($LASTEXITCODE -ne 0) {
    Write-Output "cu126 failed, try default PyPI (cpu build, link test only)"
    & $VenvPython -m pip install torch
}

Write-Output "[5/6] installing requirements"
& $VenvPython -m pip install -r (Join-Path $Root "requirements.txt")

# 踩过的坑：上一次 pip 整体跑完了，但 peft 实际没落盘，脚本却当作成功。
# 所以这里不信 pip 的退出码，按包逐个 probe，缺哪个补哪个。
Write-Output "[6/6] probing core packages"
$probe = "import importlib.util as u;mods=['transformers','peft','datasets','accelerate','modelscope','yaml','tqdm'];print('|'.join([m for m in mods if u.find_spec(m) is None]))"
$raw = (& $VenvPython -c $probe) | Select-Object -Last 1
$miss = @()
if ($raw) {
    $miss = @($raw.Trim() -split '|' | Where-Object { $_ -ne '' })
}
if ($miss.Count -gt 0) {
    foreach ($m in $miss) {
        $pipName = if ($m -eq 'yaml') { 'pyyaml' } else { $m }
        Write-Output ("  missing: " + $m + " -> pip install " + $pipName)
        & $VenvPython -m pip install $pipName
    }
    Write-Output ("[6/6] backfilled: " + ($miss -join ', '))
} else {
    Write-Output "[6/6] all core packages present, nothing to backfill"
}

Write-Output "---- verify ----"
& $VenvPython -c "import torch, transformers, peft, datasets; print('torch', torch.__version__); print('cuda_available', torch.cuda.is_available()); print('gpu', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none'); print('transformers', transformers.__version__); print('peft', peft.__version__)"
