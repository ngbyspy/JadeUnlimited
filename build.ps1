param([string]$DistPath = 'dist')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    py -3.11 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
}
& $taskPython -m pip install -r requirements-lock.txt 'pyinstaller>=6,<7'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $taskPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
& $taskPython -m PyInstaller --noconfirm --clean --windowed --onedir --name JadeUnlimited --distpath $DistPath --add-data 'jade_unlimited/assets;jade_unlimited/assets' main.py
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
$taskDistribution = Join-Path (Join-Path $PSScriptRoot $DistPath) 'JadeUnlimited'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination (Join-Path $taskDistribution 'README.md') -Force
Write-Host "Ready: $taskDistribution\JadeUnlimited.exe (distribute the entire folder)"
