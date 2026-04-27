import time
import math
import threading
import os
import ctypes
import ctypes.wintypes
import pandas as pd
from pynput import keyboard, mouse  # type: ignore

# ── CSV file path ────────────────────────────────────────────────────────────────
CSV_FILE = "user_biometric_data.csv"

ALL_COLS = [
    "timestamp", "session_id", "source", "event",
    # Keyboard
    "key", "dwell_time", "flight_time",
    # Tap / Click
    "button", "inter_tap_time", "hold_duration",
    # Move
    "x", "y", "dx", "dy", "distance", "speed_px_per_s", "direction_deg",
    # Scroll
    "accum_dx", "accum_dy", "direction", "duration_s", "scroll_speed",
    # Touch (Windows WM_POINTER)
    "finger_count", "pressure",
]

# ── Session ID — unique per run ──────────────────────────────────────────────────
SESSION_ID = str(int(time.time()))

# ── Shared state ─────────────────────────────────────────────────────────────────
data = []
data_lock = threading.Lock()

# Keyboard
key_press_times = {}
last_key_release_time = None

# Mouse / Touchpad
last_mouse_pos = None
last_mouse_time = None
last_click_time = None
scroll_start_time = None
scroll_dx_accum = 0
scroll_dy_accum = 0

# Touch state (WM_POINTER)
touch_state = {"finger_count": 0, "pressure": 0}

# ── Windows POINTER structs (for pressure + finger count) ────────────────────────
# Only loaded when running on Windows
POINTER_AVAILABLE = False
try:
    user32 = ctypes.windll.user32

    # GetPointerTouchInfo constants
    PT_TOUCH = 0x00000002
    POINTER_MESSAGE_FLAG_NEW       = 0x00000001
    POINTER_MESSAGE_FLAG_INRANGE   = 0x00000002
    POINTER_MESSAGE_FLAG_INCONTACT = 0x00000004

    class POINTER_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerType",         ctypes.c_uint32),
            ("pointerId",           ctypes.c_uint32),
            ("frameId",             ctypes.c_uint32),
            ("pointerFlags",        ctypes.c_uint32),
            ("sourceDevice",        ctypes.c_void_p),
            ("hwndTarget",          ctypes.wintypes.HWND),
            ("ptPixelLocation",     ctypes.wintypes.POINT),
            ("ptHimetricLocation",  ctypes.wintypes.POINT),
            ("ptPixelLocationRaw",  ctypes.wintypes.POINT),
            ("ptHimetricLocationRaw", ctypes.wintypes.POINT),
            ("dwTime",              ctypes.c_uint32),
            ("historyCount",        ctypes.c_uint32),
            ("inputData",           ctypes.c_int32),
            ("dwKeyStates",         ctypes.c_uint32),
            ("PerformanceCount",    ctypes.c_uint64),
            ("ButtonChangeType",    ctypes.c_uint32),
        ]

    class TOUCH_CONTACT(ctypes.Structure):
        _fields_ = [
            ("touchMask",  ctypes.c_uint32),
            ("rcContact",  ctypes.wintypes.RECT),
            ("rcContactRaw", ctypes.wintypes.RECT),
            ("orientation", ctypes.c_uint32),
            ("pressure",   ctypes.c_uint32),
        ]

    class POINTER_TOUCH_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerInfo",  POINTER_INFO),
            ("touchFlags",   ctypes.c_uint32),
            ("touchMask",    ctypes.c_uint32),
            ("rcContact",    ctypes.wintypes.RECT),
            ("rcContactRaw", ctypes.wintypes.RECT),
            ("orientation",  ctypes.c_uint32),
            ("pressure",     ctypes.c_uint32),
        ]

    POINTER_AVAILABLE = True
except Exception:
    pass  # Non-Windows or missing ctypes support — touch fields stay 0


def poll_touch_state():
    """
    Background thread: polls WM_POINTER touch contacts every 20 ms.
    Updates touch_state with live finger_count and average pressure.
    """
    if not POINTER_AVAILABLE:
        return

    while not stop_event.is_set():
        try:
            count = ctypes.c_uint32(0)
            # GetPointerDeviceRects not needed; we poll active pointer IDs
            # Use GetPointerFrameTouchInfo with pointer ID 0 to enumerate contacts
            pti = (POINTER_TOUCH_INFO * 10)()
            c = ctypes.c_uint32(10)
            # GetPointerFrameTouchInfo requires a known pointerId — 
            # instead we walk IDs 0-9 to find active contacts
            active = []
            for pid in range(10):
                info = POINTER_TOUCH_INFO()
                ok = user32.GetPointerTouchInfo(ctypes.c_uint32(pid), ctypes.byref(info))
                if ok:
                    flags = info.pointerInfo.pointerFlags
                    if flags & POINTER_MESSAGE_FLAG_INCONTACT:
                        active.append(info.pressure)

            touch_state["finger_count"] = len(active)
            touch_state["pressure"] = round(sum(active) / len(active), 2) if active else 0
        except Exception:
            pass
        time.sleep(0.02)


