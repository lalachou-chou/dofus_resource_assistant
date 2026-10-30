$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

# The final installer is self-contained with respect to Python and Tesseract.
# Python is downloaded on the developer/build PC; Tesseract portable is supplied locally.
$RuntimeDir = Join-Path $Root 'runtime'
$PythonVersion = '3.12.10'
$PythonInstaller = "python-$PythonVersion-amd64.exe"
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/$PythonInstaller"
$TesseractPortableDir = Join-Path $RuntimeDir 'tesseract-portable'

function Download-IfMissing {
    param(
        [Parameter(Mandatory=$true)][string]$Url,
        [Parameter(Mandatory=$true)][string]$Destination
    )
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        Write-Host "Runtime deja present : $([IO.Path]::GetFileName($Destination))" -ForegroundColor Green
        return
    }

    Write-Host "Telechargement : $Url" -ForegroundColor Yellow
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    } catch {}
    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        throw "Telechargement incomplet : $Destination"
    }
}

function Prepare-Runtimes {
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    Download-IfMissing $PythonUrl (Join-Path $RuntimeDir $PythonInstaller)

    $tesseractExe = Join-Path $TesseractPortableDir 'tesseract.exe'
    $tessdataDir = Join-Path $TesseractPortableDir 'tessdata'
    if (-not (Test-Path -LiteralPath $tesseractExe -PathType Leaf)) {
        throw "Tesseract portable introuvable : $tesseractExe"
    }
    if (-not (Test-Path -LiteralPath $tessdataDir -PathType Container)) {
        throw "Dossier tessdata Tesseract introuvable : $tessdataDir"
    }
    Write-Host "Tesseract portable valide : $TesseractPortableDir" -ForegroundColor Green
}

function Find-ISCC {
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\ISCC.exe"
    )

    foreach ($p in $candidates) {
        if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { return $p }
    }

    $roots = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links",
        "$env:LOCALAPPDATA\Programs",
        "$env:ProgramFiles\Inno Setup 7",
        "${env:ProgramFiles(x86)}\Inno Setup 7"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

    foreach ($root in $roots) {
        try {
            $hit = Get-ChildItem -LiteralPath $root -Filter ISCC.exe -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($hit) { return $hit.FullName }
        } catch {}
    }
    return $null
}

Prepare-Runtimes

$issc = Find-ISCC
if (-not $issc) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'Inno Setup est requis pour compiler le programme d installation. Installez Inno Setup 7 puis relancez le script. WinGet n est utilise ici que sur le PC de developpement, jamais sur les PC utilisateurs.'
    }

    Write-Host 'Inno Setup 7 est absent. Installation via WinGet sur le PC de developpement...' -ForegroundColor Yellow
    & winget install --id JRSoftware.InnoSetup.7 --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw 'Impossible d installer Inno Setup.' }

    for ($i = 0; $i -lt 10 -and -not $issc; $i++) {
        Start-Sleep -Seconds 2
        $issc = Find-ISCC
    }
}

if (-not $issc) {
    throw 'Inno Setup a ete installe mais ISCC.exe reste introuvable. Fermez puis rouvrez PowerShell et relancez ce script.'
}

Write-Host "ISCC trouve : $issc" -ForegroundColor Green
Write-Host 'Compilation du setup 1.0.7...' -ForegroundColor Cyan
& $issc (Join-Path $Root 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'La compilation de l installateur a echoue.' }

$out = Join-Path $Root 'dist'
Write-Host "Installateur cree dans : $out" -ForegroundColor Green
Get-ChildItem -LiteralPath $out -Filter '*.exe' | Select-Object Name, Length
