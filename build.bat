name: Build Windows exe

on:
  push:
    tags:
      - "v*"
  workflow_dispatch: {}

jobs:
  build:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Build with PyInstaller
        run: >
          python -m PyInstaller
          --onedir
          --noupx
          --noconsole
          --name FullscreenZoom
          --icon assets\icon_idle.ico
          --add-data "assets;assets"
          zoom_app.py

      - name: Zip the build output
        run: Compress-Archive -Path dist\FullscreenZoom\* -DestinationPath FullscreenZoom-windows.zip

      - name: Upload build artifact
        uses: actions/upload-artifact@v4
        with:
          name: FullscreenZoom-windows
          path: FullscreenZoom-windows.zip

      - name: Attach to GitHub Release
        if: startsWith(github.ref, 'refs/tags/v')
        uses: softprops/action-gh-release@v2
        with:
          files: FullscreenZoom-windows.zip