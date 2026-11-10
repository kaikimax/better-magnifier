"""
Windows Fullscreen Zoom — background / tray version
-----------------------------------------------------

Controls
--------
Alt+Z        Toggle zoom ON/OFF   (single hotkey does both directions)
Alt+R        Reset to 100% while staying in zoom mode
Alt+Q        Quit the app entirely (exits the process)

All three are chosen to be one-handed: hold Alt with your thumb/pinky
and tap Z, R, or Q with a nearby finger — no need to also reach Ctrl.

Hold (only intercepted while zoom is ON — otherwise passes through
normally to whatever app is focused, e.g. VS Code / browser):
    Alt + Up          Zoom in
    Alt + Down        Zoom out
    Alt + Scroll up    Zoom in  (two-finger scroll on a touchpad, or a mouse wheel)
    Alt + Scroll down  Zoom out

Note on touchpad "pinch to zoom": Windows does not expose raw pinch
gesture data globally (it's only delivered to whichever window is
directly under your fingers, via WM_POINTER, not through a system-wide
hook the way keyboard/mouse-wheel input is). So this app can't react
to a literal pinch gesture no matter what app has focus. Alt+Scroll
(a manual two-finger scroll while holding Alt) is the closest
equivalent that actually works globally.

Direction is detected by polling key state every frame rather than
reacting to individual key-press events, which is more reliable for
a globally-suppressed key combo.

Turning zoom ON always starts at 100% (no auto zoom-in), and turning
it OFF eases smoothly back to 100% before actually deactivating,
rather than snapping instantly — the snap was what caused a visible
pan glitch in both directions.

Note: this only works on windows that are NOT running elevated (e.g.
Task Manager) unless this app itself is also run elevated — Windows
blocks lower-privilege input hooks from reaching higher-privilege
windows (UIPI). See README for details.

Runs with no console window and lives in the system tray. Right-click
the tray icon for "Toggle Zoom" / "Quit" as a backup to the hotkeys.
Icon turns green while zoom is active, gray while idle.

Requires (see requirements.txt):
    pip install keyboard mouse pystray pillow

Build into a standalone .exe with build.bat (uses PyInstaller).
"""

import ctypes
import math
import os
import sys
import threading
import time

import keyboard
import mouse
import pystray
from PIL import Image

# ============================================================
# Settings
# ============================================================

MIN_ZOOM = 1.0
MAX_ZOOM = 8.0

ZOOM_SPEED = 2.5          # zoom units per second while holding (target ramp)
SCROLL_ZOOM_STEP = 0.5     # zoom change per scroll notch (Alt+Scroll)
UPDATE_HZ = 120

ZOOM_SMOOTHING = 8.0       # higher = snappier zoom easing
PAN_SMOOTHING = 18.0       # higher = snappier pan easing

# Fraction of the visible (zoomed) area, from each edge, that acts as
# a "dead zone" — the cursor can move freely inside it with no pan.
# Panning only kicks in once the cursor crosses into this margin near
# an edge, and only pans enough to keep the cursor just inside it.
# 0.2 = middle 60% of the view is dead zone, outer 20% on each side
# triggers panning. Higher = smaller dead zone = pans sooner/more.
EDGE_PAN_MARGIN = 0.2

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


def screen_size():
    # Read fresh every time, never cached — a resolution change while
    # the app is running (or launching at a different resolution than
    # whatever was last used) was leaving stale dimensions in place,
    # which is exactly what caused zoom to only cover the top-left
    # portion of the screen at non-1920x1080 resolutions. This is a
    # cheap call, safe to do every frame.
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


# ============================================================
# Zoom state
# ============================================================

