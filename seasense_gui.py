"""
seasense_gui.py  -  SeaSense Light Tester
=========================================

A small stand-alone test GUI for DeepSea Power & Light LED SeaLite lights
(LSL-1000 and similar) using the SeaSense serial protocol.

How the program is organised (top to bottom in this file)
---------------------------------------------------------
1. Settings          - a few timing constants you might want to tweak.
2. SerialWorker      - ONE background thread that owns the serial port.
                       It takes commands from a queue, sends them one at a
                       time, waits for the reply, and reports back.  Only
                       this thread ever touches the port, so there are no
                       read/write collisions.
3. App               - the Tkinter window.  Buttons and the slider build
                       commands (using seasense_protocol.py) and drop them
                       on the worker's queue.  Every 30 ms the window checks
                       the worker's event queue and updates the logs.

Tkinter is not thread-safe, so the worker NEVER touches the GUI directly;
it only puts messages on a queue that the GUI reads.

Requires:  Python 3.8+ and pyserial  (pip install pyserial)
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import serial
import serial.tools.list_ports

import seasense_protocol as sp

APP_TITLE = "SeaSense Light Tester"
APP_VERSION = "1.0-dev"   # replaced with the release number (e.g. 1.0.5) by the GitHub build

# --------------------------------------------------------------------------
# 1. Settings
# --------------------------------------------------------------------------

REPLY_TIMEOUT = 0.5    # seconds to wait for the light to START replying
QUIET_GAP = 0.05       # seconds of silence that means "reply finished"
                       # (needed because some replies end in <CR> only, and
                       # STAT / PSET replies are several lines long)
MAX_REPLY_TIME = 2.0   # hard limit for one reply, just in case
SCAN_TIMEOUT = 0.15    # shorter first-byte timeout used by "Scan bus"
SLIDER_PERIOD_MS = 20  # how often the slider checks whether to send
DEFAULT_SETTLE_MS = 250  # slider must be still this long before sending
                         # (changeable in the window; 0 = every 1 % step)
GUI_POLL_MS = 30       # how often the window checks for worker events


# --------------------------------------------------------------------------
# 2. SerialWorker - the only code that talks to the serial port
# --------------------------------------------------------------------------

def make_request(data, **extra):
    """Wrap bytes to send in a request dictionary for the worker, noting
    which command it is so the reply can be decoded later."""
    address, command, access = sp.identify_command(data)
    request = {"data": data, "command": command, "access": access,
               "reply_expected": sp.expects_reply(address)}
    request.update(extra)
    return request


class SerialWorker(threading.Thread):
    """Background thread: send one command, collect its reply, repeat.

    Requests are plain dictionaries put on self.requests:
        {
          "data":      bytes to send (required),
          "timeout":   first-byte timeout (optional),
          "new_baud":  switch the port to this baud after the light ACKs (optional),
          "followup":  function(reply_bytes) -> bytes or None; if it returns
                       bytes they are sent IMMEDIATELY with nothing in between
                       (used by the factory-reset key exchange) (optional),
          ...anything else is passed back untouched for the GUI to use.
        }

    Results go back to the GUI on self.events as tuples:
        ("tx", request)                         - bytes were written
        ("rx", request, echo_bytes, reply_bytes) - reply (may be empty)
        ("unsolicited", bytes)                  - bytes nobody asked for
        ("baud", new_baud)                      - port baud rate changed
        ("error", message)                      - port failed; worker stopped
    """

    def __init__(self, port_name, baud):
        super().__init__(daemon=True)
        # Open the port here so the caller gets the error immediately.
        # 8 data bits, no parity, 1 stop bit, no flow control (spec 3.1).
        self.port = serial.Serial(port_name, baud, bytesize=8, parity="N",
                                  stopbits=1, timeout=0, xonxoff=False,
                                  rtscts=False, dsrdtr=False)
        self.requests = queue.Queue()
        self.events = queue.Queue()
        self.running = True
        self.busy = False          # True while a command/reply is in progress
        self.strip_echo = True     # mirrored from the GUI checkbox

    # ---- main loop -------------------------------------------------------
    def run(self):
        while self.running:
            try:
                request = self.requests.get(timeout=0.05)
            except queue.Empty:
                self._read_unsolicited()
                continue

            self.busy = True
            try:
                self._transact(request)
            except (serial.SerialException, OSError) as error:
                self.events.put(("error", "Serial port error: %s" % error))
                self.running = False
            finally:
                self.busy = False

        try:
            self.port.close()
        except Exception:
            pass

    def stop(self):
        self.running = False

    def clear_pending(self):
        """Throw away any commands that have not been sent yet."""
        try:
            while True:
                self.requests.get_nowait()
        except queue.Empty:
            pass

    # ---- one command / reply ---------------------------------------------
    def _transact(self, request):
        data = request["data"]
        self.port.reset_input_buffer()      # forget any old junk
        self.port.write(data)
        self.port.flush()
        self.events.put(("tx", request))

        raw = self._collect(data, request.get("timeout", REPLY_TIMEOUT))
        echo, reply = sp.strip_echo(data, raw) if self.strip_echo else (b"", raw)
        self.events.put(("rx", request, echo, reply))

        # Baud rate change: the light ACKs at the OLD rate, then switches.
        # (Broadcast/group commands get no ACK, so switch anyway for those.)
        new_baud = request.get("new_baud")
        if new_baud and (sp.is_ack(reply) or not request.get("reply_expected", True)):
            time.sleep(0.1)
            self.port.baudrate = new_baud
            self.events.put(("baud", new_baud))

        # Follow-up command that must go out immediately (factory reset)
        followup = request.get("followup")
        if followup:
            next_data = followup(reply)
            if next_data:
                self._transact(make_request(next_data))

    def _collect(self, sent, first_timeout):
        """Read bytes until the line has been quiet for QUIET_GAP.

        If all we have so far is our own echo, keep waiting for the real
        reply (up to the first-byte timeout).
        """
        start = time.monotonic()
        buffer = b""
        last_byte_time = None
        while True:
            now = time.monotonic()
            waiting = self.port.in_waiting
            if waiting:
                buffer += self.port.read(waiting)
                last_byte_time = now
            else:
                # Keep waiting if all we have is our own echo, or only the
                # light's "!" + LF prefix (the real reply may still follow).
                rest = buffer[len(sent):] if self.strip_echo and buffer.startswith(sent) else buffer
                only_echo = rest == b"" or rest == sp.LIGHT_PREFIX
                if buffer and not only_echo and now - last_byte_time >= QUIET_GAP:
                    return buffer                    # reply complete
                if (not buffer or only_echo) and now - start >= first_timeout:
                    return buffer                    # no reply
            if now - start >= MAX_REPLY_TIME:
                return buffer                        # safety limit
            time.sleep(0.002)

    def _read_unsolicited(self):
        """SeaSense lights never talk unless asked, but show anything that
        arrives anyway (noise, another host on the RS-485 bus, etc.)."""
        if self.port.in_waiting:
            time.sleep(QUIET_GAP)
            self.events.put(("unsolicited", self.port.read(self.port.in_waiting)))


# --------------------------------------------------------------------------
# Dimming curve graph (used on the "Dimming curve" tab)
# --------------------------------------------------------------------------

class CurveGraph(tk.Canvas):
    """A small graph of the dimming curve with draggable set points.

    - A, B, C, D: round handles under the x axis, drag left/right.
    - YMIN, YMAX: square handles left of the y axis, drag up/down.
    Dragging updates the shared StringVars; typing in the number boxes
    redraws the graph (via trace_add), so both stay in step.
    """

    WIDTH, HEIGHT = 440, 290
    LEFT, RIGHT, TOP, BOTTOM = 44, 30, 12, 46   # margins around the plot

    def __init__(self, parent, curve_vars, current_output):
        super().__init__(parent, width=self.WIDTH, height=self.HEIGHT,
                         background="white", highlightthickness=1,
                         highlightbackground="#c0bfbc")
        self.vars = curve_vars                 # YMIN, YMAX, A, B, C, D, e
        self.current_output = current_output   # function -> LOUT or None
        self.dragging = None                   # index of the var being dragged
        for var in self.vars:
            var.trace_add("write", lambda *args: self.redraw())
        self.bind("<ButtonPress-1>", self.on_press)
        self.bind("<B1-Motion>", self.on_drag)
        self.bind("<ButtonRelease-1>", lambda event: setattr(self, "dragging", None))

    # ---- coordinate conversion (0-100 % <-> pixels) -----------------------
    def plot_width(self):
        return self.WIDTH - self.LEFT - self.RIGHT

    def plot_height(self):
        return self.HEIGHT - self.TOP - self.BOTTOM

    def px_x(self, x):
        return self.LEFT + x / 100.0 * self.plot_width()

    def px_y(self, y):
        return self.TOP + (1 - y / 100.0) * self.plot_height()

    def y_handle_x(self):
        """x pixel of the YMIN / YMAX drag handles (just right of the plot)."""
        return self.px_x(100) + 15

    def value_x(self, px):
        return max(0, min(100, round((px - self.LEFT) / self.plot_width() * 100)))

    def value_y(self, py):
        return max(0, min(100, round((1 - (py - self.TOP) / self.plot_height()) * 100)))

    # ---- reading the parameters ------------------------------------------
    def params(self):
        """Return [ymin, ymax, a, b, c, d, e] as numbers, or None if a box
        is empty or not a number (e.g. while the user is typing)."""
        try:
            return [float(var.get()) for var in self.vars]
        except ValueError:
            return None

    # ---- drawing ---------------------------------------------------------
    def redraw(self):
        self.delete("all")
        left, top = self.LEFT, self.TOP
        right, bottom = self.px_x(100), self.px_y(0)

        # Grid and axis labels
        for step in range(0, 101, 20):
            self.create_line(self.px_x(step), top, self.px_x(step), bottom, fill="#eeeeec")
            self.create_line(left, self.px_y(step), right, self.px_y(step), fill="#eeeeec")
            self.create_text(self.px_x(step), bottom + 6, text=str(step), anchor="n",
                             font=("TkDefaultFont", 7), fill="#77767b")
            self.create_text(left - 4, self.px_y(step), text=str(step), anchor="e",
                             font=("TkDefaultFont", 7), fill="#77767b")
        self.create_rectangle(left, top, right, bottom, outline="#9a9996")
        self.create_text((left + right) / 2, self.HEIGHT - 2, anchor="s",
                         text="Control input %  (analog / dimmer)", font=("TkDefaultFont", 8))
        self.create_text(10, (top + bottom) / 2, text="Output %", angle=90,
                         font=("TkDefaultFont", 8))

        values = self.params()
        if values is None:
            self.create_text((left + right) / 2, (top + bottom) / 2,
                             text="Enter numbers in all 7 boxes", fill="#c01c28")
            return
        ymin, ymax, a, b, c, d, e = values
        valid = 0 <= a <= b <= c <= d <= 100 and 1 <= ymin < ymax <= 100 and 1 <= e <= 6

        # Dashed guide lines for the set points
        for x, name in ((a, "A"), (b, "B"), (c, "C"), (d, "D")):
            self.create_line(self.px_x(x), top, self.px_x(x), bottom, fill="#99c1f1", dash=(3, 3))
        for y in (ymin, ymax):
            self.create_line(left, self.px_y(y), right, self.px_y(y), fill="#f8b6b6", dash=(3, 3))

        # The curve itself, one point per 0.5 % of input
        points = []
        for step in range(0, 201):
            x = step / 2.0
            points += [self.px_x(x), self.px_y(sp.curve_output(x, *values))]
        self.create_line(points, fill="#1a5fb4" if valid else "#c01c28", width=2)
        if not valid:
            self.create_text(right - 4, top + 4, anchor="ne", fill="#c01c28",
                             text="Invalid: needs A<=B<=C<=D, YMIN<YMAX, e 1-6")

        # Where the current LOUT setting sits (serial: LOUT 1 = B, 100 = D)
        lout = self.current_output()
        if lout:
            x = b + (lout - 1) / 99.0 * (d - b)
            y = sp.curve_output(x, *values)
            self.create_oval(self.px_x(x) - 5, self.px_y(y) - 5, self.px_x(x) + 5,
                             self.px_y(y) + 5, fill="#ff7800", outline="")

        # Handles: A-D under the x axis, YMIN/YMAX right of the plot
        for x, name in ((a, "A"), (b, "B"), (c, "C"), (d, "D")):
            cx, cy = self.px_x(x), bottom + 26
            self.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, fill="#3584e4", outline="")
            self.create_text(cx, cy, text=name, fill="white", font=("TkDefaultFont", 7, "bold"))
        for y, name, anchor in ((ymin, "YMIN %g" % ymin, "sw"), (ymax, "YMAX %g" % ymax, "nw")):
            cx, cy = self.y_handle_x(), self.px_y(y)
            self.create_rectangle(cx - 6, cy - 6, cx + 6, cy + 6, fill="#e01b24", outline="")
            self.create_text(left + 3, cy + (-2 if anchor == "sw" else 2), text=name,
                             anchor=anchor, fill="#e01b24", font=("TkDefaultFont", 7))

    # ---- dragging --------------------------------------------------------
    def on_press(self, event):
        """Pick the handle nearest the click (within 12 px)."""
        values = self.params()
        if values is None:
            return
        ymin, ymax, a, b, c, d, e = values
        handles = [(2, self.px_x(a), self.px_y(0) + 26), (3, self.px_x(b), self.px_y(0) + 26),
                   (4, self.px_x(c), self.px_y(0) + 26), (5, self.px_x(d), self.px_y(0) + 26),
                   (0, self.y_handle_x(), self.px_y(ymin)), (1, self.y_handle_x(), self.px_y(ymax))]
        best = None
        for index, hx, hy in handles:
            distance = abs(event.x - hx) + abs(event.y - hy)
            if distance < 12 and (best is None or distance < best[0]):
                best = (distance, index)
        self.dragging = best[1] if best else None

    def on_drag(self, event):
        """Move the grabbed handle, keeping A<=B<=C<=D and YMIN<YMAX."""
        if self.dragging is None:
            return
        values = self.params()
        ymin, ymax, a, b, c, d, e = values
        index = self.dragging
        if index in (2, 3, 4, 5):                       # A, B, C or D
            points = [a, b, c, d]
            position = index - 2
            low = points[position - 1] if position > 0 else 0
            high = points[position + 1] if position < 3 else 100
            new = max(low, min(high, self.value_x(event.x)))
        elif index == 0:                                # YMIN
            new = max(1, min(ymax - 1, self.value_y(event.y)))
        else:                                           # YMAX
            new = max(ymin + 1, min(100, self.value_y(event.y)))
        self.vars[index].set(str(int(new)))


# --------------------------------------------------------------------------
# 3. App - the window
# --------------------------------------------------------------------------

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("%s v%s" % (APP_TITLE, APP_VERSION))
        self.geometry("1340x820")
        self.minsize(1050, 650)

        self.worker = None            # SerialWorker while connected
        self.slider_pending = None    # value waiting to be sent by the slider
        self.known_output = None      # last output level sent to / read from light
        self.raw_history = []         # commands typed in the raw box
        self.history_index = 0
        self.scanning = False

        self._build_connection_bar()
        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=0)
        main.add(right, weight=1)
        self._build_control_tabs(left)
        self._build_log_panels(right)

        self.refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Start the repeating timers
        self.after(GUI_POLL_MS, self.process_worker_events)
        self.after(SLIDER_PERIOD_MS, self.slider_tick)
        self.after(1000, self.autopoll_tick)

    # ======================================================================
    # Window layout
    # ======================================================================

    def _build_connection_bar(self):
        """Top bar: port, baud, connect, node address, options."""
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill=tk.X)

        # --- Port and baud ---
        ttk.Label(bar, text="COM port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(bar, textvariable=self.port_var, width=34)
        self.port_box.grid(row=0, column=1, padx=3)
        ttk.Button(bar, text="Refresh", command=self.refresh_ports).grid(row=0, column=2)

        ttk.Label(bar, text="Baud:").grid(row=0, column=3, padx=(12, 0))
        self.baud_var = tk.StringVar(value="9600")
        ttk.Combobox(bar, textvariable=self.baud_var, width=8,
                     values=[str(b) for b in sp.BAUD_RATES]).grid(row=0, column=4, padx=3)

        self.connect_button = ttk.Button(bar, text="Connect", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=5, padx=6)
        self.status_label = ttk.Label(bar, text="Disconnected", foreground="gray")
        self.status_label.grid(row=0, column=6, padx=6, sticky="w")

        # --- Node address (change this to talk to a different light) ---
        ttk.Label(bar, text="Node address:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.address_var = tk.StringVar(value="001")
        ttk.Spinbox(bar, textvariable=self.address_var, from_=0, to=332,
                    format="%03.0f", width=6).grid(row=1, column=1, sticky="w", padx=3, pady=(6, 0))
        ttk.Label(bar, text="001-255 = one light   000 = all lights (no reply)   301-332 = group",
                  foreground="gray").grid(row=1, column=1, columnspan=4, sticky="e", pady=(6, 0))
        ttk.Button(bar, text="Scan bus", command=self.scan_bus).grid(row=1, column=5, pady=(6, 0))
        ttk.Button(bar, text="Stop / clear queue", command=self.stop_queue).grid(
            row=1, column=6, sticky="w", padx=6, pady=(6, 0))

        # --- Options ---
        options = ttk.Frame(bar)
        options.grid(row=0, column=7, rowspan=2, padx=(20, 0), sticky="w")
        self.checksum_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="Add checksum (*XX) to button commands",
                        variable=self.checksum_var).pack(anchor="w")
        self.echo_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="Hide RS-485 adapter echo",
                        variable=self.echo_var, command=self.echo_changed).pack(anchor="w")

    def _build_control_tabs(self, parent):
        """Left side: a tab for each group of commands."""
        tabs = ttk.Notebook(parent, width=470)
        tabs.pack(fill=tk.BOTH, expand=True)
        for title, builder in [("Light", self._tab_light),
                               ("Status", self._tab_status),
                               ("Presets", self._tab_presets),
                               ("Dimming curve", self._tab_curve),
                               ("Setup", self._tab_setup),
                               ("Dual / Strobe", self._tab_options)]:
            frame = ttk.Frame(tabs, padding=8)
            tabs.add(frame, text=title)
            builder(frame)

    # ---- Light tab -------------------------------------------------------
    def _tab_light(self, tab):
        box = ttk.LabelFrame(tab, text="Light output (LOUT)", padding=8)
        box.pack(fill=tk.X)

        # The slider: 0-100 in 1 % steps.  Moving it only records the value;
        # slider_tick() sends it once the slider has been still for the
        # settle time (and the previous command has been answered).
        self.slider = tk.Scale(box, from_=0, to=100, orient=tk.HORIZONTAL,
                               length=360, resolution=1, label="Output %",
                               command=self.slider_moved)
        self.slider.pack(fill=tk.X)

        row = ttk.Frame(box)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Send after slider settles for").pack(side=tk.LEFT)
        self.settle_ms_var = tk.StringVar(value=str(DEFAULT_SETTLE_MS))
        ttk.Spinbox(row, textvariable=self.settle_ms_var, from_=0, to=2000,
                    increment=50, width=6).pack(side=tk.LEFT, padx=3)
        ttk.Label(row, text="ms (0 = every 1 % step)", foreground="gray").pack(side=tk.LEFT)

        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=4)
        for level in (0, 10, 25, 50, 75, 100):
            ttk.Button(row, text="Off" if level == 0 else "%d%%" % level, width=5,
                       command=lambda v=level: self.set_output(v)).pack(side=tk.LEFT, padx=1)

        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=4)
        ttk.Button(row, text="- coarse", command=lambda: self.step_output("-", "")).pack(side=tk.LEFT, padx=1)
        ttk.Button(row, text="- 1", width=5, command=lambda: self.step_output("-", "1")).pack(side=tk.LEFT, padx=1)
        ttk.Button(row, text="+ 1", width=5, command=lambda: self.step_output("+", "1")).pack(side=tk.LEFT, padx=1)
        ttk.Button(row, text="+ coarse", command=lambda: self.step_output("+", "")).pack(side=tk.LEFT, padx=1)
        ttk.Button(row, text="Read output", command=lambda: self.send("lout", "?")).pack(side=tk.RIGHT)

        box = ttk.LabelFrame(tab, text="Standby (STBY)", padding=8)
        box.pack(fill=tk.X, pady=8)
        ttk.Button(box, text="Enter standby", command=lambda: self.send("stby", "=", 1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(box, text="Exit standby (wake)", command=lambda: self.send("stby", "=", 0)).pack(side=tk.LEFT, padx=2)
        ttk.Button(box, text="Read", command=lambda: self.send("stby", "?")).pack(side=tk.RIGHT)

        ttk.Label(tab, foreground="gray", wraplength=450, justify=tk.LEFT, text=(
            "The slider sends !ADR:lout=N. With a settle time it sends only "
            "where you stop; with 0 it sends each 1 % step as fast as the "
            "light answers. LOUT=1 is the lowest lit level, 0 is off.")).pack(anchor="w", pady=4)

    # ---- Status tab ------------------------------------------------------
    def _tab_status(self, tab):
        row = ttk.Frame(tab)
        row.pack(fill=tk.X)
        for text, command in [("Info", "info"), ("Status", "stat"),
                              ("Temperature", "temp"), ("Humidity", "rhum")]:
            ttk.Button(row, text=text, command=lambda c=command: self.send(c, "?")).pack(side=tk.LEFT, padx=1)
        ttk.Button(row, text="Read all", command=self.read_all).pack(side=tk.RIGHT)

        # Summary table: the latest decoded value of each item
        box = ttk.LabelFrame(tab, text="Latest values from this light", padding=8)
        box.pack(fill=tk.X, pady=8)
        self.summary = {}
        items = ["Model", "Serial number", "Firmware version", "LED color",
                 "Beam pattern", "Dimming control", "Internal temperature",
                 "Internal humidity", "Light output", "Standby", "RunTime",
                 "PrCycle", "MaxTemp", "Humid", "FLAGS"]
        for row_number, name in enumerate(items):
            ttk.Label(box, text=name + ":").grid(row=row_number, column=0, sticky="w")
            value = ttk.Label(box, text="-", foreground="#1a5fb4")
            value.grid(row=row_number, column=1, sticky="w", padx=8)
            self.summary[name] = value

        box = ttk.Frame(tab)
        box.pack(fill=tk.X)
        self.autopoll_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Auto-poll temp / humidity / output / standby every",
                        variable=self.autopoll_var).pack(side=tk.LEFT)
        self.autopoll_seconds = tk.StringVar(value="5")
        ttk.Spinbox(box, textvariable=self.autopoll_seconds, from_=1, to=600, width=4).pack(side=tk.LEFT)
        ttk.Label(box, text="s").pack(side=tk.LEFT)

    # ---- Presets tab -----------------------------------------------------
    def _tab_presets(self, tab):
        box = ttk.LabelFrame(tab, text="User presets (PSET) - saves STBY and LOUT", padding=8)
        box.pack(fill=tk.X)
        self.preset_var = tk.StringVar(value="1")
        ttk.Label(box, text="Preset #").grid(row=0, column=0)
        ttk.Spinbox(box, textvariable=self.preset_var, from_=1, to=16, width=4).grid(row=0, column=1, padx=4)
        ttk.Button(box, text="Recall", command=lambda: self.send("pset", "=", self.preset_var.get())).grid(row=0, column=2, padx=1)
        ttk.Button(box, text="Save current", command=self.save_preset).grid(row=0, column=3, padx=1)
        ttk.Button(box, text="Clear", command=self.clear_preset).grid(row=0, column=4, padx=1)
        ttk.Button(box, text="Read all presets", command=lambda: self.send("pset", "?")).grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        box = ttk.LabelFrame(tab, text="Power-on boot preset (BOOT)", padding=8)
        box.pack(fill=tk.X, pady=8)
        self.boot_var = tk.StringVar(value="0")
        ttk.Label(box, text="Boot with preset #").grid(row=0, column=0)
        ttk.Spinbox(box, textvariable=self.boot_var, from_=0, to=16, width=4).grid(row=0, column=1, padx=4)
        ttk.Button(box, text="Set", command=lambda: self.send("boot", "=", self.boot_var.get())).grid(row=0, column=2, padx=1)
        ttk.Button(box, text="Read", command=lambda: self.send("boot", "?")).grid(row=0, column=3, padx=1)
        ttk.Label(box, text="0 = factory default", foreground="gray").grid(row=1, column=0, columnspan=4, sticky="w")

    # ---- Dimming curve tab -----------------------------------------------
    def _tab_curve(self, tab):
        # One StringVar per curve parameter, in spec order:
        # YMIN, YMAX, A, B, C, D, e.  The graph and the number boxes both
        # read and write these, so they always agree.
        self.curve_vars = [tk.StringVar() for _ in sp.CURVE_NAMES]

        self.curve_graph = CurveGraph(tab, self.curve_vars,
                                      current_output=lambda: self.known_output)
        self.curve_graph.pack(anchor="w")

        boxes = ttk.Frame(tab)
        boxes.pack(anchor="w", pady=4)
        short_names = ["YMIN", "YMAX", "A", "B", "C", "D", "e"]
        for index, (name, var) in enumerate(zip(short_names, self.curve_vars)):
            ttk.Label(boxes, text=name).grid(row=0, column=index, padx=2)
            if name == "e":     # exponent: 1-6 in 0.1 steps
                ttk.Spinbox(boxes, textvariable=var, from_=1, to=6, increment=0.1,
                            width=5).grid(row=1, column=index, padx=2)
            else:
                ttk.Entry(boxes, textvariable=var, width=5).grid(row=1, column=index, padx=2)
        self.fill_curve(sp.DEFAULT_CURVE)

        row = ttk.Frame(tab)
        row.pack(anchor="w", pady=4)
        ttk.Button(row, text="Read from light",
                   command=lambda: self.send("curv", "?", on_reply=self.curve_read)).pack(side=tk.LEFT, padx=1)
        ttk.Button(row, text="Send curve", command=self.write_curve).pack(side=tk.LEFT, padx=1)
        ttk.Button(row, text="Default", command=lambda: self.fill_curve(sp.DEFAULT_CURVE)).pack(side=tk.LEFT, padx=1)
        ttk.Button(row, text="Linear", command=lambda: self.fill_curve(sp.LINEAR_CURVE)).pack(side=tk.LEFT, padx=1)
        ttk.Label(tab, foreground="gray", wraplength=450, justify=tk.LEFT, text=(
            "Drag A-D along the bottom and the red YMIN / YMAX squares up and down. "
            "The line is an approximation fitted to the spec's Figure 2 "
            "(no exact formula is published). Serial dimming uses only B..D: LOUT 1 = B, LOUT 100 = D; "
            "the orange dot is the current LOUT. Rules: A<=B<=C<=D, "
            "YMAX>YMIN, e 1-6.")).pack(anchor="w")

    # ---- Setup tab -------------------------------------------------------
    def _tab_setup(self, tab):
        box = ttk.LabelFrame(tab, text="Change this light's node address (ADDR)", padding=8)
        box.pack(fill=tk.X)
        self.new_address_var = tk.StringVar(value="002")
        ttk.Label(box, text="New address").pack(side=tk.LEFT)
        ttk.Spinbox(box, textvariable=self.new_address_var, from_=1, to=255,
                    format="%03.0f", width=6).pack(side=tk.LEFT, padx=4)
        ttk.Button(box, text="Change", command=self.change_address).pack(side=tk.LEFT, padx=2)
        ttk.Button(box, text="Read", command=lambda: self.send("addr", "?")).pack(side=tk.RIGHT)

        box = ttk.LabelFrame(tab, text="Group addresses (ADGR)", padding=8)
        box.pack(fill=tk.X, pady=8)
        self.group_var = tk.StringVar(value="301")
        ttk.Label(box, text="Group").pack(side=tk.LEFT)
        ttk.Spinbox(box, textvariable=self.group_var, from_=301, to=332, width=5).pack(side=tk.LEFT, padx=4)
        ttk.Button(box, text="Add", command=lambda: self.send("adgr", "+", self.group_var.get())).pack(side=tk.LEFT, padx=1)
        ttk.Button(box, text="Remove", command=lambda: self.send("adgr", "-", self.group_var.get())).pack(side=tk.LEFT, padx=1)
        ttk.Button(box, text="Read", command=lambda: self.send("adgr", "?")).pack(side=tk.RIGHT)

        box = ttk.LabelFrame(tab, text="Change the light's baud rate (BAUD)", padding=8)
        box.pack(fill=tk.X)
        self.new_baud_var = tk.StringVar(value="9600")
        ttk.Combobox(box, textvariable=self.new_baud_var, width=8, state="readonly",
                     values=[str(b) for b in sp.BAUD_RATES]).pack(side=tk.LEFT)
        ttk.Button(box, text="Change", command=self.change_baud).pack(side=tk.LEFT, padx=4)
        ttk.Button(box, text="Read", command=lambda: self.send("baud", "?")).pack(side=tk.RIGHT)

        box = ttk.LabelFrame(tab, text="Factory reset (RSET)", padding=8)
        box.pack(fill=tk.X, pady=8)
        ttk.Label(box, wraplength=440, justify=tk.LEFT, foreground="#a51d2d", text=(
            "Resets address to factory, baud, groups, curve and ERASES all "
            "presets. The app reads the one-time key and sends it back.")).pack(anchor="w")
        ttk.Button(box, text="Factory reset this light...", command=self.factory_reset).pack(anchor="w", pady=4)

    # ---- Optional features tab ------------------------------------------
    def _tab_options(self, tab):
        ttk.Label(tab, foreground="gray", wraplength=450, justify=tk.LEFT, text=(
            "Only for lights built with these options. A standard single-"
            "output LSL-1000 will answer NAK - that is normal.")).pack(anchor="w")

        box = ttk.LabelFrame(tab, text="Dual output channel (CHSW)", padding=8)
        box.pack(fill=tk.X, pady=6)
        self.channel_var = tk.StringVar(value="1")
        for value, text in [("1", "Ch 1"), ("2", "Ch 2"), ("3", "Both")]:
            ttk.Radiobutton(box, text=text, value=value, variable=self.channel_var).pack(side=tk.LEFT)
        ttk.Button(box, text="Set", command=lambda: self.send("chsw", "=", self.channel_var.get())).pack(side=tk.LEFT, padx=4)
        ttk.Button(box, text="Read", command=lambda: self.send("chsw", "?")).pack(side=tk.RIGHT)

        box = ttk.LabelFrame(tab, text="Strobe", padding=8)
        box.pack(fill=tk.X)
        # (label, command, choices or None for a number, default)
        strobe_rows = [
            ("Mode (PMOD)", "pmod", ["0 = Off", "1 = Input Follow", "2 = Pulse Trigger"], "0 = Off"),
            ("Edge (PEDG)", "pedg", ["0 = Falling", "1 = Rising"], "1 = Rising"),
            ("Invert (PINV)", "pinv", ["0 = ON at edge", "1 = OFF at edge"], "1 = OFF at edge"),
            ("Pulse ms (PLEN)", "plen", None, "8"),
        ]
        for row_number, (label, command, choices, default) in enumerate(strobe_rows):
            ttk.Label(box, text=label).grid(row=row_number, column=0, sticky="w")
            var = tk.StringVar(value=default)
            if choices:
                ttk.Combobox(box, textvariable=var, values=choices, state="readonly",
                             width=18).grid(row=row_number, column=1, padx=4, pady=1)
            else:
                ttk.Spinbox(box, textvariable=var, from_=2, to=1000, width=6).grid(
                    row=row_number, column=1, padx=4, pady=1, sticky="w")
            # var.get().split()[0] turns "1 = Rising" into "1"
            ttk.Button(box, text="Set", width=5, command=lambda c=command, v=var:
                       self.send(c, "=", v.get().split()[0])).grid(row=row_number, column=2)
            ttk.Button(box, text="Read", width=5, command=lambda c=command:
                       self.send(c, "?")).grid(row=row_number, column=3, padx=2)

    # ---- Right side: raw log, parsed data, raw send box -------------------
    def _build_log_panels(self, parent):
        panes = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True)

        # Raw traffic
        raw_frame = ttk.LabelFrame(panes, text="Raw serial traffic", padding=4)
        panes.add(raw_frame, weight=3)
        tools = ttk.Frame(raw_frame)
        tools.pack(fill=tk.X)
        self.hex_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tools, text="Also show hex", variable=self.hex_var).pack(side=tk.LEFT)
        ttk.Button(tools, text="Save log...", command=self.save_log).pack(side=tk.RIGHT)
        ttk.Button(tools, text="Clear", command=lambda: self.clear_text(self.raw_text)).pack(side=tk.RIGHT, padx=4)
        self.raw_text = scrolledtext.ScrolledText(raw_frame, height=16, font="TkFixedFont",
                                                  wrap=tk.CHAR, state=tk.DISABLED)
        self.raw_text.pack(fill=tk.BOTH, expand=True)
        self.raw_text.tag_config("tx", foreground="#1a5fb4")
        self.raw_text.tag_config("rx", foreground="#26a269")
        self.raw_text.tag_config("echo", foreground="#9a9996")
        self.raw_text.tag_config("note", foreground="#9a9996")
        self.raw_text.tag_config("error", foreground="#c01c28")

        # Send raw string
        send_frame = ttk.Frame(raw_frame)
        send_frame.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(send_frame, text="Send:").pack(side=tk.LEFT)
        self.raw_entry_var = tk.StringVar(value="!001:info?")
        entry = ttk.Entry(send_frame, textvariable=self.raw_entry_var, font="TkFixedFont")
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        entry.bind("<Return>", lambda event: self.send_raw())
        entry.bind("<Up>", lambda event: self.raw_history_step(-1))
        entry.bind("<Down>", lambda event: self.raw_history_step(1))
        ttk.Button(send_frame, text="Send", command=self.send_raw).pack(side=tk.LEFT)

        opts = ttk.Frame(raw_frame)
        opts.pack(fill=tk.X)
        # Type just the ASCII part; this box adds \r\n as the message leaves.
        self.raw_eof_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Append \\r\\n (CR LF) when sending",
                        variable=self.raw_eof_var).pack(side=tk.LEFT)
        ttk.Label(opts, foreground="gray", text=(
            "Otherwise sent exactly as typed (\\r \\n <CR> [0x0D] also work).  "
            "Up/Down = history")).pack(side=tk.LEFT, padx=8)

        # Parsed replies
        parsed_frame = ttk.LabelFrame(panes, text="Parsed replies", padding=4)
        panes.add(parsed_frame, weight=2)
        tools = ttk.Frame(parsed_frame)
        tools.pack(fill=tk.X)
        ttk.Button(tools, text="Clear", command=lambda: self.clear_text(self.parsed_text)).pack(side=tk.RIGHT)
        self.parsed_text = scrolledtext.ScrolledText(parsed_frame, height=10, font="TkFixedFont",
                                                     wrap=tk.WORD, state=tk.DISABLED)
        self.parsed_text.pack(fill=tk.BOTH, expand=True)
        self.parsed_text.tag_config("head", foreground="#1a5fb4")
        self.parsed_text.tag_config("ack", foreground="#26a269")
        self.parsed_text.tag_config("bad", foreground="#c01c28")

    # ======================================================================
    # Connection
    # ======================================================================

    def refresh_ports(self):
        """Fill the COM port dropdown with what Windows currently sees."""
        ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)
        self.port_box["values"] = ["%s - %s" % (p.device, p.description) for p in ports]
        if ports and not self.port_var.get():
            self.port_box.current(0)

    def toggle_connection(self):
        if self.worker:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        port_name = self.port_var.get().split(" - ")[0].strip()
        if not port_name:
            messagebox.showwarning(APP_TITLE, "Pick a COM port first.")
            return
        try:
            baud = int(self.baud_var.get())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Baud rate must be a number.")
            return
        try:
            self.worker = SerialWorker(port_name, baud)
        except (serial.SerialException, ValueError) as error:
            messagebox.showerror(APP_TITLE, "Could not open %s:\n\n%s" % (port_name, error))
            return
        self.worker.strip_echo = self.echo_var.get()
        self.worker.start()
        self.connect_button.config(text="Disconnect")
        self.status_label.config(text="Connected: %s @ %d" % (port_name, baud), foreground="#26a269")
        self.log_note("Opened %s at %d baud, 8N1" % (port_name, baud))

    def disconnect(self):
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=1)
            self.worker = None
            self.log_note("Port closed")
        self.scanning = False
        self.connect_button.config(text="Connect")
        self.status_label.config(text="Disconnected", foreground="gray")

    def echo_changed(self):
        if self.worker:
            self.worker.strip_echo = self.echo_var.get()

    def stop_queue(self):
        """Cancel everything waiting to be sent (e.g. a bus scan)."""
        self.scanning = False
        self.slider_pending = None
        if self.worker:
            self.worker.clear_pending()
            self.log_note("Pending commands cleared")

    def on_close(self):
        self.disconnect()
        self.destroy()

    # ======================================================================
    # Sending commands
    # ======================================================================

    def get_address(self):
        """Read and check the node address box.  Returns "010" or None."""
        text = self.address_var.get().strip()
        if not text.isdigit() or not 0 <= int(text) <= 999:
            messagebox.showwarning(APP_TITLE, "Node address must be a number 000-332.")
            return None
        return sp.format_address(text)

    def queue_bytes(self, data, **extra):
        """Put raw bytes on the worker's queue.  extra goes into the request
        and comes back with the reply (used for callbacks)."""
        if not self.worker:
            messagebox.showinfo(APP_TITLE, "Connect to a COM port first.")
            return False
        self.worker.requests.put(make_request(data, **extra))
        return True

    def send(self, command, access="", data="", address=None, **extra):
        """Build a command for the current node address and queue it.

        Example: self.send("lout", "=", 50)  ->  !001:lout=50*xx<CR><LF>
        """
        address = address or self.get_address()
        if address is None:
            return False
        raw = sp.build_command(address, command, access, data, self.checksum_var.get())
        # Spec 3.5: commands are limited to 31 characters.  A full dimming
        # curve plus "*XX" is 32, so drop the (optional) checksum if needed.
        if len(raw) - len(sp.EOF) > sp.MAX_COMMAND_LENGTH:
            raw = sp.build_command(address, command, access, data, use_checksum=False)
            self.log_note("Checksum left off: command would exceed %d characters"
                          % sp.MAX_COMMAND_LENGTH)
        return self.queue_bytes(raw, **extra)

    def send_raw(self):
        """Send whatever is typed in the raw box, escapes converted."""
        text = self.raw_entry_var.get()
        if not text:
            return
        data = sp.build_raw(text, add_checksum=False, add_eof=self.raw_eof_var.get())
        if self.queue_bytes(data, typed=True):
            if not self.raw_history or self.raw_history[-1] != text:
                self.raw_history.append(text)
            self.history_index = len(self.raw_history)

    def raw_history_step(self, direction):
        if not self.raw_history:
            return
        self.history_index = max(0, min(len(self.raw_history) - 1, self.history_index + direction))
        self.raw_entry_var.set(self.raw_history[self.history_index])

    # ---- Light output ----------------------------------------------------
    def slider_moved(self, value):
        """Called by Tk whenever the slider value changes.  Just remember
        the value - slider_tick() decides when to actually send it."""
        value = int(float(value))
        if value != self.known_output:       # ignore changes we made ourselves
            self.slider_pending = value
            self.slider_moved_at = time.monotonic()

    def slider_tick(self):
        """Every SLIDER_PERIOD_MS: send the newest slider value once the
        slider has been still for the settle time, and only when nothing
        else is waiting, so commands never pile up on the line."""
        try:
            settle = max(0, int(self.settle_ms_var.get())) / 1000.0
        except ValueError:
            settle = DEFAULT_SETTLE_MS / 1000.0
        settled = time.monotonic() - getattr(self, "slider_moved_at", 0) >= settle
        if (self.slider_pending is not None and settled and self.worker
                and self.worker.requests.empty() and not self.worker.busy):
            value = self.slider_pending
            self.slider_pending = None
            self.known_output = value
            self.curve_graph.redraw()   # move the orange dot
            self.summary["Light output"].config(text="%d %% (sent)" % value)
            self.send("lout", "=", value)
        self.after(SLIDER_PERIOD_MS, self.slider_tick)

    def set_output(self, value):
        """Preset buttons: move the slider and send right away."""
        self.known_output = value
        self.curve_graph.redraw()   # move the orange dot
        self.slider.set(value)
        self.summary["Light output"].config(text="%d %% (sent)" % value)
        self.slider_pending = None
        self.send("lout", "=", value)

    def step_output(self, access, amount):
        """+/- buttons, then read back so the slider shows the real level."""
        if self.send("lout", access, amount):
            self.send("lout", "?")

    def show_output(self, value):
        """Move the slider to a value read from the light WITHOUT sending."""
        self.known_output = value
        self.curve_graph.redraw()   # move the orange dot
        self.slider_pending = None
        self.slider.set(value)

    # ---- Status ----------------------------------------------------------
    def read_all(self):
        for command in ("info", "stat", "temp", "rhum", "lout", "stby"):
            self.send(command, "?")

    def autopoll_tick(self):
        """Once a second: if auto-poll is on and its time is up, queue reads."""
        self._autopoll_count = getattr(self, "_autopoll_count", 0) + 1
        try:
            interval = max(1, int(self.autopoll_seconds.get()))
        except ValueError:
            interval = 5
        if (self.autopoll_var.get() and self.worker and not self.scanning
                and self._autopoll_count >= interval and self.worker.requests.empty()):
            self._autopoll_count = 0
            for command in ("temp", "rhum", "lout", "stby"):
                self.send(command, "?")
        self.after(1000, self.autopoll_tick)

    # ---- Presets ---------------------------------------------------------
    def save_preset(self):
        number = self.preset_var.get()
        if messagebox.askyesno(APP_TITLE, "Overwrite preset %s with the light's current state?" % number):
            self.send("pset", "+", number)

    def clear_preset(self):
        number = self.preset_var.get()
        if messagebox.askyesno(APP_TITLE, "Clear preset %s back to factory default?" % number):
            self.send("pset", "-", number)

    # ---- Dimming curve ---------------------------------------------------
    def fill_curve(self, text):
        for var, value in zip(self.curve_vars, text.split(",")):
            var.set(value.strip())

    def curve_read(self, reply):
        """Callback after 'Read': put the 7 values into the boxes."""
        text = sp.reply_text(reply)
        if text.count(",") == 6:
            self.fill_curve(text)

    def write_curve(self):
        values = ",".join(var.get().strip() for var in self.curve_vars)
        if messagebox.askyesno(APP_TITLE, "Write dimming curve %s ?" % values):
            self.send("curv", "=", values)

    # ---- Setup -----------------------------------------------------------
    def change_address(self):
        old = self.get_address()
        new = self.new_address_var.get().strip()
        if old is None:
            return
        if not new.isdigit() or not 1 <= int(new) <= 255:
            messagebox.showwarning(APP_TITLE, "New address must be 001-255.")
            return
        new = sp.format_address(new)
        if not sp.expects_reply(old):
            messagebox.showwarning(APP_TITLE, "Pick a single light's address (001-255), "
                                              "not broadcast or a group.")
            return
        if messagebox.askyesno(APP_TITLE, "Change light %s to address %s?\n\n"
                               "The light stops answering at %s immediately." % (old, new, old)):
            self.send("addr", "=", new, on_reply=lambda reply: self.address_changed(reply, new))

    def address_changed(self, reply, new):
        if sp.is_ack(reply):
            self.address_var.set(new)
            self.log_note("Address box switched to %s" % new)

    def change_baud(self):
        new_baud = int(self.new_baud_var.get())
        code = {rate: code for code, rate in sp.BAUD_CODES.items()}[new_baud]
        address = self.get_address()
        if address and messagebox.askyesno(APP_TITLE, (
                "Change light %s to %d baud?\n\nAfter the light ACKs, this app "
                "will switch the COM port to %d automatically.") % (address, new_baud, new_baud)):
            self.send("baud", "=", code, new_baud=new_baud)

    def factory_reset(self):
        address = self.get_address()
        if address is None or not sp.expects_reply(address):
            messagebox.showwarning(APP_TITLE, "Factory reset needs a single light's address.")
            return
        if not messagebox.askyesno(APP_TITLE, (
                "FACTORY RESET light %s?\n\nThis erases all presets and resets "
                "the address, baud rate, groups and dimming curve.") % address, icon="warning"):
            return
        use_checksum = self.checksum_var.get()

        def send_key(reply):
            """Runs in the worker right after 'rset?' so no other command can
            sneak in between (which would cancel the reset)."""
            key = sp.reply_text(reply)
            if len(key) == 4 and key.isalnum():
                return sp.build_command(address, "rset", "=", key, use_checksum)
            return None

        self.send("rset", "?", followup=send_key)

    # ---- Bus scan --------------------------------------------------------
    def scan_bus(self):
        """Ask every address 001-255 'addr?' and report who answers.
        Takes about 40 s at SCAN_TIMEOUT = 0.15 s.  'Stop' cancels it."""
        if not self.worker:
            messagebox.showinfo(APP_TITLE, "Connect to a COM port first.")
            return
        self.scanning = True
        self.found_addresses = []
        self.log_note("Scanning addresses 001-255 (Stop / clear queue to cancel)...")
        for number in range(1, 256):
            address = sp.format_address(number)
            self.send("addr", "?", address=address, timeout=SCAN_TIMEOUT,
                      scan=address, last_scan=(number == 255))

    # ======================================================================
    # Handling results from the worker
    # ======================================================================

    def process_worker_events(self):
        """Runs every GUI_POLL_MS on the GUI thread."""
        # Re-arm the timer FIRST so one bad event can never stop the polling
        self.after(GUI_POLL_MS, self.process_worker_events)
        worker = self.worker
        while worker:
            try:
                event = worker.events.get_nowait()
            except queue.Empty:
                break
            try:
                self.handle_event(event)
            except Exception as error:          # show it, keep running
                self.log_line("ERROR", "GUI error: %r" % error, "error")

    def handle_event(self, event):
        kind = event[0]

        if kind == "tx":
            self.log_bytes("TX", event[1]["data"], "tx")

        elif kind == "rx":
            request, echo, reply = event[1], event[2], event[3]
            if echo:
                self.log_bytes("ECHO", echo, "echo")
            if reply:
                self.log_bytes("RX", reply, "rx")
            elif request.get("scan") is None:
                self.log_note("(no reply)")

            # Bus scan: only report the addresses that answered
            if request.get("scan"):
                # A light answers "addr?" with its own address, e.g.
                # "!<LF>001<CR><LF>".  Only count it when that matches the
                # address we asked.  A bare "!<LF>" (every light sends that
                # for every command) or a late reply meant for a different
                # address does not count.
                answer = sp.reply_text(reply)
                if answer.isdigit() and int(answer) == int(request["scan"]):
                    self.found_addresses.append(request["scan"])
                    self.log_note("Found a light at address %s" % request["scan"])
                if request.get("last_scan"):
                    self.scanning = False
                    found = ", ".join(self.found_addresses) or "none"
                    self.log_note("Scan finished. Lights found at: %s" % found)
                    self.write_parsed("Bus scan", [("Lights found", found)])
                return

            pairs = sp.parse_reply(request["command"], request["access"], reply,
                                   request["reply_expected"])
            heading = sp.bytes_to_display(request["data"])
            self.write_parsed(heading, pairs)
            self.update_summary(request["command"], pairs)
            if request.get("on_reply"):
                request["on_reply"](reply)

        elif kind == "unsolicited":
            self.log_bytes("RX?", event[1], "rx")

        elif kind == "baud":
            self.baud_var.set(str(event[1]))
            self.status_label.config(text="Connected: %s @ %d" % (
                self.worker.port.port, event[1]), foreground="#26a269")
            self.log_note("COM port switched to %d baud" % event[1])

        elif kind == "error":
            self.log_line("ERROR", event[1], "error")
            self.disconnect()
            messagebox.showerror(APP_TITLE, event[1])

    def update_summary(self, command, pairs):
        """Copy decoded values into the 'Latest values' table, and keep the
        slider in step with the light's real output level."""
        for label, value in pairs:
            if label in self.summary:
                self.summary[label].config(text=value)
        if command == "lout":
            for label, value in pairs:
                if label == "Light output":
                    number = value.split()[0]
                    if number.isdigit():
                        self.show_output(int(number))

    # ======================================================================
    # Text panels
    # ======================================================================

    @staticmethod
    def timestamp():
        now = time.time()
        return time.strftime("%H:%M:%S", time.localtime(now)) + ".%03d" % (now % 1 * 1000)

    def append(self, widget, text, tag=None):
        widget.config(state=tk.NORMAL)
        widget.insert(tk.END, text, tag)
        widget.see(tk.END)
        widget.config(state=tk.DISABLED)

    def log_line(self, direction, text, tag):
        self.append(self.raw_text, "%s  %-4s %s\n" % (self.timestamp(), direction, text), tag)

    def log_bytes(self, direction, data, tag):
        self.log_line(direction, sp.bytes_to_display(data), tag)
        if self.hex_var.get():
            self.log_line("", sp.bytes_to_hex(data), tag)

    def log_note(self, text):
        self.log_line("--", text, "note")

    def write_parsed(self, heading, pairs):
        self.append(self.parsed_text, "%s  %s\n" % (self.timestamp()[:8], heading), "head")
        for label, value in pairs:
            tag = None
            if label == "Result":
                tag = "ack" if value.startswith(("ACK", "DATA", "No reply (normal")) else "bad"
            self.append(self.parsed_text, "    %-22s %s\n" % (label + ":", value), tag)

    @staticmethod
    def clear_text(widget):
        widget.config(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.config(state=tk.DISABLED)

    def save_log(self):
        filename = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("Text files", "*.txt")],
            initialfile=time.strftime("seasense_log_%Y%m%d_%H%M%S.txt"))
        if filename:
            with open(filename, "w", encoding="utf-8") as handle:
                handle.write(self.raw_text.get("1.0", tk.END))
                handle.write("\n===== Parsed replies =====\n")
                handle.write(self.parsed_text.get("1.0", tk.END))


if __name__ == "__main__":
    App().mainloop()
