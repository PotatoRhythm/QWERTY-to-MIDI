from pynput import keyboard
import keyboard as kb
import mido
import tkinter as tk
import customtkinter as ctk
import threading
import json
import os
import sys
import math
import ctypes

# Also read by QWERTY-to-MIDI.spec for the .exe's version info
__version__ = "1.0"

# ---------- Load/save key bindings ----------
def app_dir():
    # Keep settings next to the .exe (or script) instead of the current working directory
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

KEYBIND_FILE = os.path.join(app_dir(), "key_bindings.json")

note_labels = [
    "C", "C#", "D", "D#", "E", "F",
    "F#", "G", "G#", "A", "A#", "B", "C High"
]
default_key_bindings = {
    "C": "q", "C#": "2", "D": "w", "D#": "3", "E": "e",
    "F": "r", "F#": "5", "G": "t", "G#": "6", "A": "y",
    "A#": "7", "B": "u", "C High": "i",
    "Octave Down": "o", "Octave Up": "p",
    "Sustain": "space", "Volume Down": "[",
    "Volume Up": "]", "Reset Volume": "\\",
    "MIDI Toggle": "ctrl+/"
}
modifier_keys = {
    "shift", "shift_l", "shift_r",
    "ctrl", "ctrl_l", "ctrl_r",
    "alt", "alt_l", "alt_r",
}
CANCEL_REBIND_KEY = "esc"

rebind_listener = None
waiting_for_bind = None
active_modifier = None
binding_buttons = {}
binding_styles = {}
binding_listening_styles = {}
# The popup with the rebind buttons; only exists while it's open
hotkeys_window = None

def vk_to_char(vk):
    # The unshifted character a virtual key types on the current keyboard layout (e.g. 191 -> "/")
    if 48 <= vk <= 57 or 65 <= vk <= 90:
        return chr(vk).lower()
    if 96 <= vk <= 111:
        return None
    try:
        # MAPVK_VK_TO_CHAR; the top bit marks dead keys, which still name a usable character
        code = ctypes.windll.user32.MapVirtualKeyW(vk, 2) & 0x7FFFFFFF
    except Exception:
        return None
    return chr(code).lower() if code > 32 else None

def repair_binding(binding):
    # Older versions saved Ctrl+punctuation as the raw key code (e.g. "ctrl+191" for Ctrl+/),
    # and briefly saved Space as " "
    head, sep, tail = binding.rpartition("+")
    if tail == " ":
        return head + sep + "space"
    if len(tail) >= 2 and tail.isdigit():
        vk_char = vk_to_char(int(tail))
        if vk_char:
            return head + sep + vk_char
    return binding

def load_key_bindings():
    if os.path.exists(KEYBIND_FILE):
        try:
            with open(KEYBIND_FILE, "r") as f:
                data = json.load(f)
            for k in default_key_bindings:
                if k not in data:
                    data[k] = default_key_bindings[k]
            return {k: repair_binding(v) for k, v in data.items()}
        except Exception as e:
            print(f"Failed to load {KEYBIND_FILE}: {e}")
    return default_key_bindings.copy()


def save_key_bindings():
    try:
        with open(KEYBIND_FILE, "w") as f:
            json.dump(key_bindings, f, indent=2)
    except Exception as e:
        print(f"Failed to save {KEYBIND_FILE}: {e}")

# ---------- MIDI Setup ----------
root_note = 60
current_octave = 0
min_octave = -4
max_octave = 4
transpose_amount = 0
DEFAULT_VELOCITY = 100
velocity_value = DEFAULT_VELOCITY
active_notes = {}
midi_mode = False
listener = None
key_bindings = load_key_bindings()
blocked_keys = set()

SUSTAIN_TAP = "Tap to release"
SUSTAIN_HOLD = "Hold to sustain"
sustain_mode = SUSTAIN_TAP

note_length = 2.5

# Notes held by the sustain key in "Hold to sustain" mode
sustain_times = {}
# Pending note-off timers for released notes that are still ringing
sustain_timers = {}
sustain_timers_lock = threading.Lock()
hold_sustain_active = False

# ---------- MIDI Port ----------
NO_PORTS = "No ports found"

def get_available_ports():
    return [p for p in mido.get_output_names() if "Microsoft GS Wavetable" not in p]

available_ports = get_available_ports()
outport = None
send_lock = threading.Lock()

if available_ports:
    try:
        outport = mido.open_output(available_ports[0])
    except Exception as e:
        print(f"Failed to open default port: {e}")
        outport = None

def set_midi_port(port_name):
    global outport
    release_all_notes()
    try:
        if outport:
            outport.close()
        outport = mido.open_output(port_name)
        print(f"MIDI out set to: {port_name}")
    except Exception as e:
        print(f"Failed to open MIDI port {port_name}: {e}")
        outport = None
    on_port_changed()

def close_midi_port():
    global outport
    release_all_notes()
    if outport:
        outport.close()
    outport = None
    on_port_changed()

def refresh_midi_ports():
    global available_ports
    available_ports = get_available_ports()

    if not available_ports:
        close_midi_port()
        midi_var.set(NO_PORTS)
        midi_dropdown.configure(values=[NO_PORTS], state="disabled")
        return

    midi_dropdown.configure(values=available_ports, state="normal")
    # Keep the current port if it's still there, otherwise switch to the first one
    if outport is None or midi_var.get() not in available_ports:
        midi_var.set(available_ports[0])
        set_midi_port(available_ports[0])

