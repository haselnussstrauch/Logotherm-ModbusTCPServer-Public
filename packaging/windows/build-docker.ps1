<#
.SYNOPSIS
  Baut den Windows-Installer im Docker-Container und legt ihn unter release\ ab; optional als
  GitHub-Release veroeffentlichen.

.DESCRIPTION
  Der Installer wird lokal gebaut (packaging\windows\docker\Dockerfile, Linux-Container mit Wine).
  Er kommt NICHT ins Git-Repo (release\ ist ignoriert), sondern wird als GitHub-Release
  veroeffentlicht. Der Launcher sucht dort nach Updates (launcher\updater.py).

  Voraussetzung: Docker Desktop im Linux-Container-Modus. Fuer -Release zusaetzlich die GitHub-CLI
  (winget install GitHub.cli; einmal "gh auth login").

  Aufruf aus dem Repo-Root:
    powershell -ExecutionPolicy Bypass -File packaging\windows\build-docker.ps1             # nur bauen
    powershell -ExecutionPolicy Bypass -File packaging\windows\build-docker.ps1 -Release    # bauen + Release v<Version>
  Optionen: -NoCache (alles neu bauen), -Repo <besitzer/name> (Ziel-Repo des Releases)

  Vor jedem Release: Version in pyproject.toml hochzaehlen und in CHANGELOG.md einen Abschnitt
  "## <Version> - <Datum>" anlegen (wird zum Release-Text).
#>
[CmdletBinding()]
param(
    [switch]$Release,
    [switch]$NoCache,
    [string]$Repo = "haselnussstrauch/Logotherm-ModbusTCPServer-Public"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Out = Join-Path $Root "release"

Push-Location $Root
try {
    $version = (Select-String -Path pyproject.toml -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
    if (-not $version) { throw "Version in pyproject.toml nicht gefunden" }

    $notes = $null
    if ($Release) {
        if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
            throw "GitHub-CLI fehlt: winget install GitHub.cli, danach gh auth login"
        }
        # Release-Text aus CHANGELOG.md: Abschnitt "## <Version> ..." bis zum naechsten "## "
        $lines = Get-Content -Encoding UTF8 CHANGELOG.md
        $start = ($lines | Select-String -Pattern ("^## " + [regex]::Escape($version) + "\b")).LineNumber
        if (-not $start) { throw "CHANGELOG.md hat keinen Abschnitt fuer $version" }
        $body = @()
        foreach ($l in $lines[$start..($lines.Count - 1)]) { if ($l -match '^## ') { break }; $body += $l }
        $notes = ($body -join "`n").Trim()
        & gh release view "v$version" --repo $Repo *> $null
        if ($LASTEXITCODE -eq 0) { throw "Release v$version gibt es in $Repo schon - Version hochzaehlen" }
    }

    New-Item -ItemType Directory -Force $Out | Out-Null
    Get-ChildItem $Out -Filter "LogothermGateway-Setup-*.exe" | Remove-Item -Force

    $dockerArgs = @("build", "-f", "packaging/windows/docker/Dockerfile", "--target", "out",
              "--output", "type=local,dest=release", ".")
    if ($NoCache) { $dockerArgs = @("build", "--no-cache") + $dockerArgs[1..($dockerArgs.Count - 1)] }
    & docker @dockerArgs
    if ($LASTEXITCODE -ne 0) { throw "docker build fehlgeschlagen (Fehlercode $LASTEXITCODE)" }

    $exe = Get-ChildItem $Out -Filter "LogothermGateway-Setup-$version.exe" | Select-Object -First 1
    if (-not $exe) { throw "Kein Installer fuer Version $version in $Out" }
    $hash = (Get-FileHash $exe.FullName -Algorithm SHA256).Hash.ToLower()
    $sums = Join-Path $Out "SHA256SUMS.txt"
    "$hash  $($exe.Name)" | Set-Content -Encoding ascii $sums
    Write-Host ("Fertig: {0} ({1:N1} MB)  SHA256 {2}" -f $exe.FullName, ($exe.Length / 1MB), $hash)

    if ($Release) {
        $notesFile = Join-Path $Out "release-notes.md"
        [IO.File]::WriteAllText($notesFile, $notes + "`n`nSHA256 ``$hash``  $($exe.Name)", (New-Object Text.UTF8Encoding($false)))
        & gh release create "v$version" $exe.FullName $sums --repo $Repo --title "Logotherm Gateway $version" --notes-file $notesFile
        if ($LASTEXITCODE -ne 0) { throw "gh release create fehlgeschlagen" }
        Write-Host "Release v$version in $Repo veroeffentlicht."
    }
} finally {
    Pop-Location
}
