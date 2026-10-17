"""
fake_light.py - a fake SeaSense LSL-1000 for testing the app WITHOUT hardware.

It has two parts:

  FakeBus     - one or more fake lights sharing a bus.  It answers SeaSense
                commands the way the REAL lights did when we tested them
                (every behaviour below was captured from hardware, see the
                notes next to each one).
  FakeSerial  - a stand-in for serial.Serial.  The app's worker thread talks
                to it exactly as it would to a COM port, so the tests run the
                real app code end to end.

Used by tests/test_app.py.  You can also try the app against it by hand:

    python tools/fake_light.py          (opens the app on a fake light)

Behaviour that depends on HOW you are connected is chosen with port_type:
    "usb"   - the light's internal micro-USB port (FW 1.4.1006):
              FACT? answers "?" (no unlock needed), SAVE answers "?" but the
              settings are kept anyway.
    "rs485" - the external serial pins: factory commands need a FACT unlock,
              SAVE answers "System settings written".

If a real light ever answers differently, change the matching line here,
note the firmware / port / date next to it, and add a test.
"""

import re
import sys
import threading
import time

sys.path.insert(0, __file__.rsplit("tools", 1)[0])   # so "import seasense_protocol" works
from seasense_protocol import checksum   # noqa: E402

ACK = "\r\n"
NAK = "?\r\n"
FRAME = re.compile(r"^!(\d{3}):([A-Za-z]{4})([=?+\-]?)([^*]*)(?:\*([0-9A-Fa-f]{2}))?$")


class FakeLight:
    """The settings of one light."""

    def __init__(self, address=1, serial_number="12-049-05581", bare=False):
        self.address = address
        self.serial_number = "" if bare else serial_number
        self.color = "" if bare else "DW00"
        self.beam = "" if bare else "FLD"
        self.dimming = "" if bare else "PV"     # original board reported PV
        self.firmware = "1.4.1006"
        self.lout, self.stby, self.boot, self.baud = 100, 0, 0, 1
        self.curve = "1,100,0,3,53,92,4.50"     # captured from the real board
        self.groups, self.presets = [], {}
        self.port, self.termination, self.flmx = ("RS2", 1), 0, 100   # bare board default: RS-232
        self.unlocked = False
        self.fact_key = None
        self.reset_key = None
        self.pending_fset = None
        self.listening = True                   # False once FPRT moves it to the other port