# ---------- MIDI Note Handling ----------
def send_note(msg_type, note, velocity):
    if outport is None:
        return
    try:
        with send_lock:
            outport.send(mido.Message(msg_type, note=note, velocity=velocity))
    except Exception as e:
        print(f"Failed to send {msg_type} {note}: {e}")

def note_on(note):
    send_note("note_on", note, velocity_value)

def note_off(note):
    send_note("note_off", note, 100)

def schedule_note_off(note, delay):
    def _off():
        with sustain_timers_lock:
            if sustain_timers.get(note) is not timer:
                return
            sustain_timers.pop(note, None)
        note_off(note)

    timer = threading.Timer(delay, _off)
    timer.daemon = True
    with sustain_timers_lock:
        existing = sustain_timers.pop(note, None)
        if existing:
            existing.cancel()
        sustain_timers[note] = timer
    timer.start()

def cancel_and_release_timers():
    with sustain_timers_lock:
        pending = list(sustain_timers.items())
        sustain_timers.clear()
    for n, t in pending:
        t.cancel()
        note_off(n)

def stop_ringing(note):
    # Cut a note that is still sounding from an earlier press
    sustained = sustain_times.pop(note, None)
    with sustain_timers_lock:
        timer = sustain_timers.pop(note, None)
    if timer:
        timer.cancel()
    if sustained or timer:
        note_off(note)

def reset_sustain():
    for n in list(sustain_times.keys()):
        note_off(n)
        sustain_times.pop(n, None)
    cancel_and_release_timers()

def release_all_notes():
    for note in list(active_notes.values()):
        note_off(note)
    active_notes.clear()
    reset_sustain()

# ---------- Hotkey Handling ----------
def get_key_name(key):
    ch = getattr(key, "char", None)
    vk = getattr(key, "vk", None)
    if vk is None:
        val = getattr(key, "value", None)
        vk = getattr(val, "vk", None)

    # With Ctrl held, letters arrive as control characters (e.g. Ctrl+Q -> "\x11")
    # and digits and punctuation arrive with no char, so fall back to the virtual key code
    if vk == 32:
        return "space"
    if isinstance(key, keyboard.KeyCode) and (not ch or ord(ch[0]) < 32) and vk is not None:
        vk_char = vk_to_char(vk)
        if vk_char:
            return vk_char
    if ch:
        return str(ch).lower()

    numpad_map = {
        96: "numpad0", 97: "numpad1", 98: "numpad2", 99: "numpad3",
        100: "numpad4", 101: "numpad5", 102: "numpad6",
        103: "numpad7", 104: "numpad8", 105: "numpad9",
        106: "numpad_multiply", 107: "numpad_add",
        109: "numpad_subtract", 110: "numpad_decimal",
        111: "numpad_divide",
    }
    if vk in numpad_map:
        return numpad_map[vk]

    name = getattr(key, "name", None)
    if name:
        return str(name).lower()

    s = str(key)
    s = s.replace("Key.", "").replace("<", "").replace(">", "").strip()
    return s.lower()

def modifier_name(key_name):
    # "shift_r" -> "shift", or None if key_name isn't a modifier
    if key_name in modifier_keys:
        return key_name.split("_")[0]
    return None

def build_combo_key(base):
    if active_modifier:
        return f"{active_modifier}+{base}"
    return base

def on_ui_thread(func, *args):
    # pynput and keyboard callbacks run on background threads; Tk must only be touched from the main thread
    root.after(0, func, *args)

# ---------- MIDI Listeners ----------
def on_press(key):
    global current_octave, hold_sustain_active, active_modifier

    if waiting_for_bind:
        return

    key_name = get_key_name(key)

    modifier = modifier_name(key_name)
    if modifier:
        active_modifier = modifier
        return

    keybind = build_combo_key(key_name)

    if keybind == key_bindings.get("Sustain"):
        if sustain_mode == SUSTAIN_HOLD:
            hold_sustain_active = True
        else:
            reset_sustain()
        return

    for i, label in enumerate(note_labels):
        if keybind == key_bindings.get(label):
            if keybind not in active_notes:
                note = root_note + i + current_octave * 12 + transpose_amount
                if not 0 <= note <= 127:
                    return
                stop_ringing(note)
                note_on(note)
                active_notes[keybind] = note
            return

    if keybind == key_bindings.get("Octave Up"):
        current_octave = min(current_octave + 1, max_octave)
        on_ui_thread(update_octave_display)
        return

    if keybind == key_bindings.get("Octave Down"):
        current_octave = max(current_octave - 1, min_octave)
        on_ui_thread(update_octave_display)
        return


def on_release(key):
    global hold_sustain_active, active_modifier
    key_name = get_key_name(key)

    if key_name in modifier_keys:
        active_modifier = None
        return

    keybind = build_combo_key(key_name)

    if keybind == key_bindings.get("Sustain") and sustain_mode == SUSTAIN_HOLD:
        hold_sustain_active = False
        for n in list(sustain_times.keys()):
            schedule_note_off(n, note_length)
            sustain_times.pop(n, None)
        return

    if keybind in active_notes:
        note = active_notes.pop(keybind)
        if sustain_mode == SUSTAIN_HOLD and hold_sustain_active:
            sustain_times[note] = True
        else:
            schedule_note_off(note, note_length)

# ---------- Midi Toggle ----------
def start_main_listener():
    global listener
    if listener is None:
        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()

def stop_main_listener():
    global listener
    if listener is not None:
        try:
            listener.stop()
        except Exception:
            pass
        listener = None

