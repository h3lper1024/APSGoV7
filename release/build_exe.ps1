[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ExpectedCondaEnvironment = "aps_3.10.18"
$ExpectedPythonVersion = "3.10.18"
$ApplicationName = "APSGoV7Service"
$PackageName = "APSGoV7"
$ReleaseDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ReleaseDirectory
$SourceDirectory = Join-Path $ProjectRoot "src"
$EntryScript = Join-Path $ReleaseDirectory "apsgo_v7_service_entry.py"
$Verifier = Join-Path $ReleaseDirectory "verify_release.py"
$SmokeRunner = Join-Path $ReleaseDirectory "smoke_release.py"
$ReleaseReadme = Join-Path $ReleaseDirectory "README.md"
$Requirements = Join-Path $ReleaseDirectory "requirements-build.txt"
$StartScriptTemplate = Join-Path $ReleaseDirectory "start_apsgo_v7_service.bat"
$StopScriptTemplate = Join-Path $ReleaseDirectory "stop_apsgo_v7_service.bat"
$SourceConfiguration = Join-Path $ProjectRoot "config\apsgo_v7_service.yaml"
$SourceDatabase = Join-Path $ProjectRoot "data\apsgo_v7_rules.sqlite3"
$BuildRoot = Join-Path $ReleaseDirectory "build"
$WorkDirectory = Join-Path $BuildRoot "pyinstaller_work"
$SpecDirectory = Join-Path $BuildRoot "pyinstaller_spec"
$PyInstallerDistDirectory = Join-Path $BuildRoot "pyinstaller_dist"
$ReleaseAssetsDirectory = Join-Path $BuildRoot "release_assets"
$StagedSeedDirectory = Join-Path $ReleaseAssetsDirectory "data"
$StagedSeedDatabase = Join-Path $StagedSeedDirectory "apsgo_v7_rules_seed.sqlite3"
$DistRoot = Join-Path $ReleaseDirectory "dist"
$BuiltDirectory = Join-Path $PyInstallerDistDirectory $ApplicationName
$PackageDirectory = Join-Path $DistRoot $PackageName
$PackageConfigurationDirectory = Join-Path $PackageDirectory "config"
$PackageDataDirectory = Join-Path $PackageDirectory "data"
$PackageConfigurationTemplate = Join-Path `
    $PackageConfigurationDirectory "apsgo_v7_service.example.yaml"
$PackageSeedDatabase = Join-Path $PackageDataDirectory "apsgo_v7_rules_seed.sqlite3"
$PackageStartScript = Join-Path $PackageDirectory "start_apsgo_v7_service.bat"
$PackageStopScript = Join-Path $PackageDirectory "stop_apsgo_v7_service.bat"
$PackageReadme = Join-Path $PackageDirectory "README.md"
$PackageManifest = Join-Path $PackageDirectory "release_manifest.json"

$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
$env:GIT_OPTIONAL_LOCKS = "0"

if ($CheckOnly -and $Clean) {
    throw "-CheckOnly and -Clean cannot be used together."
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "APSGo V7 must be packaged on Windows."
}
if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
    throw "APSGo V7 requires a 64-bit Windows build process."
}
foreach ($path in @(
    $SourceDirectory,
    $EntryScript,
    $Verifier,
    $SmokeRunner,
    $ReleaseReadme,
    $Requirements,
    $StartScriptTemplate,
    $StopScriptTemplate,
    $SourceConfiguration,
    $SourceDatabase
)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required build input is missing: $path"
    }
}

$RequirementLines = @(
    Get-Content -LiteralPath $Requirements |
        Where-Object { $_ -and -not $_.TrimStart().StartsWith("#") }
)
if ($RequirementLines.Count -ne 1 -or $RequirementLines[0] -notmatch '^PyInstaller==(.+)$') {
    throw "requirements-build.txt must contain exactly one pinned PyInstaller version."
}
$ExpectedPyInstallerVersion = $Matches[1]

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw "Conda was not found. Install it and create environment '$ExpectedCondaEnvironment'."
}
$CondaPrefix = (& conda run -n $ExpectedCondaEnvironment python -c "import sys; print(sys.prefix)") |
    Where-Object { $_ -and $_.Trim() } |
    Select-Object -Last 1
if ($LASTEXITCODE -ne 0 -or -not $CondaPrefix) {
    throw "Cannot resolve Conda environment '$ExpectedCondaEnvironment'."
}
$CondaPython = Join-Path $CondaPrefix "python.exe"
if (-not (Test-Path -LiteralPath $CondaPython -PathType Leaf)) {
    throw "Python executable is missing from Conda environment '$ExpectedCondaEnvironment'."
}

$RuntimeInspectionCode = @'
import json
import platform
import sys

print(json.dumps({
    "architecture": platform.machine(),
    "is_64_bit": sys.maxsize > 2**32,
    "version": ".".join(str(part) for part in sys.version_info[:3]),
}))
'@
$RuntimeJson = $RuntimeInspectionCode | & $CondaPython -
if ($LASTEXITCODE -ne 0) {
    throw "Cannot inspect Python in Conda environment '$ExpectedCondaEnvironment'."
}
$Runtime = $RuntimeJson | ConvertFrom-Json
if ($Runtime.version -ne $ExpectedPythonVersion) {
    throw "Python $ExpectedPythonVersion is required; found $($Runtime.version)."
}
if (-not $Runtime.is_64_bit -or $Runtime.architecture -notin @("AMD64", "x86_64")) {
    throw "The Conda Python must be Windows x64; found $($Runtime.architecture)."
}

$PyInstallerVersionCode = @'
from importlib.metadata import PackageNotFoundError, version

try:
    print(version("pyinstaller"))
except PackageNotFoundError:
    raise SystemExit(3)
'@
$ActualPyInstallerVersion = $PyInstallerVersionCode | & $CondaPython -
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: $CondaPython -m pip install -r $Requirements"
}
if ($ActualPyInstallerVersion.Trim() -ne $ExpectedPyInstallerVersion) {
    throw "PyInstaller $ExpectedPyInstallerVersion is required; found $ActualPyInstallerVersion."
}

$env:PYTHONPATH = $SourceDirectory
$SourceValidationJson = & $CondaPython $Verifier source --repository-root $ProjectRoot
$SourceValidationExitCode = $LASTEXITCODE
Write-Output $SourceValidationJson
if ($SourceValidationExitCode -ne 0) {
    throw "APSGo V7 source release validation failed with exit code $SourceValidationExitCode."
}
$SourceValidation = $SourceValidationJson | ConvertFrom-Json
if ($null -ne $SourceValidation.git.branch) {
    throw "Formal builds require a detached Git worktree; found branch '$($SourceValidation.git.branch)'."
}

if ($CheckOnly) {
    Write-Host "APSGo V7 release input check passed; no build files were written."
    exit 0
}

if (Test-Path -LiteralPath $DistRoot) {
    $distRootItem = Get-Item -LiteralPath $DistRoot -Force
    if (($distRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to build through a reparse point: $DistRoot"
    }
}

if ($Clean) {
    foreach ($path in @($BuildRoot, $PackageDirectory)) {
        if (Test-Path -LiteralPath $path) {
            $item = Get-Item -LiteralPath $path -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to clean a reparse point: $path"
            }
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }
}
elseif (
    (Test-Path -LiteralPath $BuildRoot) -or
    (Test-Path -LiteralPath $PackageDirectory)
) {
    throw "Build output already exists. Run build_exe.bat -Clean for a fresh build."
}

New-Item -ItemType Directory -Force `
    -Path $WorkDirectory, $SpecDirectory, $PyInstallerDistDirectory, `
          $StagedSeedDirectory, $DistRoot |
    Out-Null

$SeedBackupCode = @'
import json
import sys

from apsgo_v7_service.migrate_gqga4_grade_dictionary import backup_sqlite_database

result = backup_sqlite_database(
    sys.argv[1],
    sys.argv[2],
    timeout_seconds=float(sys.argv[3]),
)
print(json.dumps({
    "created": result.created,
    "integrity_check": result.integrity_check,
    "schema_version": result.user_version,
    "seed_sha256": result.database_sha256,
}, sort_keys=True, separators=(",", ":")))
'@
$DatabaseTimeoutSeconds = [Convert]::ToString(
    $SourceValidation.configuration.database_timeout_seconds,
    [Globalization.CultureInfo]::InvariantCulture
)
$SeedBackupJson = $SeedBackupCode | & $CondaPython - `
    $SourceDatabase $StagedSeedDatabase $DatabaseTimeoutSeconds
$SeedBackupExitCode = $LASTEXITCODE
Write-Output $SeedBackupJson
if ($SeedBackupExitCode -ne 0) {
    throw "APSGo V7 database seed creation failed with exit code $SeedBackupExitCode."
}

$SeedValidationJson = & $CondaPython $Verifier seed `
    --source-database $SourceDatabase `
    --seed-database $StagedSeedDatabase `
    --timeout-seconds $DatabaseTimeoutSeconds
$SeedValidationExitCode = $LASTEXITCODE
Write-Output $SeedValidationJson
if ($SeedValidationExitCode -ne 0) {
    throw "APSGo V7 database seed validation failed with exit code $SeedValidationExitCode."
}
$SeedValidation = $SeedValidationJson | ConvertFrom-Json
if ($SeedValidation.source.sha256 -ne $SourceValidation.database.sha256) {
    throw "The source rule database changed after release input validation."
}

$PyInstallerArguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--console",
    "--noupx",
    "--contents-directory", "_internal",
    "--name", $ApplicationName,
    "--distpath", $PyInstallerDistDirectory,
    "--workpath", $WorkDirectory,
    "--specpath", $SpecDirectory,
    "--paths", $SourceDirectory,
    "--collect-submodules", "uvicorn",
    "--exclude-module", "pytest",
    "--exclude-module", "tests",
    $EntryScript
)

Push-Location $ProjectRoot
try {
    & $CondaPython @PyInstallerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

$BuiltExecutable = Join-Path $BuiltDirectory "$ApplicationName.exe"
$BuiltInternal = Join-Path $BuiltDirectory "_internal"
if (
    -not (Test-Path -LiteralPath $BuiltExecutable -PathType Leaf) -or
    -not (Test-Path -LiteralPath $BuiltInternal -PathType Container)
) {
    throw "PyInstaller finished without the expected onedir output."
}
Move-Item -LiteralPath $BuiltDirectory -Destination $PackageDirectory

New-Item -ItemType Directory -Force `
    -Path $PackageConfigurationDirectory, $PackageDataDirectory |
    Out-Null
Copy-Item -LiteralPath $SourceConfiguration -Destination $PackageConfigurationTemplate
Move-Item -LiteralPath $StagedSeedDatabase -Destination $PackageSeedDatabase
Copy-Item -LiteralPath $StartScriptTemplate -Destination $PackageStartScript
Copy-Item -LiteralPath $StopScriptTemplate -Destination $PackageStopScript
Copy-Item -LiteralPath $ReleaseReadme -Destination $PackageReadme

$FinalSourceValidationJson = & $CondaPython $Verifier source --repository-root $ProjectRoot
$FinalSourceValidationExitCode = $LASTEXITCODE
Write-Output $FinalSourceValidationJson
if ($FinalSourceValidationExitCode -ne 0) {
    throw "APSGo V7 source changed during packaging."
}
$FinalSourceValidation = $FinalSourceValidationJson | ConvertFrom-Json
if (
    $FinalSourceValidation.git.commit -ne $SourceValidation.git.commit -or
    $FinalSourceValidation.configuration.sha256 -ne $SourceValidation.configuration.sha256 -or
    $FinalSourceValidation.database.sha256 -ne $SourceValidation.database.sha256
) {
    throw "APSGo V7 release input identity changed during packaging."
}

$PackagedSeedValidationJson = & $CondaPython $Verifier seed `
    --source-database $SourceDatabase `
    --seed-database $PackageSeedDatabase `
    --timeout-seconds $DatabaseTimeoutSeconds
$PackagedSeedValidationExitCode = $LASTEXITCODE
Write-Output $PackagedSeedValidationJson
if ($PackagedSeedValidationExitCode -ne 0) {
    throw "Packaged APSGo V7 database seed validation failed."
}

$BuiltAtUtc = [DateTime]::UtcNow.ToString(
    "yyyy-MM-dd'T'HH:mm:ss'Z'",
    [Globalization.CultureInfo]::InvariantCulture
)
$Utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)

function Write-ReleaseManifest {
    param([Parameter(Mandatory = $true)][string]$SmokeStatus)

    $ManifestOutput = @(& $CondaPython $Verifier manifest `
        --repository-root $ProjectRoot `
        --package-root $PackageDirectory `
        --built-at-utc $BuiltAtUtc `
        --smoke-status $SmokeStatus)
    $ManifestExitCode = $LASTEXITCODE
    if ($ManifestExitCode -ne 0 -or $ManifestOutput.Count -ne 1) {
        throw "APSGo V7 release manifest generation failed."
    }
    [IO.File]::WriteAllText(
        $PackageManifest,
        $ManifestOutput[0] + "`n",
        $Utf8WithoutBom
    )
}