class ZoomState:

    def __init__(self):
        self.active = False
        self._deactivating = False

        self.target_zoom = MIN_ZOOM
        self.zoom = MIN_ZOOM

        self.pan_left = 0.0
        self.pan_top = 0.0
        self._pan_initialized = False

        self.zoom_direction = 0
        self.stop = False

        self._alt_menu_guard_sent = False

        # Dirty flags for work that must NOT run directly on the hook
        # thread (see note above alt_arrow_suppressor): the worker
        # thread polls these every frame (~120Hz, so effectively
        # instant) and does the actual pystray/API calls itself.
        self._icon_dirty = False

        self.lock = threading.Lock()

        # Set after the tray icon is created, so toggle()/quit() can
        # update the tray icon image and stop it.
        self.tray_icon = None
        self.icon_idle = None
        self.icon_active = None

    def toggle(self):
        try:
            with self.lock:
                if self.active and not self._deactivating:
                    # Turning off: don't snap the transform back to
                    # normal — ease the zoom back down to 1x first
                    # (zoom_loop handles this), THEN actually deactivate.
                    # Snapping instantly was what caused the "pans the
                    # opposite way" jump on unzoom.
                    self._deactivating = True
                    self.target_zoom = MIN_ZOOM
                    safe_print("Zoom easing out -> OFF")

                elif not self.active:
                    # Turning on: always start from a clean, unzoomed
                    # state. Previously this carried over whatever zoom
                    # level was last used, which caused an instant
                    # jump/pan the moment zoom activated. Starting at 1x
                    # means activation is visually a no-op — zoom only
                    # happens once Alt+Up is actually held.
                    self.active = True
                    self.target_zoom = MIN_ZOOM
                    self.zoom = MIN_ZOOM
                    self._pan_initialized = False
                    safe_print("Zoom toggled -> ON")

                self._icon_dirty = True
        except Exception as e:
            safe_print(f"ERROR in toggle(): {e!r}")

    def reset_zoom(self):
        # Return to 100% while staying in zoom mode (cursor-following
        # stays active, Alt+Up still zooms back in). Distinct from
        # toggle(), which fully exits zoom mode.
        try:
            with self.lock:
                if not self.active or self._deactivating:
                    return
                self.target_zoom = MIN_ZOOM
                safe_print("Zoom reset -> 100%")
        except Exception as e:
            safe_print(f"ERROR in reset_zoom(): {e!r}")

    def quit(self):
        with self.lock:
            self.stop = True
            self.active = False
            self._deactivating = False
        # tray_icon.stop() and the actual MagSetFullscreenTransform
        # reset + MagUninitialize all happen on the worker thread (see
        # zoom_loop) — never called directly from here, since this runs
        # on the same hook thread as every hotkey and any blocking call
        # here risks Windows silently killing the whole hook.

    def _refresh_tray_icon(self):
        # Only ever call this from the worker thread, not from a hook
        # callback — see note above alt_arrow_suppressor.
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
#
# One side effect of suppressing Up/Down: many Windows apps treat a
# "solo" Alt press/release (with no other key in between, from the
# app's point of view) as a request to focus/open the menu bar. Since
# we're blocking Up/Down from ever reaching the app, the app only ever
# sees Alt go down and come back up — which looks exactly like a solo
# Alt tap, so it opens the menu. To prevent that, the moment we block
# the first Up/Down of a given Alt-hold, we also send a harmless
# Ctrl tap. That gives the OS a "real" key it saw during the Alt hold,
# so it no longer looks like a solo tap. The flag is reset when Alt is
# released so it fires once per hold, not on every OS key-repeat.
#
# IMPORTANT: this callback runs directly inside Windows' low-level
# keyboard hook procedure, which is subject to a strict OS timeout
# (~300ms by default). If a hook callback ever blocks past that,
# Windows silently and PERMANENTLY uninstalls the entire hook — which
# would kill every hotkey at once (Alt+Z/R/Q included, since they
# share the same underlying hook), matching exactly the "hotkeys
# randomly stop working" symptom. keyboard.send() does a synchronous,
# blocking OS call, so it must never run directly in this callback —
# it's dispatched to a throwaway background thread instead, so this
# function itself returns almost immediately no matter what.


def _send_menu_guard_key():
    try:
        keyboard.send("ctrl")
    except Exception:
        pass


