$ErrorActionPreference = "Stop"

$python = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }

& $python -m pip install --upgrade pip
& $python -m pip install -r requirements-dev.txt
& $python -m pytest -q
& $python -m PyInstaller --clean --noconfirm SnapToGMod.spec

Write-Host "Built: dist\SnapToGMod.exe"
