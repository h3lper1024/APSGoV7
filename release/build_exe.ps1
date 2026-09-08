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
$Requirements = Join-Path $ReleaseDirectory "requirements-build.txt"
$BuildRoot = Join-Path $ReleaseDirectory "build"
$WorkDirectory = Join-Path $BuildRoot "pyinstaller_work"
$SpecDirectory = Join-Path $BuildRoot "pyinstaller_spec"
$PyInstallerDistDirectory = Join-Path $BuildRoot "pyinstaller_dist"
$DistRoot = Join-Path $ReleaseDirectory "dist"
$BuiltDirectory = Join-Path $PyInstallerDistDirectory $ApplicationName
$PackageDirectory = Join-Path $DistRoot $PackageName

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
foreach ($path in @($SourceDirectory, $EntryScript, $Verifier, $Requirements)) {
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

$RuntimeJson = & $CondaPython -c @'
import json
import platform
import sys

print(json.dumps({
    "architecture": platform.machine(),
    "is_64_bit": sys.maxsize > 2**32,
    "version": ".".join(str(part) for part in sys.version_info[:3]),
}))
'@
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

$ActualPyInstallerVersion = & $CondaPython -c @'
from importlib.metadata import PackageNotFoundError, version

try:
    print(version("pyinstaller"))
except PackageNotFoundError:
    raise SystemExit(3)
'@
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
    -Path $WorkDirectory, $SpecDirectory, $PyInstallerDistDirectory, $DistRoot |
    Out-Null
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

$PackagedExecutable = Join-Path $PackageDirectory "$ApplicationName.exe"
if (-not (Test-Path -LiteralPath $PackagedExecutable -PathType Leaf)) {
    throw "The packaged executable was not found: $PackagedExecutable"
}
Write-Host "APSGo V7 onedir build completed: $PackageDirectory"
