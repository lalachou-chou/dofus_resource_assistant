param(
    [string]$PythonDir = "$env:LOCALAPPDATA\Programs\Python312"
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Log = Join-Path $Root 'install-runtime.log'

$BundledPythonInstaller = Join-Path $Root 'runtime\python-3.12.10-amd64.exe'
$BundledTesseractDir = Join-Path $Root 'tesseract'
$BundledTesseractExe = Join-Path $BundledTesseractDir 'tesseract.exe'

function Write-Log([string]$Message) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Add-Content -LiteralPath $Log -Value $line -Encoding UTF8
    Write-Host $line
}

function Invoke-External {
    param(
        [Parameter(Mandatory=$true)][string]$File,
        [string[]]$Arguments = @(),
        [string]$Step = 'Commande'
    )
    Write-Log "$Step : $File $($Arguments -join ' ')"
    & $File @Arguments 2>&1 | Tee-Object -FilePath $Log -Append
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw "$Step a echoue (code $code). Voir $Log" }
}

function Find-Python {
    $candidatePaths = New-Object System.Collections.Generic.List[string]
    $seen = @{}

    function Add-PythonCandidate([string]$Path) {
        if ([string]::IsNullOrWhiteSpace($Path)) { return }
        try { $Path = [Environment]::ExpandEnvironmentVariables($Path).Trim('"') } catch { return }
        if (-not [IO.Path]::IsPathRooted($Path)) { return }
        if (-not $Path.ToLowerInvariant().EndsWith('.exe')) { return }
        $key = $Path.ToLowerInvariant()
        if (-not $seen.ContainsKey($key)) {
            $seen[$key] = $true
            [void]$candidatePaths.Add($Path)
        }
    }

    if ($PythonDir) { Add-PythonCandidate (Join-Path $PythonDir 'python.exe') }

    foreach ($name in @('python.exe','python3.exe','pythonw.exe')) {
        try {
            foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
                if ($cmd.Source) { Add-PythonCandidate $cmd.Source }
            }
        } catch {}
    }

    try {
        $py = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($py -and $py.Source) {
            foreach ($lineObj in @(& $py.Source -0p 2>$null)) {
                $line = ([string]$lineObj).Trim()
                if (-not $line) { continue }
                $m = [regex]::Match($line, '(?<path>[A-Za-z]:\\.*?python(?:w)?\.exe)\s*$')
                if ($m.Success) { Add-PythonCandidate $m.Groups['path'].Value }
            }
            foreach ($tag in @('-3.16','-3.15','-3.14','-3.13','-3.12')) {
                try {
                    $exe = [string](& $py.Source $tag -c 'import sys; print(sys.executable)' 2>$null)
                    Add-PythonCandidate $exe.Trim()
                } catch {}
            }
        }
    } catch {}

    $roots = @(
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        $env:ProgramW6432,
        $env:LOCALAPPDATA,
        $env:APPDATA,
        $env:USERPROFILE
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) } | Select-Object -Unique

    $relativeRoots = @(
        'Python',
        'Programs\Python',
        'Programs',
        'AppData\Local\Python',
        'AppData\Local\Programs\Python'
    )

    foreach ($root in $roots) {
        foreach ($rel in $relativeRoots) {
            $base = Join-Path $root $rel
            if (-not (Test-Path -LiteralPath $base -PathType Container)) { continue }
            try {
                foreach ($item in @(Get-ChildItem -LiteralPath $base -File -Filter 'python*.exe' -ErrorAction SilentlyContinue)) {
                    Add-PythonCandidate $item.FullName
                }
                foreach ($dir in @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue)) {
                    foreach ($exeName in @('python.exe','pythonw.exe')) {
                        $candidate = Join-Path $dir.FullName $exeName
                        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                            Add-PythonCandidate $candidate
                        }
                    }
                }
            } catch {}
        }
    }

    foreach ($base in @(
        (Join-Path $env:LOCALAPPDATA 'Python'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python')
    )) {
        if (-not $base -or -not (Test-Path -LiteralPath $base -PathType Container)) { continue }
        try {
            foreach ($dir in @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue)) {
                Add-PythonCandidate (Join-Path $dir.FullName 'python.exe')
                Add-PythonCandidate (Join-Path $dir.FullName 'pythonw.exe')
            }
        } catch {}
    }

    foreach ($rr in @(
        'HKCU:\Software\Python\PythonCore',
        'HKLM:\Software\Python\PythonCore',
        'HKLM:\Software\WOW6432Node\Python\PythonCore'
    )) {
        if (Test-Path $rr) {
            try {
                foreach ($key in @(Get-ChildItem $rr -ErrorAction SilentlyContinue)) {
                    $ip = (Get-ItemProperty $key.PSPath -Name InstallPath -ErrorAction SilentlyContinue).InstallPath
                    if ($ip) { Add-PythonCandidate (Join-Path ([string]$ip) 'python.exe') }
                }
            } catch {}
        }
    }

    foreach ($key in @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\python.exe',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\App Paths\python.exe',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\python.exe'
    )) {
        try {
            $v = (Get-ItemProperty $key -ErrorAction Stop).'(default)'
            if ($v) { Add-PythonCandidate ([string]$v) }
        } catch {}
    }

    $found = @()
    foreach ($cmd in $candidatePaths) {
        if (-not (Test-Path -LiteralPath $cmd -PathType Leaf)) { continue }
        try {
            $versionOutput = @(& $cmd --version 2>&1)
            $versionText = (($versionOutput -join " ").Trim())
            $match = [regex]::Match($versionText, 'Python\s+(?<version>\d+\.\d+\.\d+)')
            $version = $null
            if ($match.Success -and [version]::TryParse($match.Groups['version'].Value, [ref]$version) -and $version.Major -eq 3 -and $version.Minor -ge 12) {
                Write-Log "Python compatible detecte : $cmd (version $($version.ToString()))"
                $found += [pscustomobject]@{ Path = $cmd; Version = $version }
            } elseif ($match.Success) {
                Write-Log "Candidat Python ignore : $cmd (version detectee: $($match.Groups['version'].Value))"
            } else {
                Write-Log "Impossible de verifier Python : $cmd (sortie: $versionText)"
            }
        } catch {
            Write-Log "Impossible de verifier Python : $cmd ($($_.Exception.Message))"
        }
    }

    if ($found.Count -eq 0) { return $null }
    $selected = $found | Sort-Object Version -Descending | Select-Object -First 1
    Write-Log "Python selectionne : $($selected.Path) (version $($selected.Version))"
    return @($selected.Path, '')
}