def toggle_mode():
    global midi_mode, active_modifier, hold_sustain_active
    if not midi_mode and outport is None:
        return
    midi_mode = not midi_mode
    update_toggle_button()
    refresh_hotkeys()

    if midi_mode:
        start_main_listener()
    else:
        stop_main_listener()
        release_all_notes()
        active_modifier = None
        hold_sustain_active = False

def update_blocked_keys(enable):
    global blocked_keys
    keys_to_block = set(key_bindings.get(k) for k in note_labels + ["Octave Up", "Octave Down", "Sustain"])
    keys_to_block = {k for k in keys_to_block if k}

    for k in blocked_keys:
        try:
            kb.unblock_key(k)
        except Exception:
            pass
    blocked_keys = set()

    if enable:
        for k in keys_to_block:
            try:
                kb.block_key(k)
                blocked_keys.add(k)
            except Exception as e:
                print(f"Could not block key {k!r}: {e}")

# ---------- Rebinding ----------
KEY_DISPLAY_NAMES = {
    "space": "Space", "esc": "Esc", "enter": "Enter", "tab": "Tab",
    "backspace": "Bksp", "caps_lock": "Caps", "delete": "Del", "insert": "Ins",
    "page_up": "PgUp", "page_down": "PgDn", "home": "Home", "end": "End",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
}
MODIFIER_DISPLAY_NAMES = {"shift": "⇧", "ctrl": "Ctrl+", "alt": "Alt+"}

def format_key(binding):
    # "shift+q" -> "⇧Q", "space" -> "Space", "numpad1" -> "Num1"
    modifier, base = "", binding
    head, sep, tail = binding.partition("+")
    if sep and tail and head in MODIFIER_DISPLAY_NAMES:
        modifier, base = MODIFIER_DISPLAY_NAMES[head], tail

    if len(base) == 1:
        base = base.upper()
    elif base.startswith("numpad"):
        base = "Num" + base[len("numpad"):].replace("_", " ").title()
    else:
        base = KEY_DISPLAY_NAMES.get(base, base.replace("_", " ").title())
    return modifier + base

def update_binding_button(action):
    binding_buttons[action].configure(text=format_key(key_bindings[action]), **binding_styles[action])

def cancel_rebind():
    global waiting_for_bind, rebind_listener, active_modifier
    if waiting_for_bind is None:
        return

    action = waiting_for_bind
    waiting_for_bind = None
    active_modifier = None
    update_binding_button(action)

    if rebind_listener is not None:
        rebind_listener.stop()
        rebind_listener = None

    if hotkeys_window is not None:
        hotkeys_window.unbind("<Button-1>")
        hotkeys_window.unbind("<FocusOut>")

    if midi_mode:
        start_main_listener()

def finish_rebind(action, keybind):
    if waiting_for_bind != action:
        return

    if keybind != CANCEL_REBIND_KEY:
        # If another action already uses this key, give it this action's old key
        for other, bound in key_bindings.items():
            if other != action and bound == keybind:
                key_bindings[other] = key_bindings[action]
                update_binding_button(other)
        key_bindings[action] = keybind
        save_key_bindings()
        refresh_hotkeys()
        update_toggle_button()

    cancel_rebind()

def start_rebind(action):
    global waiting_for_bind, rebind_listener, active_modifier

    if waiting_for_bind is not None:
        previous = waiting_for_bind
        cancel_rebind()
        if previous == action:
            return

    waiting_for_bind = action
    active_modifier = None
    btn = binding_buttons[action]
    btn.configure(text="…", **binding_listening_styles[action])

    stop_main_listener()

    def on_click_outside(event):
        parent = event.widget
        while parent is not None:
            if parent == btn:
                return
            parent = getattr(parent, "master", None)
        cancel_rebind()

    hotkeys_window.bind("<Button-1>", on_click_outside)
    hotkeys_window.bind("<FocusOut>", lambda e: cancel_rebind())

    def _on_press_rebind(key):
        global active_modifier
        key_name = get_key_name(key)

        modifier = modifier_name(key_name)
        if modifier:
            active_modifier = modifier
            return

        on_ui_thread(finish_rebind, action, build_combo_key(key_name))
        return False

    def _on_release_rebind(key):
        global active_modifier
        if get_key_name(key) in modifier_keys:
            active_modifier = None

    rebind_listener = keyboard.Listener(on_press=_on_press_rebind, on_release=_on_release_rebind)
    rebind_listener.start()


def reset_bindings():
    global key_bindings
    cancel_rebind()
    key_bindings = default_key_bindings.copy()
    save_key_bindings()
    refresh_hotkeys()
    update_toggle_button()
    for action in binding_buttons:
        update_binding_button(action)

# ---------- Theme ----------
# Colors are (light, dark) pairs; CustomTkinter picks one based on the appearance mode
BG = ("#f4f6f5", "#2b2b2b")
TEXT = ("#1d2421", "#e3e8ec")
MUTED_TEXT = ("#5f6b66", "#9aa3a8")
# Slightly stronger than MUTED_TEXT, for labels that name a control
LABEL_TEXT = ("#48534e", "#b9c1c5")
FIELD = ("#ffffff", "#343638")
FIELD_BORDER = ("#c5ccc8", "#565b5e")
DIVIDER = ("#d5dbd8", "#3d4042")
RANGE_OUTLINE = ("#717d78", "#e3e8ec")

