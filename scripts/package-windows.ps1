param([switch]$SkipDriverBuild)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (-not [Environment]::Is64BitProcess) { throw 'Use 64-bit PowerShell and Python.' }
    if (-not $SkipDriverBuild) {
        & .\build.bat
        if ($LASTEXITCODE -ne 0) { throw 'Driver build failed.' }
    }
    $sdk = if ($env:XvsdkDir) { $env:XvsdkDir } else { 'C:/Program Files/xvsdk' }
    foreach ($required in @('Driver/x64/Release/driver_BSB-SLAM.dll', "$sdk/bin/xvsdk.dll")) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Required driver file missing: $required" }
    }
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create Python build environment.' }
    }
    $python = Join-Path $root '.venv/Scripts/python.exe'
    & $python -c 'import struct, tkinter; assert struct.calcsize(chr(80)) == 8'
    if ($LASTEXITCODE -ne 0) { throw 'The build needs 64-bit Python with Tcl/Tk installed.' }
    & $python -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    & $python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Python tests failed.' }
    cmake -S tests -B build/native-tests -A x64
    if ($LASTEXITCODE -ne 0) { throw 'Native test configuration failed.' }
    cmake --build build/native-tests --config Release --parallel 8
    if ($LASTEXITCODE -ne 0) { throw 'Native test build failed.' }
    ctest --test-dir build/native-tests -C Release --output-on-failure
    if ($LASTEXITCODE -ne 0) { throw 'Native tests failed.' }
    & $python scripts/release-files.py prepare
    if ($LASTEXITCODE -ne 0) { throw 'Release notices or version resource preparation failed.' }
    $version = & $python -c 'import release_version; print(release_version.VERSION)'
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the release version.' }
    & $python -m PyInstaller --noconfirm --windowed --onedir --contents-directory . --name BeyondSLAMControl --version-file dist/version-info.txt --manifest installer/app.manifest --icon assets/beyond-slam.ico --collect-all sv_ttk --add-data 'profiles;profiles' --add-data 'assets;assets' app.py
    if ($LASTEXITCODE -ne 0) { throw 'Desktop app packaging failed.' }
    $candidates = @('.tools/InnoSetup/ISCC.exe', "${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe", "$env:ProgramFiles/Inno Setup 6/ISCC.exe", "$env:ProgramFiles/Inno Setup 7/ISCC.exe", "${env:ProgramFiles(x86)}/Inno Setup 7/ISCC.exe")
    $iscc = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $iscc) { $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source }
    if (-not $iscc) { throw 'Install Inno Setup 6 or 7, then rerun this script.' }
    & $iscc "/DAppVersion=$version" installer/Beyond-SLAM.iss
    if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
    & $python scripts/release-files.py checksum
    if ($LASTEXITCODE -ne 0) { throw 'Installer checksum generation failed.' }
    Write-Host 'Installer ready: dist/Beyond-SLAM-Setup.exe'
} finally { Pop-Location }
