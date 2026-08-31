$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$Controller = Join-Path $ProjectRoot "tools\b3_contract_capture_gui.py"

if (-not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) {
    throw "Python da virtualenv não encontrado: $Pythonw"
}
if (-not (Test-Path -LiteralPath $Controller -PathType Leaf)) {
    throw "Controlador da captura B3 não encontrado: $Controller"
}

& $Pythonw $Controller --scheduled
exit $LASTEXITCODE