# ── Helper ────────────────────────────────────────────────────────────────────────
def log(event_type, **kwargs):
    row = {
        "timestamp":    round(time.time(), 6),
        "session_id":   SESSION_ID,
        "source":       "keyboard" if "key" in kwargs else "touchpad",
        "event":        event_type,
        "finger_count": touch_state["finger_count"],
        "pressure":     touch_state["pressure"],
    }
    row.update(kwargs)
    with data_lock:
        data.append(row)


# ── Keyboard callbacks ────────────────────────────────────────────────────────────
def on_press(key):
    global last_key_release_time
    current_time = time.time()

    try:
        key_name = key.char
    except AttributeError:
        key_name = str(key)

    if key_name not in key_press_times:
        key_press_times[key_name] = current_time
        flight_time = round(current_time - last_key_release_time, 4) if last_key_release_time else 0
        log("key_press", key=key_name, flight_time=flight_time)


def on_release(key):
    global last_key_release_time
    current_time = time.time()

    try:
        key_name = key.char
    except AttributeError:
        key_name = str(key)

    if key_name in key_press_times:
        dwell_time = round(current_time - key_press_times.pop(key_name), 4)
        last_key_release_time = current_time
        log("key_release", key=key_name, dwell_time=dwell_time)

    if key == keyboard.Key.esc:
        save_and_exit()
        return False


# ── Touchpad callbacks ────────────────────────────────────────────────────────────
def on_move(x, y):
    global last_mouse_pos, last_mouse_time
    current_time = time.time()

    if last_mouse_pos is None:
        last_mouse_pos = (x, y)
        last_mouse_time = current_time
        return

    dx = x - last_mouse_pos[0]
    dy = y - last_mouse_pos[1]
    dt = current_time - last_mouse_time
    distance = math.hypot(dx, dy)
    speed = round(distance / dt, 2) if dt > 0 else 0
    angle = round(math.degrees(math.atan2(dy, dx)), 1)

    log("move", x=x, y=y,
        dx=round(dx, 2), dy=round(dy, 2),
        distance=round(distance, 2),
        speed_px_per_s=speed,
        direction_deg=angle)

    last_mouse_pos = (x, y)
    last_mouse_time = current_time


def on_click(x, y, button, pressed):
    global last_click_time
    current_time = time.time()
    btn_name = str(button).replace("Button.", "")

    if pressed:
        inter_tap_time = round(current_time - last_click_time, 4) if last_click_time else 0
        log("tap_press", x=x, y=y, button=btn_name, inter_tap_time=inter_tap_time)
        last_click_time = current_time
    else:
        hold_duration = round(current_time - last_click_time, 4) if last_click_time else 0
        log("tap_release", x=x, y=y, button=btn_name, hold_duration=hold_duration)


def on_scroll(x, y, dx, dy):
    global scroll_start_time, scroll_dx_accum, scroll_dy_accum
    current_time = time.time()

    if scroll_start_time is None:
        scroll_start_time = current_time
        scroll_dx_accum = 0
        scroll_dy_accum = 0

    scroll_dx_accum += dx
    scroll_dy_accum += dy
    duration = round(current_time - scroll_start_time, 4)
    direction = ("up" if dy > 0 else "down") if abs(dy) >= abs(dx) else ("right" if dx > 0 else "left")
    speed = round(math.hypot(scroll_dx_accum, scroll_dy_accum) / duration, 2) if duration > 0 else 0

    log("scroll", x=x, y=y, dx=dx, dy=dy,
        accum_dx=round(scroll_dx_accum, 2),
        accum_dy=round(scroll_dy_accum, 2),
        direction=direction, duration_s=duration, scroll_speed=speed)


# ── Persistent save (append, never overwrite) ─────────────────────────────────────
def save_and_exit():
    stop_event.set()

    with data_lock:
        new_df = pd.DataFrame(data)

    # Ensure all columns exist
    for col in ALL_COLS:
        if col not in new_df.columns:
            new_df[col] = None
    new_df = new_df[ALL_COLS].sort_values("timestamp").reset_index(drop=True)

    if os.path.exists(CSV_FILE):
        # Load existing data and append — preserving every past session
        existing_df = pd.read_csv(CSV_FILE)
        combined_df = pd.concat([existing_df, new_df], ignore_index=True)
        combined_df.to_csv(CSV_FILE, index=False)
        print(f"\n✓ Appended {len(new_df)} rows to existing {CSV_FILE}  "
            f"(total: {len(combined_df)} rows across all sessions)")
    else:
        new_df.to_csv(CSV_FILE, index=False)
        print(f"\n✓ Created {CSV_FILE} with {len(new_df)} rows  (session {SESSION_ID})")


# ── Entry point ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    stop_event = threading.Event()

    # Start Windows touch-polling thread
    touch_thread = threading.Thread(target=poll_touch_state, daemon=True)
    touch_thread.start()

    # Start mouse/touchpad listener
    mouse_listener = mouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll)
    mouse_listener.start()

    print(f"Session {SESSION_ID} started.")
    print(f"Recording to '{CSV_FILE}' — previous sessions are preserved.")
    print("Type and use your touchpad naturally. Press [Esc] to stop.\n")

    with keyboard.Listener(on_press=on_press, on_release=on_release) as kb_listener:
        kb_listener.join()

    mouse_listener.stop()
    stop_event.set()