Write-ReleaseManifest -SmokeStatus "pending"
$PendingPackageValidationJson = & $CondaPython $Verifier package `
    --package-root $PackageDirectory `
    --allow-pending-smoke
$PendingPackageValidationExitCode = $LASTEXITCODE
Write-Output $PendingPackageValidationJson
if ($PendingPackageValidationExitCode -ne 0) {
    throw "APSGo V7 pre-smoke package validation failed."
}

$SmokeJson = & $CondaPython $SmokeRunner `
    --repository-root $ProjectRoot `
    --package-root $PackageDirectory
$SmokeExitCode = $LASTEXITCODE
Write-Output $SmokeJson
if ($SmokeExitCode -ne 0) {
    throw "APSGo V7 package smoke failed."
}

Write-ReleaseManifest -SmokeStatus "pass"
$FinalPackageValidationJson = & $CondaPython $Verifier package `
    --package-root $PackageDirectory
$FinalPackageValidationExitCode = $LASTEXITCODE
Write-Output $FinalPackageValidationJson
if ($FinalPackageValidationExitCode -ne 0) {
    throw "APSGo V7 final package validation failed."
}

$PackagedExecutable = Join-Path $PackageDirectory "$ApplicationName.exe"
foreach ($path in @(
    $PackagedExecutable,
    $PackageConfigurationTemplate,
    $PackageSeedDatabase,
    $PackageStartScript,
    $PackageStopScript,
    $PackageReadme,
    $PackageManifest
)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "The packaged release input was not found: $path"
    }
}
Write-Host "APSGo V7 onedir build completed: $PackageDirectory"