# Brightest green is reserved for the main action
MAIN_GREEN = "#2fa572"
MAIN_GREEN_HOVER = "#27905f"
STOP_RED = ("#c46d69", "#8e4441")
STOP_RED_HOVER = ("#b45e5a", "#7d3b38")
DISABLED = ("#d3dad6", "#3d4441")
DISABLED_TEXT = ("#7d8a84", "#8d9893")
CALM_GREEN = "#2a9768"
CALM_GREEN_DARK = "#227d56"
CALM_GREEN_HOVER = "#1f6f4c"
SECONDARY = ("#8ecaac", "#3b7d60")
SECONDARY_HOVER = ("#7dbb9c", "#336d54")
SECONDARY_TEXT = ("#062b19", "#ffffff")

WHITE_KEY = ("#ffffff", "#e3e8ec")
WHITE_KEY_HOVER = ("#e2e9e5", "#c9d0cc")
WHITE_KEY_TEXT = "#1d2421"
BLACK_KEY = ("#2b2b2b", "#1a1b1b")
BLACK_KEY_HOVER = ("#3d4140", "#2c2e2e")
BLACK_KEY_TEXT = "#e3e8ec"

SECONDARY_STYLE = dict(fg_color=SECONDARY, hover_color=SECONDARY_HOVER, text_color=SECONDARY_TEXT)
WHITE_KEY_STYLE = dict(fg_color=WHITE_KEY, hover_color=WHITE_KEY_HOVER, text_color=WHITE_KEY_TEXT,
                       border_width=1, border_color=FIELD_BORDER)
BLACK_KEY_STYLE = dict(fg_color=BLACK_KEY, hover_color=BLACK_KEY_HOVER, text_color=BLACK_KEY_TEXT, border_width=0)

# While waiting for a new key: piano keys stay close to their own color with a green outline,
# control buttons switch to the main green
WHITE_KEY_LISTENING = dict(fg_color=WHITE_KEY_HOVER, hover_color=WHITE_KEY_HOVER, text_color=WHITE_KEY_TEXT,
                           border_width=2, border_color=MAIN_GREEN)
BLACK_KEY_LISTENING = dict(fg_color=BLACK_KEY_HOVER, hover_color=BLACK_KEY_HOVER, text_color=BLACK_KEY_TEXT,
                           border_width=2, border_color=MAIN_GREEN)
CONTROL_LISTENING = dict(fg_color=MAIN_GREEN, hover_color=MAIN_GREEN, text_color="#ffffff")