class FakeBus:
    """Fake lights on one bus, plus the knobs the tests need."""

    def __init__(self, lights=None, port_type="usb", refuse=(), reply_delay=0.01):
        self.lights = lights if lights is not None else [FakeLight()]
        self.port_type = port_type
        self.refuse = set(refuse)        # commands to NAK on purpose, e.g. {"fset"}
        self.reply_delay = reply_delay
        self.log = []                    # every (line, reply) - handy when a test fails

    def light(self, address=1):
        return next(light for light in self.lights if light.address == address)

    def handle(self, line):
        """Process one command line (without CR LF).  Returns the reply text
        of the addressed light, or None if nobody answers beyond "!<LF>"."""
        match = FRAME.match(line)
        if not match:
            return None
        address, command, access, data, given_checksum = match.groups()
        if given_checksum is not None:
            body = line[:line.index("*") + 1]
            if checksum(body.encode()).decode() != given_checksum.upper():
                return None                     # real light: bad checksum -> only "!<LF>"
        address, command = int(address), command.lower()
        if address == 0:
            targets = self.lights               # broadcast: everyone acts, nobody answers
        else:
            targets = [l for l in self.lights if l.address == address or address in l.groups]
        reply = None
        for light in targets:
            was_addressed = light.address == address   # before ADDR= changes it
            answer = self.run(light, command, access, data)
            if was_addressed:
                reply = answer                  # ADDR= is answered at the old address
        return reply

    # ---- one light's behaviour ----------------------------------------------
    def run(self, light, command, access, data):
        if not light.listening:
            return None
        if command in self.refuse:
            return NAK
        usb = self.port_type == "usb"

        # Factory unlock: needed on the serial pins, not on USB (FW 1.4.1006)
        if command == "fact":
            if usb:
                return NAK
            if access == "?":
                light.fact_key = "7Q2b"
                return "7Q2b\r\n"
            if access == "=" and data == light.fact_key:
                light.unlocked = True
                return ACK
            return NAK
        if command in ("fset", "fprt", "ften", "flmx") and not (usb or light.unlocked):
            return NAK

        if access == "?":
            return self.read(light, command)
        try:
            return self.write(light, command, access, data, usb)
        except (ValueError, IndexError):
            return NAK

    def read(self, light, command):
        if command == "info":
            return "%03d,LSL-1000,%s,0A,%s,%s,%s,%s,CH1+2\r\n" % (
                light.address, light.serial_number, light.firmware,
                light.color, light.beam, light.dimming)
        if command == "stat":   # captured: 8 tab columns, no trailing blank line
            return ("RunTime\tPrCycle\tMaxTemp\tTemp1\tTemp2\tTemp3\tHumid\tFLAGS\r\n"
                    "1.1hr\t12\t102.3C\t24.9C\t22.2C\t21.2C\t29%\t0x0000\r\n")
        if command == "temp":
            return "24.88\r\n"                  # captured: 2 decimals
        if command == "rhum":
            return "29.10\r\n"
        if command == "lout":
            return "%d\r\n" % light.lout        # captured: CR LF (spec shows CR only)
        if command == "stby":
            return "%d\r\n" % light.stby
        if command == "addr":
            return "%03d\r\n" % light.address
        if command == "baud":
            return "%d\r\n" % light.baud
        if command == "curv":
            return light.curve + "\r\n"
        if command == "adgr":                   # captured: no groups -> empty line
            return ",".join(str(g) for g in sorted(light.groups)) + "\r\n"
        if command == "boot":                   # captured: stray trailing tab
            return "Preset\tSTBY\tLOUT\r\n%d:\t%d\t%d\t\r\n" % (light.boot, light.stby, light.lout)
        if command == "pset":                   # captured: presets 1-16, no CHSW column
            rows = "".join("%d:\t%d\t%d\r\n" % (n, *light.presets.get(n, (0, 100)))
                           for n in range(1, 17))
            return "Preset\tSTBY\tLOUT\r\n" + rows
        if command == "fprt":
            return "%s,%d\r\n" % light.port
        if command == "ften":
            return "%d\r\n" % light.termination
        if command == "flmx":
            return "%d\r\n" % light.flmx
        if command == "rset":
            light.reset_key = "4a93"
            return "4a93\r\n"
        if command == "chsw":
            return None                         # captured: CH1+2 unit sends only "!<LF>"
        return NAK                              # captured: unknown command (HELP?, SAVE?)

    def write(self, light, command, access, data, usb):
        if command == "lout":
            value = int(data) if data else 10
            if access == "=":
                if not 0 <= value <= 100:
                    return NAK
                light.lout = value
            elif access == "+":
                light.lout = min(100, light.lout + value)
            elif access == "-":
                light.lout = max(0, light.lout - value)
            return ACK
        if command == "stby" and access == "=":
            light.stby = int(data)
            return ACK
        if command == "addr" and access == "=":
            light.address = int(data)
            return ACK
        if command == "baud" and access == "=":
            light.baud = int(data)
            return ACK
        if command == "curv" and access == "=":
            return ACK if data.count(",") == 6 else NAK
        if command == "adgr" and access in "=+":
            light.groups.append(int(data))
            return ACK
        if command == "adgr" and access == "-":
            light.groups.remove(int(data))
            return ACK
        if command == "pset" and access == "+":
            light.presets[int(data)] = (light.stby, light.lout)
            return ACK
        if command == "boot" and access == "=":
            light.boot = int(data)
            return ACK
        if command == "fset" and access == "=":
            serial_number, dimming, color, beam = data.split(",")
            # Applied straight away: on FW 1.4.1006 INFO showed the new values
            # even though SAVE answered "?"
            light.serial_number, light.dimming, light.color, light.beam = \
                serial_number, dimming, color, beam
            return ACK
        if command == "ften" and access == "=":
            light.termination = int(data)
            return ACK
        if command == "flmx" and access == "=":
            value = int(data)
            if not 1 <= value <= 100:
                return NAK
            light.flmx = value
            return ACK
        if command == "fprt" and access == "=":
            port, code = data.split(",")
            light.port = (port, int(code))
            # FPRT takes effect at once.  The internal USB port keeps working;
            # on the serial pins the light moves away if the port type changes.
            if not usb and port != ("RS4" if self.port_type == "rs485" else "RS2"):
                light.listening = False
            return ACK
        if command == "save" and access == "":
            # USB, FW 1.4.1006: "?" after FSET, FTEN and FPRT - settings kept anyway
            return NAK if usb else "System settings written\r\n"
        if command == "rset" and access == "=":
            if data == light.reset_key:
                address = light.address
                light.__init__(address)
                return ACK
            return NAK
        return NAK


