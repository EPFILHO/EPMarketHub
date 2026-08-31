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

# pythonw.exe é um executável de subsistema GUI: o operador "&" do
# PowerShell não garante esperar por ele nem preencher $LASTEXITCODE de
# forma confiável — a GUI pode continuar aberta muito depois deste script
# "terminar". Start-Process -Wait -PassThru espera de verdade o processo
# encerrar e devolve o exit code real (0 só em sucesso real, ver
# tools/b3_contract_capture_gui.py), que o Agendador do Windows precisa
# para reportar corretamente se a captura teve sucesso.
$process = Start-Process -FilePath $Pythonw -ArgumentList @($Controller, "--scheduled") -Wait -PassThru
exit $process.ExitCode