def blend(color, background, amount):
    # Mix `amount` of color into background, giving a solid (not see-through) tint
    c = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(background[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(cc * amount + bb * (1 - amount)):02x}" for cc, bb in zip(c, b))

def readable_text_color(color):
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#1d2421" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#ffffff"

def format_signed(value):
    return f"{value:+d}" if value else "0"

# Buttons that draw their icon as canvas shapes; these need recoloring when the theme changes
button_symbols = []

def button_center(button):
    # CTk draws the button shape at the scaled size rounded down to an even number of pixels
    w = button._apply_widget_scaling(button._current_width)
    h = button._apply_widget_scaling(button._current_height)
    return int(w / 2), int(h / 2)

def set_button_symbol(button, items):
    for b, old_items in list(button_symbols):
        if b is button:
            for item in old_items:
                button._canvas.delete(item)
            button_symbols.remove((b, old_items))
    button_symbols.append((button, items))

def recolor_button_symbols():
    for button, items in button_symbols:
        color = button._apply_appearance_mode(button._text_color)
        for item in items:
            button._canvas.itemconfigure(item, fill=color)

def arc_points(cx, cy, radius, start, end, through):
    # Points along a circle from angle start to end, going whichever way passes `through`
    span = (end - start) % (2 * math.pi)
    if (through - start) % (2 * math.pi) > span:
        span -= 2 * math.pi
    steps = 16
    return [(cx + radius * math.cos(start + span * i / steps), cy + radius * math.sin(start + span * i / steps))
            for i in range(steps + 1)]

def crescent_points(radius):
    # Crescent = circle minus a smaller circle offset up and to the right, centered on its own bounding box
    ox, oy, inner = 0.55 * radius, -0.45 * radius, 0.8 * radius
    d = math.hypot(ox, oy)
    along = (d * d + radius * radius - inner * inner) / (2 * d)
    across = math.sqrt(radius * radius - along * along)
    ux, uy = ox / d, oy / d
    p1 = (along * ux - across * uy, along * uy + across * ux)
    p2 = (along * ux + across * uy, along * uy - across * ux)

    away = math.atan2(-oy, -ox)
    points = (arc_points(0, 0, radius, math.atan2(p1[1], p1[0]), math.atan2(p2[1], p2[0]), through=away)
              + arc_points(ox, oy, inner, math.atan2(p2[1] - oy, p2[0] - ox),
                           math.atan2(p1[1] - oy, p1[0] - ox), through=away))
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return [(x - mx, y - my) for x, y in points]

def draw_theme_icon(button, sun):
    # Sun (switch to light) or crescent moon (switch to dark), drawn as shapes so they sit dead center
    canvas = button._canvas
    scale = button._apply_widget_scaling
    cx, cy = button_center(button)
    color = button._apply_appearance_mode(button._text_color)
    items = []

    if sun:
        core = scale(3.2)
        items.append(canvas.create_oval(cx - core, cy - core, cx + core, cy + core, fill=color, outline=""))
        for i in range(8):
            angle = i * math.pi / 4
            x1, y1 = cx + scale(5.5) * math.cos(angle), cy + scale(5.5) * math.sin(angle)
            x2, y2 = cx + scale(8) * math.cos(angle), cy + scale(8) * math.sin(angle)
            items.append(canvas.create_line(x1, y1, x2, y2, fill=color, width=max(1, scale(1.6)), capstyle="round"))
    else:
        points = [c for x, y in crescent_points(scale(6.5)) for c in (cx + x, cy + y)]
        items.append(canvas.create_polygon(points, fill=color, outline=""))

    set_button_symbol(button, items)

# ---------- GUI Setup ----------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

root = ctk.CTk(fg_color=BG)
root.title(f"QWERTY-to-MIDI v{__version__}")
root.resizable(False, False)

heading_font = ctk.CTkFont(size=15, weight="bold")
body_font = ctk.CTkFont(size=13)
small_font = ctk.CTkFont(size=12)
key_font = ctk.CTkFont(size=13, weight="bold")

content = ctk.CTkFrame(root, fg_color="transparent")
content.pack(padx=20, pady=(12, 16))

def section_heading(row, text, parent=content, space_above=18, space_below=6):
    # More space above a heading than below it, except for the first row
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.grid(row=row, column=0, sticky="ew", pady=(0 if row == 0 else space_above, space_below))
    ctk.CTkLabel(frame, text=text, font=heading_font, text_color=TEXT, height=30).pack(side="left")
    return frame

def field_label(parent, text, color=MUTED_TEXT):
    return ctk.CTkLabel(parent, text=text, font=small_font, text_color=color, height=18, anchor="w")

def styled_dropdown(parent, **kwargs):
    return ctk.CTkOptionMenu(
        parent, height=30, font=body_font, anchor="w", dynamic_resizing=False,
        fg_color=CALM_GREEN, button_color=CALM_GREEN_DARK, button_hover_color=CALM_GREEN_HOVER,
        text_color="#ffffff", text_color_disabled=("#d8efe3", "#a9cbb9"),
        dropdown_fg_color=FIELD, dropdown_hover_color=SECONDARY, dropdown_text_color=TEXT,
        dropdown_font=body_font, **kwargs
    )

def styled_slider(parent, **kwargs):
    return ctk.CTkSlider(
        parent, fg_color=FIELD_BORDER, progress_color=CALM_GREEN,
        button_color=MAIN_GREEN, button_hover_color=MAIN_GREEN_HOVER, **kwargs
    )

content.grid_columnconfigure(0, minsize=320)

# ---------- MIDI Port ----------
port_heading = section_heading(0, "MIDI Port")

port_row = ctk.CTkFrame(content, fg_color="transparent")
port_row.grid(row=1, column=0, sticky="ew")

midi_var = tk.StringVar(value=available_ports[0] if available_ports else NO_PORTS)
midi_dropdown = styled_dropdown(
    port_row, variable=midi_var, values=available_ports or [NO_PORTS], command=set_midi_port,
    state="normal" if available_ports else "disabled"
)
midi_dropdown.pack(side="left", fill="x", expand=True)

refresh_ports_btn = ctk.CTkButton(port_row, text="⟳", width=30, height=30, font=ctk.CTkFont(size=16),
                                  command=refresh_midi_ports, **SECONDARY_STYLE)
refresh_ports_btn.pack(side="left", padx=(6, 0))

# ---------- Hotkeys popup ----------
WHITE_NOTES = ["C", "D", "E", "F", "G", "A", "B", "C High"]
# Black key -> index of the white key to its left
BLACK_NOTES = {"C#": 0, "D#": 1, "F#": 3, "G#": 4, "A#": 5}
NOTE_DISPLAY_NAMES = {"C High": "High C"}

control_pairs = [
    ("Octave Down", "Octave Up"),
    ("Volume Down", "Volume Up"),
    ("Sustain", "Reset Volume"),
    ("MIDI Toggle", None),
]

def make_binding_button(parent, action, style, listening_style, **kwargs):
    btn = ctk.CTkButton(parent, text=format_key(key_bindings[action]), font=key_font,
                        command=lambda: start_rebind(action), **style, **kwargs)
    binding_buttons[action] = btn
    binding_styles[action] = style
    binding_listening_styles[action] = listening_style
    return btn

def build_hotkeys_content(parent):
    section_heading(0, "Notes", parent=parent)

    # ----- Notes (mini keyboard) -----
    piano = ctk.CTkFrame(parent, fg_color="transparent")
    piano.grid(row=1, column=0, sticky="new")
    # Each white key spans two grid columns, so black keys can sit halfway between them
    for c in range(len(WHITE_NOTES) * 2):
        piano.grid_columnconfigure(c, weight=1, uniform="piano")

    for note, left_index in BLACK_NOTES.items():
        column = left_index * 2 + 1
        field_label(piano, note).grid(row=0, column=column, columnspan=2)
        make_binding_button(piano, note, BLACK_KEY_STYLE, BLACK_KEY_LISTENING, width=34, height=36, corner_radius=6) \
            .grid(row=1, column=column, columnspan=2, padx=2, pady=(0, 4))

    for i, note in enumerate(WHITE_NOTES):
        make_binding_button(piano, note, WHITE_KEY_STYLE, WHITE_KEY_LISTENING, width=34, height=40, corner_radius=6) \
            .grid(row=2, column=i * 2, columnspan=2, padx=2)
        field_label(piano, NOTE_DISPLAY_NAMES.get(note, note)).grid(row=3, column=i * 2, columnspan=2)

    # ----- Controls -----
    section_heading(2, "Controls", parent=parent)

    controls = ctk.CTkFrame(parent, fg_color="transparent")
    controls.grid(row=3, column=0, sticky="new")
    controls.grid_columnconfigure((1, 3), weight=1, uniform="keys")

    for row, pair in enumerate(control_pairs):
        for slot, action in enumerate(pair):
            if action is None:
                continue
            ctk.CTkLabel(controls, text=action, font=body_font, text_color=LABEL_TEXT, anchor="w") \
                .grid(row=row, column=slot * 2, sticky="w", padx=(0 if slot == 0 else 14, 8), pady=3)
            make_binding_button(controls, action, SECONDARY_STYLE, CONTROL_LISTENING, width=48, height=28) \
                .grid(row=row, column=slot * 2 + 1, sticky="ew", pady=3)

    ctk.CTkButton(controls, text="Reset to defaults", font=body_font, height=28,
                  command=reset_bindings, **SECONDARY_STYLE) \
        .grid(row=len(control_pairs) - 1, column=2, columnspan=2, sticky="ew", padx=(14, 0), pady=3)

def open_hotkeys_window():
    global hotkeys_window
    if hotkeys_window is not None:
        hotkeys_window.deiconify()
        hotkeys_window.lift()
        hotkeys_window.focus_force()
        return

    hotkeys_window = ctk.CTkToplevel(root, fg_color=BG)
    hotkeys_window.title("Hotkeys")
    hotkeys_window.resizable(False, False)
    # Stays above the main window
    hotkeys_window.transient(root)
    hotkeys_window.protocol("WM_DELETE_WINDOW", close_hotkeys_window)

    frame = ctk.CTkFrame(hotkeys_window, fg_color="transparent")
    frame.pack(padx=20, pady=(12, 20))
    build_hotkeys_content(frame)

    # Center over the main window
    hotkeys_window.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - hotkeys_window.winfo_reqwidth()) // 2
    y = root.winfo_rooty() + (root.winfo_height() - hotkeys_window.winfo_reqheight()) // 2
    tk.Toplevel.wm_geometry(hotkeys_window, f"+{max(0, x)}+{max(0, y)}")
    hotkeys_window.focus_force()

