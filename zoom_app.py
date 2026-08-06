"""
Windows Fullscreen Zoom — background / tray version
-----------------------------------------------------

Controls
--------
Ctrl+Alt+Z   Toggle zoom ON/OFF   (single hotkey does both directions)
Ctrl+Alt+Q   Quit the app entirely (exits the process)

Hold (only intercepted while zoom is ON — otherwise passes through
normally to whatever app is focused, e.g. VS Code / browser):
    Alt + Up      Zoom in
    Alt + Down    Zoom out

Direction is detected by polling key state every frame rather than
reacting to individual key-press events, which is more reliable for
a globally-suppressed key combo.

Runs with no console window and lives in the system tray. Right-click
the tray icon for "Toggle Zoom" / "Quit" as a backup to the hotkeys.
Icon turns green while zoom is active, gray while idle.

Requires (see requirements.txt):
    pip install keyboard pystray pillow

Build into a standalone .exe with build.bat (uses PyInstaller).
"""

import ctypes
import math
import os
import sys
import threading
import time

import keyboard
import pystray
from PIL import Image

# ============================================================
# Settings
# ============================================================

MIN_ZOOM = 1.0
MAX_ZOOM = 8.0
DEFAULT_ZOOM = 2.0

ZOOM_SPEED = 2.5          # zoom units per second while holding (target ramp)
UPDATE_HZ = 120

ZOOM_SMOOTHING = 8.0       # higher = snappier zoom easing
PAN_SMOOTHING = 18.0       # higher = snappier pan easing

APP_NAME = "FullscreenZoom"

# ============================================================
# Safe console output (no-op if there's no console, e.g. --noconsole build)
# ============================================================

def safe_print(*args, **kwargs):
    try:
        if sys.stdout is not None:
            print(*args, **kwargs)
    except Exception:
        pass


# ============================================================
# Resource path helper (works both as a script and as a PyInstaller exe)
# ============================================================

def resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


# ============================================================
# Single-instance guard (prevents duplicate keyboard hooks if the
# .exe is launched more than once, e.g. double-clicked twice)
# ============================================================

def acquire_single_instance_lock():
    # "Local\" rather than "Global\" — Global namespace mutexes can need
    # SeCreateGlobalPrivilege for non-admin users in some Terminal
    # Services/RDP contexts. Local is scoped to this session, which is
    # all a single-instance check needs anyway.
    mutex_name = f"Local\\{APP_NAME}_SingleInstanceMutex"
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    last_error = ctypes.windll.kernel32.GetLastError()
    ERROR_ALREADY_EXISTS = 183
    if last_error == ERROR_ALREADY_EXISTS:
        return None
    return handle


# ============================================================
# Windows API (Magnification)
# ============================================================

user32 = ctypes.WinDLL("user32", use_last_error=True)
mag = ctypes.WinDLL("magnification", use_last_error=True)

try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

# NOTE: MagInitialize() is intentionally NOT called here. The Windows
# Magnification API ties a fullscreen session to the thread that calls
# MagInitialize — every MagSetFullscreenTransform call after that must
# happen on that same thread, or it silently fails (returns 0, no
# exception). Since the actual transform calls happen in zoom_loop(),
# which runs on a background thread, MagInitialize is called there too
# (see zoom_loop below) rather than here on the main thread.


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


SCREEN_W = user32.GetSystemMetrics(0)
SCREEN_H = user32.GetSystemMetrics(1)


# ============================================================
# Zoom state
# ============================================================

class ZoomState:

    def __init__(self):
        self.active = False

        self.target_zoom = DEFAULT_ZOOM
        self.zoom = DEFAULT_ZOOM

        self.pan_left = 0.0
        self.pan_top = 0.0
        self._pan_initialized = False

        self.zoom_direction = 0
        self.stop = False

        self.lock = threading.Lock()

        # Set after the tray icon is created, so toggle()/quit() can
        # update the tray icon image and stop it.
        self.tray_icon = None
        self.icon_idle = None
        self.icon_active = None

    def toggle(self):
        with self.lock:
            self.active = not self.active
            safe_print(f"Zoom toggled -> {'ON' if self.active else 'OFF'}")

            if self.active:
                self._pan_initialized = False
            # NOTE: no direct MagSetFullscreenTransform call here — this
            # runs on the hotkey callback thread, not the worker thread
            # that owns the magnification session. The worker thread
            # (zoom_loop) notices the state change and performs the
            # actual reset call itself, on the correct thread.

            self._refresh_tray_icon()

    def quit(self):
        with self.lock:
            self.stop = True
            self.active = False
        # Actual MagSetFullscreenTransform reset + MagUninitialize happen
        # in zoom_loop, on the thread that owns the magnification session,
        # right before that thread exits.

        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass

    def _refresh_tray_icon(self):
        if self.tray_icon is None:
            return
        try:
            self.tray_icon.icon = self.icon_active if self.active else self.icon_idle
            self.tray_icon.title = f"{APP_NAME} — {'ON' if self.active else 'off'}"
        except Exception:
            pass


state = ZoomState()

# ============================================================
# Key handling
# ============================================================
#
# Direction (zoom in/out) is NOT decided inside a key-press callback —
# that depends on exact event ordering inside the keyboard library's
# low-level hook, which turned out to be unreliable for a suppressed
# key. Instead, the render loop polls is_pressed() every frame (see
# zoom_loop below), which is simpler and self-corrects every ~8ms.
#
# The hook below has exactly one job: block Up/Down from reaching the
# OS (and therefore other apps) *only* while zoom is active AND Alt is
# held. Whenever zoom is off, or Alt isn't held, the key passes through
# completely normally (line move in VS Code, browser shortcuts, etc.).