def alt_arrow_suppressor(event):
    try:
        if state.active and keyboard.is_pressed("alt"):
            if not state._alt_menu_guard_sent:
                state._alt_menu_guard_sent = True
                threading.Thread(target=_send_menu_guard_key, daemon=True).start()
            return False  # block: don't let this reach the OS/focused app
        return True        # allow through untouched
    except Exception as e:
        safe_print(f"ERROR in alt_arrow_suppressor: {e!r}")
        return True  # fail open — never risk leaving keys stuck suppressed


def _reset_alt_menu_guard(event):
    state._alt_menu_guard_sent = False
    return True  # never suppress Alt itself


def alt_scroll_handler(event):
    try:
        # mouse.hook() receives ALL mouse events (moves, clicks, wheel) —
        # only act on wheel events.
        if not isinstance(event, mouse.WheelEvent):
            return True

        if not (state.active and keyboard.is_pressed("alt")):
            return True  # not our concern — let it through untouched

        direction = 1 if event.delta > 0 else -1
        state.target_zoom += direction * SCROLL_ZOOM_STEP
        state.target_zoom = max(MIN_ZOOM, min(MAX_ZOOM, state.target_zoom))

        return False  # attempt to suppress — see note in register_hotkeys()
    except Exception as e:
        safe_print(f"ERROR in alt_scroll_handler: {e!r}")
        return True