def close_hotkeys_window():
    global hotkeys_window
    cancel_rebind()
    binding_buttons.clear()
    hotkeys_window.destroy()
    hotkeys_window = None

# ---------- Sustain ----------
def toggle_theme():
    mode = "light" if ctk.get_appearance_mode() == "Dark" else "dark"
    ctk.set_appearance_mode(mode)
    recolor_button_symbols()
    draw_theme_icon(theme_btn, sun=(mode == "dark"))
    octave_range.draw()

# Neutral, field-like styling so these don't compete with the green action buttons
TOOLBAR_STYLE = dict(height=30, corner_radius=6, fg_color=FIELD, hover_color=DIVIDER,
                     border_width=1, border_color=FIELD_BORDER, text_color=MUTED_TEXT)
theme_btn = ctk.CTkButton(port_heading, text="", width=30, command=toggle_theme, **TOOLBAR_STYLE)
theme_btn.pack(side="right")
hotkeys_btn = ctk.CTkButton(port_heading, text="Hotkeys", width=76, font=small_font,
                            command=open_hotkeys_window, **TOOLBAR_STYLE)
hotkeys_btn.pack(side="right", padx=(0, 6))
draw_theme_icon(theme_btn, sun=True)

def update_sustain_mode(event=None):
    global sustain_mode, hold_sustain_active
    reset_sustain()
    hold_sustain_active = False
    sustain_mode = sustain_var.get()


# ---------- Sound ----------
section_heading(2, "Sound", space_above=10, space_below=2)

sound = ctk.CTkFrame(content, fg_color="transparent")
sound.grid(row=3, column=0, sticky="new")

field_label(sound, "Sustain", color=LABEL_TEXT).pack(fill="x", pady=(0, 2))
sustain_var = tk.StringVar(value=SUSTAIN_TAP)
sustain_dropdown = styled_dropdown(sound, variable=sustain_var, values=[SUSTAIN_TAP, SUSTAIN_HOLD],
                                   command=update_sustain_mode)
sustain_dropdown.pack(fill="x")

def labelled_slider(parent, label, value_text, top_pad):
    header = ctk.CTkFrame(parent, fg_color="transparent")
    header.pack(fill="x", pady=(top_pad, 2))
    field_label(header, label, color=LABEL_TEXT).pack(side="left")
    value_label = ctk.CTkLabel(header, text=value_text, font=small_font, text_color=TEXT, height=18)
    value_label.pack(side="right")
    return value_label

def update_note_length(val):
    global note_length
    note_length = float(val)
    note_length_value_label.configure(text=f"{note_length:.2f} s")

note_length_value_label = labelled_slider(sound, "Note length", f"{note_length:.2f} s", top_pad=11)
note_length_slider = styled_slider(sound, from_=0, to=5, number_of_steps=100, command=update_note_length)
note_length_slider.set(note_length)
note_length_slider.pack(fill="x")

def set_velocity(val):
    global velocity_value
    velocity_value = max(1, min(127, int(val)))
    velocity_slider.set(velocity_value)
    velocity_value_label.configure(text=str(velocity_value))

def change_velocity(delta):
    set_velocity(velocity_value + delta)

def reset_velocity():
    set_velocity(DEFAULT_VELOCITY)

velocity_value_label = labelled_slider(sound, "Velocity (volume)", str(velocity_value), top_pad=7)
velocity_slider = styled_slider(sound, from_=1, to=127, number_of_steps=126, command=set_velocity)
velocity_slider.set(velocity_value)
velocity_slider.pack(fill="x")

