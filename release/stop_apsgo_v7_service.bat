@echo off
setlocal
set "APSGO_V7_TARGET_EXE=%~dp0APSGoV7Service.exe"
set "APSGO_V7_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "APSGO_V7_POWERSHELL=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%APSGO_V7_TARGET_EXE%" (
    echo Missing APSGo V7 executable: "%APSGO_V7_TARGET_EXE%" 1>&2
    exit /b 2
)
if not exist "%APSGO_V7_POWERSHELL%" (
    echo Missing 64-bit Windows PowerShell. 1>&2
    exit /b 2
)

"%APSGO_V7_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$target = [IO.Path]::GetFullPath($env:APSGO_V7_TARGET_EXE); $targets = @(Get-Process -Name 'APSGoV7Service' -ErrorAction SilentlyContinue | Where-Object { $_.Path -and [string]::Equals([IO.Path]::GetFullPath($_.Path), $target, [StringComparison]::OrdinalIgnoreCase) }); if ($targets.Count -eq 0) { Write-Host 'APSGo V7 service is not running for' $target; exit 0 }; foreach ($process in $targets) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue; $remaining = Get-Process -Id $process.Id -ErrorAction SilentlyContinue; if ($null -ne $remaining -and -not $remaining.WaitForExit(15000)) { throw ('Timed out stopping process ' + $process.Id) } }; exit 0"
set "APSGO_V7_EXIT_CODE=%ERRORLEVEL%"
exit /b %APSGO_V7_EXIT_CODE%
