@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 (
    echo Cannot enter the APSGo V7 release directory. 1>&2
    exit /b 2
)
set "APSGO_V7_EXE=%~dp0APSGoV7Service.exe"
set "APSGO_V7_CONFIG_TEMPLATE=%~dp0config\apsgo_v7_service.example.yaml"
set "APSGO_V7_CONFIG=%~dp0config\apsgo_v7_service.yaml"
set "APSGO_V7_DATABASE_SEED=%~dp0data\apsgo_v7_rules_seed.sqlite3"
set "APSGO_V7_RUNTIME_DATABASE=%~dp0data\apsgo_v7_rules.sqlite3"
set "APSGO_V7_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "APSGO_V7_POWERSHELL=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%APSGO_V7_EXE%" (
    echo Missing APSGo V7 executable: "%APSGO_V7_EXE%" 1>&2
    exit /b 2
)
if not exist "%APSGO_V7_CONFIG_TEMPLATE%" (
    echo Missing APSGo V7 configuration template: "%APSGO_V7_CONFIG_TEMPLATE%" 1>&2
    exit /b 2
)
if not exist "%APSGO_V7_DATABASE_SEED%" (
    echo Missing APSGo V7 database seed: "%APSGO_V7_DATABASE_SEED%" 1>&2
    exit /b 2
)
if not exist "%APSGO_V7_POWERSHELL%" (
    echo Missing 64-bit Windows PowerShell. 1>&2
    exit /b 2
)

call :initialize_file "%APSGO_V7_CONFIG_TEMPLATE%" "%APSGO_V7_CONFIG%"
if errorlevel 1 (
    echo Failed to initialize APSGo V7 configuration. 1>&2
    exit /b 3
)

call :initialize_file "%APSGO_V7_DATABASE_SEED%" "%APSGO_V7_RUNTIME_DATABASE%"
if errorlevel 1 (
    echo Failed to initialize APSGo V7 rule database. 1>&2
    exit /b 3
)

"%APSGO_V7_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "try { $stream = [IO.File]::Open($env:APSGO_V7_RUNTIME_DATABASE, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite); $stream.Dispose(); exit 0 } catch { Write-Error $_; exit 1 }"
if errorlevel 1 (
    echo The APSGo V7 rule database is not writable. 1>&2
    exit /b 4
)

"%APSGO_V7_EXE%" --config "%APSGO_V7_CONFIG%"
set "APSGO_V7_EXIT_CODE=%ERRORLEVEL%"
exit /b %APSGO_V7_EXIT_CODE%

:initialize_file
set "APSGO_V7_COPY_SOURCE=%~1"
set "APSGO_V7_COPY_DESTINATION=%~2"
"%APSGO_V7_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$source = $env:APSGO_V7_COPY_SOURCE; $destination = $env:APSGO_V7_COPY_DESTINATION; if ([IO.File]::Exists($destination)) { exit 0 }; $temporary = $destination + '.initializing.' + [guid]::NewGuid().ToString('N'); try { [IO.File]::Copy($source, $temporary, $false); try { [IO.File]::Move($temporary, $destination) } catch [IO.IOException] { if (-not [IO.File]::Exists($destination)) { throw } } } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }"
exit /b %ERRORLEVEL%