# Key the C key plays in after transposing, spelled the way it's most common in sheet music
TRANSPOSED_KEY_NAMES = ["C", "D♭", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B"]

def format_transpose(amount):
    return f"{format_signed(amount)} [{TRANSPOSED_KEY_NAMES[amount % 12]}]"

def update_transpose(val):
    global transpose_amount
    transpose_amount = int(round(float(val)))
    transpose_value_label.configure(text=format_transpose(transpose_amount))

transpose_value_label = labelled_slider(sound, "Transpose", format_transpose(transpose_amount), top_pad=7)
transpose_slider = styled_slider(sound, from_=-12, to=12, number_of_steps=24, command=update_transpose)
transpose_slider.set(transpose_amount)
transpose_slider.pack(fill="x")

# ---------- Octave ----------
octave_colors = {
    -4: "#29325C",
    -3: "#435094",
    -2: "#6071BF",
    -1: "#93A5F5",
     0: "#6DCCA3",
     1: "#F28785",
     2: "#D15E5E",
     3: "#A63A3A",
     4: "#662424"
}

def update_octave_display():
    color = octave_colors.get(current_octave, MAIN_GREEN)
    octave_display.configure(fg_color=color)
    octave_display_label.configure(text=f"Octave  {format_signed(current_octave)}",
                                   text_color=readable_text_color(color))

def clamp_current_octave():
    global current_octave
    current_octave = max(min_octave, min(current_octave, max_octave))
    update_octave_display()

def set_octave_range(low, high):
    global min_octave, max_octave
    min_octave, max_octave = low, high
    octave_range_value_label.configure(text=f"{format_signed(low)} to {format_signed(high)}")
    clamp_current_octave()
    octave_range.draw()

def themed(color):
    # Resolve a (light, dark) pair for drawing on a plain Tk canvas
    if isinstance(color, (tuple, list)):
        return color[1] if ctk.get_appearance_mode() == "Dark" else color[0]
    return color

class OctaveRangeSelector:
    """
    A row of octave cells; the ones inside the playable range are lit in their octave's color.
    The edges of the lit block are handles, dragged to change the range, and pressing a cell
    outside the range stretches the nearest edge to it.
    Modelled on the octave swap range selector in SongCraft Instruments.
    """
    LOWEST, HIGHEST = -4, 4
    CELL_COUNT = HIGHEST - LOWEST + 1
    HEIGHT = 30
    # Room above, below and beside the cells for the handles
    MARGIN = 4
    # How far (in unscaled pixels) from a handle a press still grabs it
    HANDLE_REACH = 6

    def __init__(self, parent):
        # width=1: take the column's width rather than widening it
        self.canvas = tk.Canvas(parent, width=1, height=self.scaled(self.HEIGHT), highlightthickness=0, bd=0)
        self.dragging = None
        self.hovered = None
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", self.on_leave)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", lambda e: self.drag_to(e.x))
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

    @staticmethod
    def scaled(value):
        return value * ctk.ScalingTracker.get_widget_scaling(root)

    def cell_area(self):
        margin = self.scaled(self.MARGIN)
        left, right = margin, self.canvas.winfo_width() - margin
        top, bottom = margin, self.canvas.winfo_height() - margin
        return left, top, (right - left) / self.CELL_COUNT, bottom

    def edge_x(self, boundary):
        # x of the boundary between cells; boundary 0 is the left edge of the lowest octave
        left, _, cell_width, _ = self.cell_area()
        return left + boundary * cell_width

    def range_edges(self):
        return self.edge_x(min_octave - self.LOWEST), self.edge_x(max_octave - self.LOWEST + 1)

    # ----- Drawing -----

    def draw(self):
        c = self.canvas
        c.delete("all")
        background = themed(BG)
        c.configure(bg=background)
        left, top, cell_width, bottom = self.cell_area()
        if cell_width <= 0:
            return

        gap = self.scaled(2)
        font = ("Roboto", -round(self.scaled(11)), "bold")
        for i in range(self.CELL_COUNT):
            octave = self.LOWEST + i
            color = octave_colors[octave]
            # Muted tints, so the full-color octave display below stays the focus
            if min_octave <= octave <= max_octave:
                fill, text_color = blend(color, background, 0.5), themed(TEXT)
            else:
                fill, text_color = blend(color, background, 0.12), themed(MUTED_TEXT)
            x = left + i * cell_width
            c.create_rectangle(x + gap / 2, top, x + cell_width - gap / 2, bottom, fill=fill, outline="")
            c.create_text(x + cell_width / 2, (top + bottom) / 2, text=format_signed(octave),
                          fill=text_color, font=font)

        # Outline the range, with its edges as handles
        range_left, range_right = self.range_edges()
        line = themed(RANGE_OUTLINE)
        thickness = max(1, round(self.scaled(1.5)))
        c.create_line(range_left, top, range_right, top, fill=line, width=thickness)
        c.create_line(range_left, bottom, range_right, bottom, fill=line, width=thickness)

        active = self.dragging or self.hovered
        half = self.scaled(2)
        reach = self.scaled(self.MARGIN - 1)
        for handle, x in (("min", range_left), ("max", range_right)):
            color = MAIN_GREEN if handle == active else line
            c.create_rectangle(x - half, top - reach, x + half, bottom + reach, fill=color, outline="")

    # ----- Input -----

    def handle_at(self, x):
        range_left, range_right = self.range_edges()
        min_dist, max_dist = abs(x - range_left), abs(x - range_right)
        if min(min_dist, max_dist) > self.scaled(self.HANDLE_REACH):
            return None
        return "min" if min_dist <= max_dist else "max"

    def octave_at(self, x):
        left, _, cell_width, _ = self.cell_area()
        return max(0, min(int((x - left) // cell_width), self.CELL_COUNT - 1)) + self.LOWEST

    def on_motion(self, event):
        if self.dragging:
            return
        hovered = self.handle_at(event.x)
        self.canvas.configure(cursor="sb_h_double_arrow" if hovered else "")
        if hovered != self.hovered:
            self.hovered = hovered
            self.draw()

    def on_leave(self, event):
        if not self.dragging and self.hovered:
            self.hovered = None
            self.draw()

    def on_press(self, event):
        self.dragging = self.handle_at(event.x)
        if self.dragging is None:
            octave = self.octave_at(event.x)
            if octave < min_octave:
                self.dragging = "min"
            elif octave > max_octave:
                self.dragging = "max"
        if self.dragging:
            self.drag_to(event.x)

    def drag_to(self, x):
        if not self.dragging:
            return
        left, _, cell_width, _ = self.cell_area()
        # Snap to the nearest boundary between cells
        boundary = max(0, min(round((x - left) / cell_width), self.CELL_COUNT))
        low, high = min_octave, max_octave
        if self.dragging == "min":
            low = max(self.LOWEST, min(boundary + self.LOWEST, max_octave))
        else:
            high = min(self.HIGHEST, max(boundary + self.LOWEST - 1, min_octave))
        if (low, high) != (min_octave, max_octave):
            set_octave_range(low, high)
        else:
            self.draw()

    def on_release(self, event):
        self.dragging = None
        self.on_motion(event)
        self.draw()

octave_heading = section_heading(4, "Octave", space_above=10, space_below=4)
octave_range_value_label = ctk.CTkLabel(octave_heading, text=f"{format_signed(min_octave)} to {format_signed(max_octave)}",
                                        font=small_font, text_color=MUTED_TEXT, height=30)
octave_range_value_label.pack(side="right")

octave_section = ctk.CTkFrame(content, fg_color="transparent")
octave_section.grid(row=5, column=0, sticky="ew")

octave_range = OctaveRangeSelector(octave_section)
octave_range.canvas.pack(fill="x", pady=(0, 14))

octave_display = ctk.CTkFrame(octave_section, height=63.5, corner_radius=8, border_width=2, border_color=FIELD_BORDER)
octave_display.pack(fill="x")
octave_display.pack_propagate(False)
octave_display_label = ctk.CTkLabel(octave_display, text="", font=ctk.CTkFont(size=17, weight="bold"),
                                    fg_color="transparent")
octave_display_label.pack(expand=True, padx=4, pady=4)

update_octave_display()

# ---------- Start/Stop ----------
def update_toggle_button():
    # Show the toggle hotkey, since the hotkeys themselves are tucked away in their own window
    shortcut = format_key(key_bindings["MIDI Toggle"])
    if midi_mode:
        toggle_button.configure(text=f"Stop MIDI  ({shortcut})", state="normal",
                                fg_color=STOP_RED, hover_color=STOP_RED_HOVER)
    elif outport is None:
        toggle_button.configure(text="No MIDI port", state="disabled", fg_color=DISABLED)
    else:
        toggle_button.configure(text=f"Start MIDI  ({shortcut})", state="normal",
                                fg_color=MAIN_GREEN, hover_color=MAIN_GREEN_HOVER)

def on_port_changed():
    # Stop playing if the port went away, and only allow starting when a port is open
    if midi_mode and outport is None:
        toggle_mode()
    update_toggle_button()

toggle_button = ctk.CTkButton(content, height=44, font=ctk.CTkFont(size=16, weight="bold"),
                              text_color="#ffffff", text_color_disabled=DISABLED_TEXT, command=toggle_mode)
toggle_button.grid(row=6, column=0, sticky="ew", pady=(16, 0))
update_toggle_button()

# ---------- Global Hotkeys ----------
always_on_hotkeys = {
    "MIDI Toggle": toggle_mode,
}
# Only active (and suppressed from other apps) while MIDI is ON
midi_mode_hotkeys = {
    "Volume Down": lambda: change_velocity(-20),
    "Volume Up": lambda: change_velocity(20),
    "Reset Volume": reset_velocity,
}

def register_hotkeys(actions, suppress):
    for action, callback in actions.items():
        hotkey = key_bindings.get(action)
        if not hotkey:
            continue
        try:
            kb.add_hotkey(hotkey, on_ui_thread, args=(callback,), suppress=suppress)
        except Exception as e:
            print(f"Could not register hotkey {hotkey!r} for {action}: {e}")

def setup_global_hotkeys():
    register_hotkeys(always_on_hotkeys, suppress=False)
    if midi_mode:
        register_hotkeys(midi_mode_hotkeys, suppress=True)

def refresh_hotkeys():
    kb.unhook_all_hotkeys()
    setup_global_hotkeys()
    update_blocked_keys(midi_mode)

def on_close():
    cancel_rebind()
    # Closing the port also turns MIDI off, which stops the listener and unblocks keys
    close_midi_port()
    stop_main_listener()
    update_blocked_keys(False)
    kb.unhook_all()
    root.destroy()

setup_global_hotkeys()
root.protocol("WM_DELETE_WINDOW", on_close)

root.mainloop()