function Find-Tesseract {
    $paths = @(
        $BundledTesseractExe,
        "$env:ProgramFiles\Tesseract-OCR\tesseract.exe",
        "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe",
        "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tesseract.exe",
        "$env:LOCALAPPDATA\Tesseract-OCR\tesseract.exe",
        "$env:ProgramW6432\Tesseract-OCR\tesseract.exe"
    ) | Where-Object { $_ }

    foreach ($p in $paths) {
        if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    }

    try { return (Get-Command tesseract.exe -ErrorAction Stop).Source } catch {}
    try {
        $where = & where.exe tesseract.exe 2>$null | Select-Object -First 1
        if ($where -and (Test-Path -LiteralPath $where -PathType Leaf)) { return $where }
    } catch {}
    return $null
}

function Install-BundledPython {
    if (-not (Test-Path -LiteralPath $BundledPythonInstaller -PathType Leaf)) {
        throw "Installateur Python embarque introuvable : $BundledPythonInstaller"
    }
    Write-Log 'Python 3.12 absent. Installation du runtime Python 3.12.10 embarque.'
    Invoke-External $BundledPythonInstaller @(
        '/quiet',
        'InstallAllUsers=0',
        'PrependPath=0',
        'Include_launcher=0',
        'Include_test=0',
        'Include_pip=1',
        "TargetDir=$PythonDir"
    ) 'Installation de Python 3.12.10'
}

function Validate-BundledTesseract {
    if (-not (Test-Path -LiteralPath $BundledTesseractExe -PathType Leaf)) {
        throw "Tesseract portable embarque introuvable : $BundledTesseractExe"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $BundledTesseractDir 'tessdata') -PathType Container)) {
        throw "Dossier tessdata introuvable : $(Join-Path $BundledTesseractDir 'tessdata')"
    }
    Write-Log "Tesseract portable embarque : $BundledTesseractExe"
}

try {
    Remove-Item $Log -Force -ErrorAction SilentlyContinue
    Write-Log '=== Dofus Resource Assistant 1.0.7 : installation du runtime ==='
    Write-Log "OS: $([Environment]::OSVersion.VersionString)"
    Write-Log "PowerShell: $($PSVersionTable.PSVersion)"

    $python = Find-Python
    if (-not $python) {
        Install-BundledPython
        $python = Find-Python
        $installedPython = Join-Path $PythonDir 'python.exe'
        if (-not $python -and (Test-Path -LiteralPath $installedPython -PathType Leaf)) {
            try {
                $v = & $installedPython -c "import sys; print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
                Write-Log "Python trouve directement apres installation : $installedPython (version $v)"
                $python = @($installedPython, '')
            } catch {}
        }
        if (-not $python) { throw "Python 3.12.10 a ete installe dans $PythonDir mais reste introuvable." }
    }

    $pyExe = $python[0]
    $pyArgs = @(); if ($python[1]) { $pyArgs += $python[1] }
    Write-Log "Python selectionne : $pyExe $($pyArgs -join ' ')"

    $venv = Join-Path $Root '.venv'
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Invoke-External $pyExe ($pyArgs + @('-m','venv',$venv)) 'Creation de l environnement Python'
    }

    Invoke-External $venvPython @('-m','pip','install','--disable-pip-version-check','--upgrade','pip') 'Mise a jour de pip'
    Invoke-External $venvPython @('-m','pip','install','--disable-pip-version-check','-r',(Join-Path $Root 'requirements.txt')) 'Installation des dependances Python'
    Invoke-External $venvPython @('-c','import mss; from PIL import Image; import pytesseract; import pystray') 'Test des dependances Python'

    $tesseract = Find-Tesseract
    if (-not $tesseract) {
        Validate-BundledTesseract
        $tesseract = $BundledTesseractExe
    }

    Write-Log "Tesseract selectionne : $tesseract"
    Invoke-External $tesseract @('--version') 'Test de Tesseract OCR'

    Write-Log 'Runtime installe avec succes.'
    exit 0
}
catch {
    Write-Log "ERREUR : $($_.Exception.Message)"
    Write-Log '=== ECHEC DE L INSTALLATION DU RUNTIME ==='
    exit 1
}
