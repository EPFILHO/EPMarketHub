param(
    [string]$TargetRoot = "D:\EP\EPMarketHub",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$SourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$TargetRoot = (Resolve-Path -LiteralPath $TargetRoot).Path

if ($SourceRoot -eq $TargetRoot) {
    throw "Origem e destino da sincronização não podem ser a mesma pasta."
}
if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot ".git"))) {
    throw "A origem não é o clone de desenvolvimento esperado: $SourceRoot"
}
if (-not (Test-Path -LiteralPath (Join-Path $TargetRoot "app.py"))) {
    throw "O destino não parece ser uma instalação do EP Market Hub: $TargetRoot"
}

$protectedPaths = @(
    "MT5\terminal64.exe",
    "user_data\terminals.json",
    "user_data\symbols.json"
)

function Get-ProtectedHashes {
    param([string]$Root)

    $result = @{}
    foreach ($relative in $protectedPaths) {
        $path = Join-Path $Root $relative
        $result[$relative] = if (Test-Path -LiteralPath $path) {
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        } else {
            $null
        }
    }
    return $result
}

function Assert-ProtectedHashes {
    param(
        [hashtable]$Before,
        [hashtable]$After
    )

    foreach ($relative in $protectedPaths) {
        if ($Before[$relative] -ne $After[$relative]) {
            throw "Arquivo protegido foi alterado durante a sincronização: $relative"
        }
    }
}

try {
    $running = Get-CimInstance Win32_Process | Where-Object {
        $commandLine = [string]$_.CommandLine
        $executable = [string]$_.ExecutablePath
        ($executable -and $executable.StartsWith($TargetRoot, [StringComparison]::OrdinalIgnoreCase)) -or
        ($commandLine -and
            $commandLine.IndexOf($TargetRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $commandLine.IndexOf("app.py", [StringComparison]::OrdinalIgnoreCase) -ge 0)
    }
} catch {
    throw "Não foi possível confirmar se a instalação de teste está fechada: $($_.Exception.Message)"
}
if ($running) {
    $names = ($running | Select-Object -ExpandProperty Name -Unique) -join ", "
    throw "Feche o EP Market Hub e seus MT5 antes de sincronizar. Processos encontrados: $names"
}

$branchFiles = & git -C $SourceRoot diff --name-only main...HEAD
if ($LASTEXITCODE -ne 0) {
    throw "Não foi possível comparar a branch de desenvolvimento com main."
}
$workingFiles = & git -C $SourceRoot diff --name-only
$untrackedFiles = & git -C $SourceRoot ls-files --others --exclude-standard
$relativeFiles = @($branchFiles) + @($workingFiles) + @($untrackedFiles) |
    Where-Object { $_ } |
    ForEach-Object { $_.Replace("\", "/") } |
    Sort-Object -Unique

$forbidden = $relativeFiles | Where-Object {
    $_ -match '^(\.git|MT5|user_data|\.venv|venv|env)(/|$)' -or
    $_ -match '(^|/)(__pycache__|\.pytest_cache|\.ruff_cache)(/|$)' -or
    $_ -match '\.(exe|log|tmp|pyc|pyo)$'
}
if ($forbidden) {
    throw "A lista de alterações contém caminho protegido: $($forbidden -join ', ')"
}

$missing = $relativeFiles | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $SourceRoot $_) -PathType Leaf)
}
if ($missing) {
    throw "Há arquivo removido ou não regular na alteração. Revise manualmente: $($missing -join ', ')"
}

Write-Output "Origem: $SourceRoot"
Write-Output "Destino: $TargetRoot"
Write-Output "Arquivos seguros selecionados: $($relativeFiles.Count)"
$relativeFiles | ForEach-Object { Write-Output "  $_" }

if (-not $Apply) {
    Write-Output "Simulação concluída. Use -Apply para copiar esses arquivos."
    exit 0
}

$before = Get-ProtectedHashes -Root $TargetRoot
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path (Split-Path $TargetRoot -Parent) "EPMarketHub-runtime-backups\$timestamp"
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
foreach ($relative in $protectedPaths) {
    if ($relative -notlike "user_data\*.json") {
        continue
    }
    $source = Join-Path $TargetRoot $relative
    if (Test-Path -LiteralPath $source) {
        $backup = Join-Path $backupRoot $relative
        New-Item -ItemType Directory -Path (Split-Path $backup -Parent) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $backup -Force
    }
}

foreach ($relative in $relativeFiles) {
    $source = Join-Path $SourceRoot $relative
    $destination = Join-Path $TargetRoot $relative
    New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force

    $sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
    $destinationHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
    if ($sourceHash -ne $destinationHash) {
        throw "Falha ao verificar a cópia de: $relative"
    }
}

$after = Get-ProtectedHashes -Root $TargetRoot
Assert-ProtectedHashes -Before $before -After $after

Write-Output "Sincronização concluída sem alterar MT5, registros ou símbolos locais."
Write-Output "Backup preventivo: $backupRoot"
