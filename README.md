# Better Magnifier

A lightweight Windows fullscreen magnifier that lives in the system tray.
Smoothly zooms around your cursor without a console window, from the
keyboard, the mouse wheel or a two-finger touchpad swipe — similar to
macOS's "use scroll gesture with modifier keys to zoom".

## Controls

| Input | Action |
|---|---|
| `Ctrl+Alt+Z` | Toggle zoom on/off (same key both directions) |
| `Ctrl+Alt+Q` | Quit the app completely |
| `Alt + Up` (held) | Zoom in. Turns zoom on automatically if it was off |
| `Alt + Down` (held) | Zoom out. Turns zoom off once you're back at 1×. When zoom is off, passes through to the focused app as normal |
| `Alt + scroll up` / `Alt + two fingers up` | Zoom in. Works with a mouse wheel or a Precision Touchpad. Turns zoom on automatically if it was off |
| `Alt + scroll down` / `Alt + two fingers down` | Zoom out. Turns zoom off once you're back at 1× |

Right-click the tray icon for "Toggle Zoom" / "Quit" if you'd rather not
use hotkeys. The tray icon turns **green** while zoom is active and
**gray** while idle.

Zoom is anchored to the cursor: whatever is under the pointer stays put
while you zoom, and moving the pointer pans across the whole screen.

Only **plain** Alt is used. With Ctrl, Shift or Win also held, arrow
keys and scrolling pass through untouched, so shortcuts like VS Code's
`Shift+Alt+Up` (copy line) and `Ctrl+Alt+Up` (add cursor) keep working.
Plain `Alt+Up` is always taken for zoom, so VS Code's "move line up"
needs a different key binding. Scrolling without Alt works normally,
and releasing Alt after zooming won't open the focused app's menu bar.

## How Alt + scroll works

- **Mouse wheel**: read with a low-level mouse hook. While Alt is held
  the wheel event is used for zoom and never reaches the app underneath.
- **Precision Touchpad**: Windows doesn't reliably pass touchpad swipes to
  mouse hooks (on a Surface they only arrive while zoom is already on),
  so the touchpad's finger positions are read directly
  (Windows Raw Input) and two fingers moving up/down while Alt is held
  become zoom. Raw Input can read the touchpad but not block it, so the
  page under the cursor also scrolls a little while you zoom.
- Older non-Precision touchpads that send ordinary wheel events work
  like a mouse wheel.

## Settings

Adjustable constants near the top of `zoom_app.py`:

| Setting | Default | What it does |
|---|---|---|
| `MIN_ZOOM` / `MAX_ZOOM` | `1.0` / `8.0` | Zoom range |
| `DEFAULT_ZOOM` | `2.0` | Zoom level `Ctrl+Alt+Z` turns on at |
| `ZOOM_SPEED` | `2.5` | Zoom units per second while holding `Alt+Up/Down` |
| `ZOOM_SMOOTHING` | `8.0` | Higher = snappier zoom easing |
| `SCROLL_ZOOM_PER_NOTCH` | `1.25` | Zoom multiplier per mouse-wheel notch |
| `TOUCHPAD_NOTCHES_PER_HEIGHT` | `6.0` | Touchpad speed: a swipe the full pad height counts as this many wheel notches (≈4×) |
| `SCROLL_ZOOM_INVERT` | `False` | Flip the Alt+scroll / swipe direction |
| `CENTER_ON_CURSOR` | `False` | `True` keeps the cursor centred on screen instead of anchoring zoom to it (the original behaviour) |
| `PAN_SMOOTHING` | `18.0` | Pan easing, only used when `CENTER_ON_CURSOR = True` |


## Running from source

```bash
pip install -r requirements.txt
python zoom_app.py
```

## Building a standalone .exe

```bash
pip install -r requirements.txt
python -m PyInstaller --onedir --noupx --noconsole --name FullscreenZoom --icon assets\icon_idle.ico --add-data "assets;assets" zoom_app.py
```

This produces `dist/FullscreenZoom/FullscreenZoom.exe` — a windowed
(no console) app in a folder alongside its dependencies. It does
**not** require administrator privileges — the keyboard and mouse
hooks, touchpad Raw Input and the
Magnification API all work as a standard user. The one limitation: if
a window running *with* elevated privileges (an elevated installer,
Task Manager, etc.) has focus or is under the cursor, a non-elevated
app can't intercept keys or mouse-wheel input aimed at it — a rare
edge case, not a general problem.

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
  immediately instead of double-hooking the keyboard and mouse.

## License

MIT
