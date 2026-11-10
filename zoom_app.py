"""
Windows Fullscreen Zoom — background / tray version
-----------------------------------------------------

Controls
--------
Ctrl+Alt+Z   Toggle zoom ON/OFF   (single hotkey does both directions)
Ctrl+Alt+Q   Quit the app entirely (exits the process)

Hold (plain Alt only — with Ctrl/Shift/Win also held, keys pass through):
    Alt + Up      Zoom in — also turns zoom ON if it was off
    Alt + Down    Zoom out — turns zoom OFF at 1x; passes through when off

Alt + scroll (mouse wheel), macOS-style:
    Scroll up     Zoom in — also turns zoom ON if it was off
    Scroll down   Zoom out — turns zoom OFF once you reach 1x
    Without Alt held, scrolling works normally.
    Zoom is anchored: whatever is under the cursor stays put while zooming.

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
from ctypes import wintypes
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
PAN_SMOOTHING = 18.0       # higher = snappier pan easing (only used when CENTER_ON_CURSOR)

# False (default): zoom toward the point under the cursor, which stays put
#   on screen (macOS-style). True: keep the cursor centred on screen
#   (the original behaviour).
CENTER_ON_CURSOR = False

# Alt + scroll (mouse wheel), macOS-style.
SCROLL_ZOOM_PER_NOTCH = 1.25   # zoom multiplier per wheel notch
SCROLL_ZOOM_INVERT = False     # set True if Alt+scroll zooms the opposite way you expect

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
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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

        # Alt+scroll bookkeeping (see handle_scroll_zoom / zoom_loop).
        self.scroll_off_pending = False   # scrolled down to 1x -> turn off once eased there
        self.alt_masked = False           # already sent the menu-mask key this Alt hold
        self.menu_mask_pending = False    # zoom_loop should send the menu-mask key
        self.down_blocked = False         # a Down press is being swallowed until it's released
        self.alt_key_down = False         # Alt is physically down (tracked by alt_repeat_suppressor)

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

            self.scroll_off_pending = False
            if self.active:
                self._pan_initialized = False
                # If the last session ended by Alt+scrolling down to 1x,
                # don't toggle back on at an invisible 1x.
                if self.target_zoom <= MIN_ZOOM + 1e-3:
                    self.target_zoom = DEFAULT_ZOOM
            # NOTE: no direct MagSetFullscreenTransform call here — this
            # runs on the hotkey callback thread, not the worker thread
            # that owns the magnification session. The worker thread
            # (zoom_loop) notices the state change and performs the
            # actual reset call itself, on the correct thread.

            self._refresh_tray_icon()

    def _activate_from_1x(self):
        """Turn zoom on starting at 1x, so Alt+scroll / Alt+Up grow it
        smoothly instead of jumping. Caller must hold self.lock."""
        self.active = True
        self.scroll_off_pending = False
        self._pan_initialized = False
        self.zoom = MIN_ZOOM
        self.target_zoom = MIN_ZOOM
        safe_print("Zoom toggled -> ON (Alt+scroll / Alt+Up)")
        self._refresh_tray_icon_async()

    def activate_with_alt_up(self):
        with self.lock:
            if not self.active and not self.stop:
                self._activate_from_1x()

    def handle_scroll_zoom(self, notches):
        """Apply an Alt+scroll amount, in mouse-wheel notches (positive =
        zoom in; may be fractional). Called from the mouse hook thread, so it
        must stay fast. Returns True if the scroll
        was used for zooming (and should be blocked from reaching the app)."""
        if SCROLL_ZOOM_INVERT:
            notches = -notches
        if notches == 0:
            return False

        with self.lock:
            if self.stop:
                return False

            if not self.active:
                if notches < 0:
                    return False  # already at 1x — let Alt+scroll-down through
                self._activate_from_1x()

            self.target_zoom *= SCROLL_ZOOM_PER_NOTCH ** notches
            self.target_zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.target_zoom))
            self.scroll_off_pending = self.target_zoom <= MIN_ZOOM

            if not self.alt_masked:
                self.alt_masked = True
                self.menu_mask_pending = True
        return True

    def quit(self):
        stop_input_thread()
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

    def _refresh_tray_icon_async(self):
        # Used from the mouse hook / render threads, which must never
        # block on the tray (a slow low-level hook gets dropped by Windows).
        threading.Thread(target=self._refresh_tray_icon, daemon=True).start()


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
# The hooks below only decide whether Up/Down reach the OS (and so other
# apps). Only *plain* Alt+arrow is taken — with Ctrl, Shift or Win also
# held the key always passes through, so combos like VS Code's
# Shift+Alt+Up (copy line) or Ctrl+Alt+Up (add cursor) keep working.
#   Alt+Up    always zooms in — it also turns zoom on when it's off,
#             like Alt+scroll up — so it's always blocked.
#   Alt+Down  only blocked while zoom is on; when zoom is off there's
#             nothing to zoom out of, so it passes through normally.
#             A press that was blocked stays blocked (auto-repeats and the
#             release too) until Down is released, even if zooming out to
#             1x switches zoom off partway through — otherwise the held
#             key would start reaching the focused app mid-press.

VK_SHIFT, VK_CONTROL, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x5B, 0x5C
VK_DOWN = 0x28


def plain_alt_held():
    """Alt held with no Ctrl / Shift / Win."""
    if not keyboard.is_pressed("alt"):
        return False
    return not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_SHIFT, VK_CONTROL, VK_LWIN, VK_RWIN))


def alt_up_suppressor(event):
    return not plain_alt_held()                    # False = block


def alt_down_suppressor(event):
    # Returns False to block the event, True to let it through.
    if event.event_type == "up":
        was_blocked = state.down_blocked
        state.down_blocked = False
        return not was_blocked   # swallow the release of a press we swallowed
    if state.down_blocked or (state.active and plain_alt_held()):
        state.down_blocked = True
        return False
    return True


# Alt auto-repeat. Once zooming starts during an Alt hold, one tap of the
# unassigned "mask" key (see VK_MENU_MASK) tells the focused app that Alt
# was used together with another key. But a held Alt keeps auto-repeating
# its key-down every ~30 ms *after* that tap, and the app then sees "Alt
# pressed alone" again, so letting go of Alt after an Alt+scroll lasting
# longer than the repeat delay still opened the menu bar / showed key tips
# (e.g. in the new Notepad). So once an Alt hold has been masked, its
# auto-repeats are swallowed; the app sees Alt down, mask key, Alt up.


def alt_repeat_suppressor(event):
    if event.event_type == "up":
        state.alt_key_down = False
        return True
    was_down = state.alt_key_down
    state.alt_key_down = True
    return not (was_down and state.alt_masked)   # False = swallow this auto-repeat


def register_hotkeys():
    keyboard.hook_key("alt", alt_repeat_suppressor, suppress=True)
    keyboard.hook_key("up", alt_up_suppressor, suppress=True)
    keyboard.hook_key("down", alt_down_suppressor, suppress=True)

    keyboard.add_hotkey("ctrl+alt+z", state.toggle)   # single hotkey: on AND off
    keyboard.add_hotkey("ctrl+alt+q", state.quit)      # always fully exits


# ============================================================
# Alt + scroll zoom (low-level mouse hook)
# ============================================================
#
# A mouse wheel sends WM_MOUSEWHEEL events, which this hook sees. While Alt
# is held the wheel event is consumed for zooming; without Alt it passes
# through untouched.
# Windows Precision Touchpad swipes reach this hook only some of the time:
# on a Surface they arrive as ordinary wheel events while the fullscreen
# magnifier is active, but not with zoom off (observed; cause unknown).
# So a swipe can zoom in/out once zoom is on, but can't start it from 1x.
#
# The hook runs on its own thread with a message loop (required for
# WH_MOUSE_LL). The callback must return quickly — Windows silently
# removes low-level hooks that time out — so it only updates numbers;
# the render loop does the actual work.

WH_MOUSE_LL = 14
HC_ACTION = 0
WM_MOUSEWHEEL = 0x020A
WM_QUIT = 0x0012
WHEEL_DELTA = 120
VK_MENU = 0x12
# Unassigned virtual key. Tapping it while Alt is held stops Windows
# from treating the Alt release as a lone "Alt tap", which would
# otherwise pop open the menu bar in many apps after Alt+scrolling.
VK_MENU_MASK = 0xE8
KEYEVENTF_KEYUP = 0x0002

LRESULT = ctypes.c_ssize_t


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


LowLevelMouseProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LowLevelMouseProc, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def alt_held():
    return bool(user32.GetAsyncKeyState(VK_MENU) & 0x8000)


def send_menu_mask_key():
    user32.keybd_event(VK_MENU_MASK, 0, 0, 0)
    user32.keybd_event(VK_MENU_MASK, 0, KEYEVENTF_KEYUP, 0)


@LowLevelMouseProc
def _mouse_hook_proc(n_code, w_param, l_param):
    try:
        if n_code == HC_ACTION and w_param == WM_MOUSEWHEEL and alt_held():
            info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            delta = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
            if state.handle_scroll_zoom(delta / WHEEL_DELTA):
                return 1  # consumed: don't let the app scroll as well
    except Exception:
        pass  # never let an exception break mouse input
    return user32.CallNextHookEx(None, n_code, w_param, l_param)


# ============================================================
# Input thread: owns the mouse hook
# ============================================================

_input_thread_id = None


def input_loop():
    global _input_thread_id
    _input_thread_id = kernel32.GetCurrentThreadId()

    hook = user32.SetWindowsHookExW(WH_MOUSE_LL, _mouse_hook_proc, kernel32.GetModuleHandleW(None), 0)
    if not hook:
        err = ctypes.get_last_error()
        safe_print(f"WARNING: mouse hook failed (GetLastError={err}); Alt+mouse-wheel zoom disabled.")
        return

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        pass  # low-level hook callbacks are delivered inside GetMessageW

    user32.UnhookWindowsHookEx(hook)


def stop_input_thread():
    if _input_thread_id is not None:
        user32.PostThreadMessageW(_input_thread_id, WM_QUIT, 0, 0)


# ============================================================
# Main render loop (runs on a background thread)
# ============================================================

def _centered_view(x, y, visible_w, visible_h):
    left = max(0.0, min(x - visible_w / 2, SCREEN_W - visible_w))
    top = max(0.0, min(y - visible_h / 2, SCREEN_H - visible_h))
    return left, top


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

        alt_is_held = alt_held()

        # Safety net for alt_down_suppressor: if a release event was ever
        # missed, don't leave the Down key swallowed forever.
        if state.down_blocked and not (user32.GetAsyncKeyState(VK_DOWN) & 0x8000):
            state.down_blocked = False

        # Alt+scroll menu-bar suppression (see VK_MENU_MASK). Done here
        # rather than in the mouse hook callback to keep that callback fast.
        if alt_is_held:
            if state.menu_mask_pending:
                state.menu_mask_pending = False
                send_menu_mask_key()
        else:
            state.alt_masked = False
            state.menu_mask_pending = False

        # Alt+Up turns zoom on when it's off (polled here, like the
        # arrow keys below, rather than decided in the key callback).
        if not state.active and keyboard.is_pressed("up") and plain_alt_held():
            state.activate_with_alt_up()

        if not state.active:
            if was_active:
                # Zoom was just switched off — reset the transform here,
                # on the thread that actually owns the mag session.
                mag.MagSetFullscreenTransform(ctypes.c_float(1.0), 0, 0)
                was_active = False
            continue

        was_active = True

        alt_down = plain_alt_held()
        up_held = alt_down and keyboard.is_pressed("up")
        down_held = alt_down and keyboard.is_pressed("down")

        if up_held and not down_held:
            state.zoom_direction = 1
        elif down_held and not up_held:
            state.zoom_direction = -1
        else:
            state.zoom_direction = 0

        if state.zoom_direction != 0:
            # The app never sees the blocked arrow key, only Alt down/up,
            # which would open its menu bar — mask it as with Alt+scroll.
            if not state.alt_masked:
                state.alt_masked = True
                state.menu_mask_pending = True
            state.target_zoom += state.zoom_direction * ZOOM_SPEED * dt
            state.target_zoom = max(MIN_ZOOM, min(MAX_ZOOM, state.target_zoom))
            # Same rule as Alt+scroll: zooming out all the way to 1x turns
            # zoom off, so Alt+Down never leaves an invisible "on at 1x"
            # state that keeps eating the arrow keys.
            state.scroll_off_pending = state.zoom_direction < 0 and state.target_zoom <= MIN_ZOOM

        zoom_alpha = 1.0 - math.exp(-ZOOM_SMOOTHING * dt)
        state.zoom += (state.target_zoom - state.zoom) * zoom_alpha

        # Alt+scrolled all the way back down: once the zoom has eased to
        # 1x, switch off (macOS-style). The next frame resets the transform.
        if state.scroll_off_pending and state.zoom - MIN_ZOOM < 0.005:
            with state.lock:
                if state.scroll_off_pending:
                    state.scroll_off_pending = False
                    state.active = False
                    state.zoom = MIN_ZOOM
                    safe_print("Zoom toggled -> OFF (scrolled to 1x)")
                    state._refresh_tray_icon_async()
            continue

        x, y = cursor()
        visible_w = SCREEN_W / state.zoom
        visible_h = SCREEN_H / state.zoom

        if not CENTER_ON_CURSOR:
            # Anchored (macOS-style): the cursor stays at the same spot on
            # screen as it would be unzoomed, so whatever is under it stays
            # put while zooming, and moving the cursor across the screen
            # pans across the whole desktop. The view comes straight from
            # the (already eased) zoom level — no separate pan easing, which
            # is what made zooming drift in a curve before.
            state.pan_left = max(0.0, min(x * (1.0 - 1.0 / state.zoom), SCREEN_W - visible_w))
            state.pan_top = max(0.0, min(y * (1.0 - 1.0 / state.zoom), SCREEN_H - visible_h))
            state._pan_initialized = True
        elif not state._pan_initialized:
            target_left, target_top = _centered_view(x, y, visible_w, visible_h)
            state.pan_left = target_left
            state.pan_top = target_top
            state._pan_initialized = True
        else:
            target_left, target_top = _centered_view(x, y, visible_w, visible_h)
            pan_alpha = 1.0 - math.exp(-PAN_SMOOTHING * dt)
            state.pan_left += (target_left - state.pan_left) * pan_alpha
            state.pan_top += (target_top - state.pan_top) * pan_alpha

        mag_ok = mag.MagSetFullscreenTransform(
            ctypes.c_float(state.zoom),
            int(round(state.pan_left)),
            int(round(state.pan_top)),
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

    input_thread = threading.Thread(target=input_loop, daemon=True)
    input_thread.start()

    tray.run()  # blocks on the main thread until state.quit() calls tray.stop()


if __name__ == "__main__":
    main()