class FakeSerial:
    """Just enough of serial.Serial for the app's worker thread.

    Every complete command line gets the "!<LF>" prefix almost at once (every
    real light sends that for every command on the bus), then the addressed
    light's reply after bus.reply_delay seconds."""

    def __init__(self, bus, port="FAKE", baudrate=9600, echo=False, **_settings):
        self.bus = bus
        self.port = port
        self.baudrate = baudrate
        self.echo = echo                 # True = behave like an echoing RS-485 adapter
        self.is_open = True
        self._lock = threading.Lock()
        self._incoming = b""             # bytes written by the app, not yet a full line
        self._outgoing = []              # (time the bytes become readable, bytes)

    # -- what the app calls --------------------------------------------------
    def write(self, data):
        now = time.monotonic()
        with self._lock:
            if self.echo:
                self._outgoing.append((now, bytes(data)))
            self._incoming += data
            while b"\r\n" in self._incoming:
                line, self._incoming = self._incoming.split(b"\r\n", 1)
                text = line.decode("ascii", "replace")
                reply = self.bus.handle(text)
                self.bus.log.append((text, reply))
                if text.startswith("!"):
                    self._outgoing.append((now + 0.002, b"!\n"))
                if reply is not None:
                    self._outgoing.append((now + self.bus.reply_delay, reply.encode()))
        return len(data)

    def _ready(self):
        now = time.monotonic()
        return b"".join(data for when, data in self._outgoing if when <= now)

    @property
    def in_waiting(self):
        with self._lock:
            return len(self._ready())

    def read(self, size=1):
        with self._lock:
            now = time.monotonic()
            ready, waiting = b"", []
            for when, data in self._outgoing:
                if when <= now:
                    ready += data
                else:
                    waiting.append((when, data))
            out, rest = ready[:size], ready[size:]
            self._outgoing = ([(now, rest)] if rest else []) + waiting
            return out

    def flush(self):
        pass

    def reset_input_buffer(self):
        with self._lock:
            now = time.monotonic()
            self._outgoing = [(when, data) for when, data in self._outgoing if when > now]

    def close(self):
        self.is_open = False


if __name__ == "__main__":
    # Open the real app on a fake light: python tools/fake_light.py [usb|rs485]
    from unittest import mock
    import serial
    import seasense_gui

    bus = FakeBus(port_type=sys.argv[1] if len(sys.argv) > 1 else "usb")
    with mock.patch.object(serial, "Serial", lambda port, baud, **kw: FakeSerial(bus, port, baud)):
        app = seasense_gui.App()
        app.port_var.set("FAKE - simulated LSL-1000")
        app.mainloop()
