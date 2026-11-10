@echo off
REM Builds a windowed (no console) app. Does NOT request admin
REM elevation — testing showed the keyboard hook and Magnification
REM API both work fine as a standard user.
REM
REM Uses --onedir (a folder, not a single self-extracting exe) and
REM --noupx (no UPX compression). Both significantly reduce false-
REM positive flags from Windows SmartScreen / antivirus: a single
REM self-extracting, UPX-packed exe is structurally similar to how
REM a lot of malware droppers are built, so scanners flag it on
REM pattern alone even when the code is completely benign. A plain
REM folder of files with an uncompressed exe looks much more like
REM ordinary software.

pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo pip install failed - see errors above.
    pause
    exit /b 1
)

REM Using "python -m PyInstaller" instead of the bare "pyinstaller" command
REM avoids PATH issues where pip installs the package but the Scripts
REM folder containing pyinstaller.exe isn't on PATH.
python -m PyInstaller ^
    --onedir ^
    --noupx ^
    --noconsole ^
    --name FullscreenZoom ^
    --icon assets\icon_idle.ico ^
    --add-data "assets;assets" ^
    zoom_app.py

if errorlevel 1 (
    echo.
    echo Build FAILED - see errors above.
    pause
    exit /b 1
)

echo.
echo Build complete: dist\FullscreenZoom\FullscreenZoom.exe
echo Zip up the whole dist\FullscreenZoom folder to share/move it - all
echo the files in it are needed, not just the .exe.
pause