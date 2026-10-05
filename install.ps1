$ErrorActionPreference = 'Stop'
$python = $null
$candidates = @()
$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($launcher) {
    $candidate = & $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
    if ($LASTEXITCODE -eq 0) { $candidates += $candidate }
}
foreach ($name in @('python.exe', 'python3.exe')) {
    $candidate = Get-Command $name -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.Source -notlike '*WindowsApps*') { $candidates += $candidate.Source }
}
$localPrograms = Join-Path $env:LOCALAPPDATA 'Programs\Python'
if (Test-Path $localPrograms) {
    $candidates += Get-ChildItem $localPrograms -Filter python.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
}
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
}
if (-not $python) {
    throw 'Python 3.10+ not found. Install it from https://www.python.org/downloads/windows/ then run this file again.'
}
& $python --version
& $python (Join-Path $PSScriptRoot 'install.py')
if ($LASTEXITCODE -ne 0) { throw 'Installation did not complete. Review the error above.' }