def alt_arrow_suppressor(event):
    if state.active and keyboard.is_pressed("alt"):
        return False  # block: don't let this reach the OS/focused app
    return True        # allow through untouched


def register_hotkeys():
    keyboard.hook_key("up", alt_arrow_suppressor, suppress=True)
    keyboard.hook_key("down", alt_arrow_suppressor, suppress=True)

    keyboard.add_hotkey("ctrl+alt+z", state.toggle)   # single hotkey: on AND off
    keyboard.add_hotkey("ctrl+alt+q", state.quit)      # always fully exits


# ============================================================
# Main render loop (runs on a background thread)
# ============================================================

def zoom_loop():
    # MagInitialize must be called on this same thread — see the note
    # above where mag/user32 are loaded.
    if not mag.MagInitialize():
        err = ctypes.get_last_error()
        safe_print(f"ERROR: MagInitialize failed on worker thread. GetLastError={err}")
        state.stop = True
        if state.tray_icon is not None:
            try:
                state.tray_icon.stop()
            except Exception:
                pass
        return

    target_frame_time = 1.0 / UPDATE_HZ
    last_time = time.perf_counter()
    was_active = False

    while not state.stop:
        now = time.perf_counter()
        dt = now - last_time
        last_time = now

        sleep_time = target_frame_time - (time.perf_counter() - now)
        if sleep_time > 0:
            time.sleep(sleep_time)

        if not state.active:
            if was_active:
                # Zoom was just switched off — reset the transform here,
                # on the thread that actually owns the mag session.
                mag.MagSetFullscreenTransform(ctypes.c_float(1.0), 0, 0)
                was_active = False
            continue

        was_active = True

        alt_held = keyboard.is_pressed("alt")
        up_held = alt_held and keyboard.is_pressed("up")
        down_held = alt_held and keyboard.is_pressed("down")

        if up_held and not down_held:
            state.zoom_direction = 1
        elif down_held and not up_held:
            state.zoom_direction = -1
        else:
            state.zoom_direction = 0

        if state.zoom_direction != 0:
            state.target_zoom += state.zoom_direction * ZOOM_SPEED * dt
            state.target_zoom = max(MIN_ZOOM, min(MAX_ZOOM, state.target_zoom))

        zoom_alpha = 1.0 - math.exp(-ZOOM_SMOOTHING * dt)
        state.zoom += (state.target_zoom - state.zoom) * zoom_alpha

        x, y = cursor()
        visible_w = SCREEN_W / state.zoom
        visible_h = SCREEN_H / state.zoom

        target_left = x - visible_w / 2
        target_top = y - visible_h / 2
        target_left = max(0, min(target_left, SCREEN_W - visible_w))
        target_top = max(0, min(target_top, SCREEN_H - visible_h))

        if not state._pan_initialized:
            state.pan_left = target_left
            state.pan_top = target_top
            state._pan_initialized = True
        else:
            pan_alpha = 1.0 - math.exp(-PAN_SMOOTHING * dt)
            state.pan_left += (target_left - state.pan_left) * pan_alpha
            state.pan_top += (target_top - state.pan_top) * pan_alpha

        mag_ok = mag.MagSetFullscreenTransform(
            ctypes.c_float(state.zoom),
            int(state.pan_left),
            int(state.pan_top),
        )
        if not mag_ok and not getattr(state, "_logged_mag_failure", False):
            err = ctypes.get_last_error()
            safe_print(f"WARNING: MagSetFullscreenTransform returned failure (0). GetLastError={err}")
            state._logged_mag_failure = True

    mag.MagUninitialize()


# ============================================================
# Tray icon
# ============================================================

def build_tray_icon():
    icon_idle = Image.open(resource_path("assets/icon_idle.ico"))
    icon_active = Image.open(resource_path("assets/icon_active.ico"))

    state.icon_idle = icon_idle
    state.icon_active = icon_active

    def on_toggle(icon, item):
        state.toggle()

    def on_quit(icon, item):
        state.quit()

    menu = pystray.Menu(
        pystray.MenuItem("Toggle Zoom  (Ctrl+Alt+Z)", on_toggle),
        pystray.MenuItem("Quit  (Ctrl+Alt+Q)", on_quit),
    )

    icon = pystray.Icon(APP_NAME, icon_idle, f"{APP_NAME} — off", menu)
    state.tray_icon = icon
    return icon


# ============================================================
# Entry point
# ============================================================

def main():
    safe_print(f"{APP_NAME} starting...")

    lock_handle = acquire_single_instance_lock()
    if lock_handle is None:
        safe_print(f"{APP_NAME} is already running.")
        sys.exit(0)

    try:
        register_hotkeys()
    except Exception as e:
        safe_print(f"ERROR: failed to register hotkeys: {e!r}")
        safe_print("This usually means the process needs to run as Administrator.")
        sys.exit(1)

    safe_print("Hotkeys registered. Ctrl+Alt+Z to toggle, Ctrl+Alt+Q to quit.")

    tray = build_tray_icon()

    worker = threading.Thread(target=zoom_loop, daemon=True)
    worker.start()

    tray.run()  # blocks on the main thread until state.quit() calls tray.stop()


if __name__ == "__main__":
    main()