def register_hotkeys():
    keyboard.hook_key("up", alt_arrow_suppressor, suppress=True)
    keyboard.hook_key("down", alt_arrow_suppressor, suppress=True)
    keyboard.on_release_key("alt", _reset_alt_menu_guard)

    # No suppress=True here, deliberately: Z/R/Q are one-off taps, not
    # held keys apps rely on for their own behavior the way Up/Down are
    # (line move, browser history, etc.), and very few apps bind Z/R/Q
    # as top-level menu mnemonics. Leaving these unsuppressed means the
    # OS still sees a normal Alt+<key> combo (not a "solo" Alt tap), so
    # the menu-activation quirk we worked around for Up/Down doesn't
    # come up here in the first place.
    keyboard.add_hotkey("alt+z", state.toggle)        # single hotkey: on AND off
    keyboard.add_hotkey("alt+r", state.reset_zoom)      # snap back to 100% while staying active
    keyboard.add_hotkey("alt+q", state.quit)             # always fully exits

    try:
        mouse.hook(alt_scroll_handler, suppress=True)
    except TypeError:
        # Older versions of the `mouse` library don't accept a suppress
        # kwarg on hook(). Falls back to non-suppressing — Alt+Scroll
        # will still zoom, it just might also scroll whatever's under
        # the cursor at the same time. Not a big deal in practice.
        safe_print("NOTE: mouse.hook() suppress not supported by this "
                    "version of 'mouse' — Alt+Scroll may also scroll "
                    "the app underneath.")
        mouse.hook(alt_scroll_handler)


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

        if state._icon_dirty:
            # toggle() (running on the hotkey hook thread) can't safely
            # touch pystray directly — it just sets this flag. We do the
            # actual icon/title update here instead, on the worker
            # thread, where there's no hook timeout to worry about.
            state._refresh_tray_icon()
            state._icon_dirty = False

        if not state.active:
            if was_active:
                # Zoom was just switched off — reset the transform here,
                # on the thread that actually owns the mag session.
                mag.MagSetFullscreenTransform(ctypes.c_float(1.0), 0, 0)
                was_active = False
            continue

        was_active = True

        if state._deactivating:
            # Ignore Alt+arrow input while easing back to 1x — the
            # target is already forced to MIN_ZOOM by toggle().
            state.zoom_direction = 0
        else:
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

        prev_zoom = state.zoom
        zoom_alpha = 1.0 - math.exp(-ZOOM_SMOOTHING * dt)
        state.zoom += (state.target_zoom - state.zoom) * zoom_alpha
        zoom_changed = abs(state.zoom - prev_zoom) > 1e-6

        x, y = cursor()
        SCREEN_W, SCREEN_H = screen_size()
        visible_w = SCREEN_W / state.zoom
        visible_h = SCREEN_H / state.zoom

        if not state._pan_initialized:
            # First frame after activating zoom: center on the cursor
            # once, directly, so you start looking at where you were.
            target_left = x - visible_w / 2
            target_top = y - visible_h / 2
            target_left = max(0, min(target_left, SCREEN_W - visible_w))
            target_top = max(0, min(target_top, SCREEN_H - visible_h))
            state.pan_left = target_left
            state.pan_top = target_top
            state._pan_initialized = True

        elif zoom_changed:
            # Zoom-to-cursor: while the zoom level is actively ramping
            # (Alt+Up/Down or Alt+Scroll held), keep the exact point
            # under the cursor fixed on screen — same idea as zooming
            # in a map or image editor. Without this, only the *size*
            # of the visible area was shrinking each frame while its
            # top-left corner stayed put, which made zoom drift toward
            # the top-left instead of centering on the cursor.
            old_visible_w = SCREEN_W / prev_zoom
            old_visible_h = SCREEN_H / prev_zoom

            frac_x = (x - state.pan_left) / old_visible_w if old_visible_w > 0 else 0.5
            frac_y = (y - state.pan_top) / old_visible_h if old_visible_h > 0 else 0.5
            frac_x = max(0.0, min(1.0, frac_x))
            frac_y = max(0.0, min(1.0, frac_y))

            target_left = x - frac_x * visible_w
            target_top = y - frac_y * visible_h
            target_left = max(0, min(target_left, SCREEN_W - visible_w))
            target_top = max(0, min(target_top, SCREEN_H - visible_h))

            # Snapped directly, not eased — this has to track the zoom
            # ramp exactly frame by frame or the anchor point drifts.
            state.pan_left = target_left
            state.pan_top = target_top

        else:
            # Zoom is steady: switch to edge-triggered "dead zone"
            # panning. The view only moves once the cursor gets close
            # to the edge of what's visible, and then only pans enough
            # to keep the cursor just inside the dead zone — not glued
            # to the exact cursor position.
            margin_x = visible_w * EDGE_PAN_MARGIN
            margin_y = visible_h * EDGE_PAN_MARGIN

            rel_x = x - state.pan_left
            rel_y = y - state.pan_top

            target_left = state.pan_left
            target_top = state.pan_top

            if rel_x < margin_x:
                target_left = x - margin_x
            elif rel_x > visible_w - margin_x:
                target_left = x - (visible_w - margin_x)

            if rel_y < margin_y:
                target_top = y - margin_y
            elif rel_y > visible_h - margin_y:
                target_top = y - (visible_h - margin_y)

            target_left = max(0, min(target_left, SCREEN_W - visible_w))
            target_top = max(0, min(target_top, SCREEN_H - visible_h))

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

        if state._deactivating and abs(state.zoom - MIN_ZOOM) < 0.02:
            # Ease-out finished — snap the final values exactly to
            # normal and actually leave zoom mode now.
            state.zoom = MIN_ZOOM
            state.target_zoom = MIN_ZOOM
            state.pan_left = 0.0
            state.pan_top = 0.0
            mag.MagSetFullscreenTransform(ctypes.c_float(1.0), 0, 0)
            state.active = False
            state._deactivating = False
            was_active = False
            state._refresh_tray_icon()
            safe_print("Zoom toggled -> OFF")

    mag.MagUninitialize()

    if state.tray_icon is not None:
        try:
            state.tray_icon.stop()
        except Exception:
            pass


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

    def on_reset(icon, item):
        state.reset_zoom()

    def on_quit(icon, item):
        state.quit()

    menu = pystray.Menu(
        pystray.MenuItem("Toggle Zoom  (Alt+Z)", on_toggle),
        pystray.MenuItem("Reset to 100%  (Alt+R)", on_reset),
        pystray.MenuItem("Quit  (Alt+Q)", on_quit),
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

    safe_print("Hotkeys registered. Alt+Z to toggle, Alt+R to reset, Alt+Q to quit.")

    tray = build_tray_icon()

    worker = threading.Thread(target=zoom_loop, daemon=True)
    worker.start()

    tray.run()  # blocks on the main thread until state.quit() calls tray.stop()


if __name__ == "__main__":
    main()