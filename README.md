# Fullscreen Zoom

A lightweight Windows fullscreen magnifier that lives in the system tray.
Smoothly zooms and pans around your cursor without a console window.

## Controls

| Hotkey | Action |
|---|---|
| `Ctrl+Alt+Z` | Toggle zoom on/off (same key both directions) |
| `Ctrl+Alt+Q` | Quit the app completely |
| `Alt + Up` (held) | Zoom in — only while zoom is ON, otherwise passes through to the focused app as normal |
| `Alt + Down` (held) | Zoom out — same as above |

Right-click the tray icon for the same actions if you'd rather not use hotkeys.
The tray icon turns **green** while zoom is active and **gray** while idle.

## Running from source

```bash
pip install -r requirements.txt
python zoom_app.py
```

## Building a standalone .exe

```bash
build.bat
```

This produces `dist/FullscreenZoom/FullscreenZoom.exe` — a windowed
(no console) app in a folder alongside its dependencies. It does
**not** require administrator privileges — the keyboard hook and
Magnification API both work fine as a standard user. The one
limitation: if a window running *with* elevated privileges (an
elevated installer, Task Manager, etc.) has focus, a non-elevated
hook can't intercept keys typed into it — a rare edge case, not a
general problem.

The build deliberately avoids `--onefile` and UPX compression: a
single self-extracting, compressed exe is structurally similar to
how a lot of malware droppers are built, so unsigned single-file
PyInstaller executables get flagged by Windows SmartScreen /
antivirus far more often than a plain folder of files does — even
though the code itself is unchanged. If you still hit a SmartScreen
warning, click "More info" → "Run anyway" (for a personal machine),
or send it to your IT/security team for allowlisting (for a managed
work machine — please don't try to bypass a company security policy
yourself). To eliminate the warning entirely, the exe would need to
be digitally code-signed with a certificate, which costs money and
is really only worth it if you're distributing this more broadly.

To move or share the app, zip up the entire `dist/FullscreenZoom`
folder — every file in it is required, not just the `.exe`.

## Running in the background automatically

- **Manual**: just launch `FullscreenZoom.exe` — it minimizes straight
  to the tray, no window ever appears, no admin prompt.
- **Start at login**: press `Win+R`, run `shell:startup`, and drop a
  shortcut to `FullscreenZoom.exe` in that folder. Windows will launch
  it silently every time you sign in.

## Notes

- Only one copy can run at a time — launching a second instance exits
  immediately instead of double-hooking the keyboard.
- Zoom range, zoom speed, and smoothing responsiveness are all
  adjustable constants near the top of `zoom_app.py`.

## License

MIT — I guess.