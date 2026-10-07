# =====================================================================
#  Prepara e compila o instalador .exe do EspelhoHex Simulador
#  1. baixa o Python embutível e a biblioteca 3D para a pasta "build"
#  2. chama o compilador do Inno Setup (ISCC.exe)
#  Resultado: saida\EspelhoHex-Simulador-Setup-<versão>.exe
# =====================================================================
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$Root  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Build = Join-Path $Root 'build'
$PyVer = '3.12.7'
$PyUrl = "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip"
$Vendor = [ordered]@{
  'three.min.js'     = 'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js'
  'OrbitControls.js' = 'https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js'
  'STLExporter.js'   = 'https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/exporters/STLExporter.js'
  'OBJExporter.js'   = 'https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/exporters/OBJExporter.js'
  'jszip.min.js'     = 'https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js'
}

function Step($t) { Write-Host ''; Write-Host "> $t" -ForegroundColor Cyan }

function Find-ISCC {
  $c = Get-Command ISCC.exe -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA 'Programs'))) {
    if (-not $base) { continue }
    $p = Join-Path $base 'Inno Setup 6\ISCC.exe'
    if (Test-Path $p) { return $p }
  }
  return $null
}

try {
  $iscc = Find-ISCC
  if (-not $iscc) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
      Step 'Instalando o Inno Setup (gratuito) pelo winget'
      winget install --id JRSoftware.InnoSetup -e --accept-package-agreements --accept-source-agreements
      $iscc = Find-ISCC
    }
    if (-not $iscc) { throw 'Não encontrei o Inno Setup 6. Instale em https://jrsoftware.org/isdl.php e rode de novo.' }
  }

  New-Item -ItemType Directory -Force -Path $Build, (Join-Path $Build 'vendor') | Out-Null

  $pyDir = Join-Path $Build 'python'
  if (-not (Test-Path (Join-Path $pyDir 'pythonw.exe'))) {
    Step "Baixando o Python $PyVer embutível"
    $zip = Join-Path $Build 'python.zip'
    Invoke-WebRequest -Uri $PyUrl -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $pyDir -Force
    Remove-Item $zip -Force
  }

  Step 'Baixando a biblioteca 3D'
  foreach ($name in $Vendor.Keys) {
    $out = Join-Path $Build "vendor\$name"
    if (-not (Test-Path $out)) { Invoke-WebRequest -Uri $Vendor[$name] -OutFile $out -UseBasicParsing }
    Write-Host "  ok  $name"
  }

  Step 'Compilando o instalador'
  & $iscc (Join-Path $Root 'EspelhoHex.iss')
  if ($LASTEXITCODE -ne 0) { throw "O Inno Setup terminou com erro ($LASTEXITCODE)." }

  $exe = Get-ChildItem (Join-Path $Root 'saida') -Filter '*.exe' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  Write-Host ''
  Write-Host "Pronto: $($exe.FullName)" -ForegroundColor Green
  if (-not $env:CI) { Start-Process explorer.exe -ArgumentList "/select,`"$($exe.FullName)`"" }
} catch {
  Write-Host ''
  Write-Host "Erro: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
