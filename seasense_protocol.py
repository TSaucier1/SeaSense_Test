"""
seasense_protocol.py
====================

Pure helper functions for the DeepSea Power & Light SeaSense serial protocol
(document 792-004-013-0A-01, Rev C).  Nothing in this file touches the GUI or
the serial port, which keeps it easy to read and easy to test.

Quick protocol refresher (section 4 of the spec)
------------------------------------------------
Every host command looks like this:

    !  010  :  lout  =  100  *  A8  <CR><LF>
    |   |   |   |    |   |   |   |     |
    |   |   |   |    |   |   |   |     +-- End-of-Frame (always CR LF)
    |   |   |   |    |   |   |   +-------- Checksum (optional, 2 hex chars)
    |   |   |   |    |   |   +------------ Data separator (only if checksum used)
    |   |   |   |    |   +---------------- Data payload (only for = + -)
    |   |   |   |    +-------------------- Access code: = write, ? read,
    |   |   |   |                          + increment/add, - decrement/remove,
    |   |   |   |                          (nothing) = immediate command
    |   |   |   +------------------------- 4-letter command ID
    |   |   +----------------------------- Address separator
    |   +--------------------------------- 3-digit node address (000 = broadcast)
    +------------------------------------- Start-of-Frame

The light answers with one of:
    ACK        -> just <CR><LF>
    ACK+DATA   -> some text, then <CR><LF>  (some replies end in <CR> only)
    NAK        -> "?" <CR><LF>
    nothing    -> broadcast/group commands, or a checksum error
"""

import re

# --------------------------------------------------------------------------
# Constants straight from the spec
# --------------------------------------------------------------------------

EOF = b"\r\n"                     # End-of-Frame marker (section 4.9)
MAX_COMMAND_LENGTH = 31           # Section 3.5 (not counting the CR LF)
BAUD_RATES = [9600, 19200, 57600]  # Section 3.2
BAUD_CODES = {1: 9600, 2: 19200, 3: 57600}  # Table 5, used by the BAUD command

# Friendly names for the control characters we show in the raw log and
# accept in the "send raw" box.  Key = byte value, value = name.
CONTROL_NAMES = {
    0x00: "NUL", 0x01: "SOH", 0x02: "STX", 0x03: "ETX", 0x04: "EOT",
    0x05: "ENQ", 0x06: "ACK", 0x07: "BEL", 0x08: "BS", 0x09: "TAB",
    0x0A: "LF", 0x0B: "VT", 0x0C: "FF", 0x0D: "CR", 0x15: "NAK",
    0x1B: "ESC", 0x7F: "DEL",
}
# Reverse lookup: name -> byte value (e.g. "CR" -> 0x0D)
CONTROL_BYTES = {name: value for value, name in CONTROL_NAMES.items()}


# --------------------------------------------------------------------------
# Building commands
# --------------------------------------------------------------------------

def checksum(text_bytes):
    """Return the 2-character checksum for everything from '!' up to and
    including '*' (Appendix A).

    The checksum is simply the sum of all the byte values, keeping only the
    lowest 8 bits, written as two uppercase hex digits.

        checksum(b"!010:lout?*")  ->  b"19"
    """
    total = sum(text_bytes) & 0xFF
    return ("%02X" % total).encode("ascii")


def format_address(address):
    """Turn 10, "10" or "010" into the 3-digit string "010"."""
    return str(int(address)).zfill(3)


def build_command(address, command, access="", data="", use_checksum=True):
    """Build a complete, ready-to-send SeaSense command as bytes.

    address      -- node address, e.g. 10 or "010"
    command      -- 4-letter command ID, e.g. "lout"
    access       -- "=", "?", "+", "-" or "" (immediate)
    data         -- payload for = + -, e.g. 50 or "1,100,1,5,70,95,4.5"
    use_checksum -- add "*XX" before the CR LF

    Example:
        build_command(10, "lout", "=", 100)  ->  b"!010:lout=100*A8\\r\\n"
    """
    body = "!" + format_address(address) + ":" + command.lower() + access + str(data)
    body = body.encode("ascii")
    if use_checksum:
        body += b"*"
        body += checksum(body)
    return body + EOF


# --------------------------------------------------------------------------
# The "send raw" text box
# --------------------------------------------------------------------------

# One regular expression that finds every escape we understand:
#   <CR>  <lf>  <Tab>          -> named control characters (spec notation)
#   [0x0D]  [0d]               -> hex byte in square brackets (spec notation)
#   \r  \n  \t  \\  \x0D       -> C / Python style escapes
_ESCAPE_RE = re.compile(
    r"<(?P<name>[A-Za-z]{2,3})>"
    r"|\[(?:0x)?(?P<hexbr>[0-9A-Fa-f]{2})\]"
    r"|\\x(?P<hexc>[0-9A-Fa-f]{2})"
    r"|\\(?P<c>[rnt\\])"
)
_C_ESCAPES = {"r": b"\r", "n": b"\n", "t": b"\t", "\\": b"\\"}


def parse_escapes(text):
    """Convert what the user typed into the exact bytes to send.

    Anything that is not a recognised escape is sent as-is.  An unknown
    <XYZ> name is also sent as-is (so "<" and ">" still work normally).

        parse_escapes("!010:lout?<CR><LF>")  ->  b"!010:lout?\\r\\n"
        parse_escapes("abc[0x0D]\\n")         ->  b"abc\\r\\n"
    """
    out = b""
    position = 0
    for match in _ESCAPE_RE.finditer(text):
        # Copy the plain text before this escape
        out += text[position:match.start()].encode("latin-1", errors="replace")
        position = match.end()

        if match.group("name"):
            name = match.group("name").upper()
            if name in CONTROL_BYTES:
                out += bytes([CONTROL_BYTES[name]])
            else:
                out += match.group(0).encode("latin-1")   # not ours, keep it
        elif match.group("hexbr"):
            out += bytes([int(match.group("hexbr"), 16)])
        elif match.group("hexc"):
            out += bytes([int(match.group("hexc"), 16)])
        else:
            out += _C_ESCAPES[match.group("c")]

    out += text[position:].encode("latin-1", errors="replace")
    return out


def build_raw(text, add_checksum=False, add_eof=False):
    """Turn the raw text box contents into bytes, optionally adding a
    checksum and/or the CR LF terminator.

    The checksum is inserted *before* any trailing CR/LF the user typed, and
    is skipped if the text already ends in "*XX".
    """
    data = parse_escapes(text)

    # Split off trailing CR / LF so the checksum goes in the right place
    body = data.rstrip(b"\r\n")
    tail = data[len(body):]

    if add_checksum and body.startswith(b"!") and not re.search(rb"\*[0-9A-Fa-f]{2}$", body):
        if not body.endswith(b"*"):
            body += b"*"
        body += checksum(body)

    if add_eof and not tail.endswith(EOF):
        tail += EOF

    return body + tail


# --------------------------------------------------------------------------
# Showing bytes in the raw log
# --------------------------------------------------------------------------

def bytes_to_display(data):
    """Show bytes as readable text with control characters made visible.

        b"!010:lout?*19\\r\\n"  ->  "!010:lout?*19<CR><LF>"
    """
    parts = []
    for value in data:
        if value in CONTROL_NAMES:
            parts.append("<" + CONTROL_NAMES[value] + ">")
        elif 32 <= value < 127:
            parts.append(chr(value))
        else:
            parts.append("[0x%02X]" % value)
    return "".join(parts)


def bytes_to_hex(data):
    """Show bytes as hex: b"!0" -> "21 30"."""
    return " ".join("%02X" % value for value in data)


# --------------------------------------------------------------------------
# Understanding what was sent / received
# --------------------------------------------------------------------------

_COMMAND_RE = re.compile(rb"!(\d{3}):([A-Za-z]{4})([=?+\-]?)")


def identify_command(sent):
    """Pull the address, command ID and access code out of a sent command.

    Returns (address, command, access) or (None, None, None) if the bytes do
    not look like a SeaSense command.

        identify_command(b"!010:lout?*19\\r\\n")  ->  ("010", "lout", "?")
    """
    match = _COMMAND_RE.search(sent)
    if not match:
        return None, None, None
    address, command, access = (part.decode("ascii") for part in match.groups())
    return address, command.lower(), access


def expects_reply(address):
    """Broadcast (000) and group (301-332) commands never get a reply."""
    if address is None:
        return True
    number = int(address)
    return not (number == 0 or 301 <= number <= 332)


# Real LSL-1000 lights (firmware 1.4.x) send "!" + LF as soon as they see
# ANY command start on the bus - even one addressed to a different light.
# The addressed light then continues with its actual reply, e.g.
#     addressed light:  b"!\n100\r\n"     (data)   b"!\n\r\n"  (ACK)
#     any other light:  b"!\n"            (nothing more - NOT an answer)
# The spec does not mention this prefix.
#
# Captured over the internal USB port (FW 1.4.1006, one light only), a bare
# b"!\n" also came back for: a wrong checksum, a broadcast read (!000:stby?)
# and CHSW? on a CH1+2 unit at the right address.  So "!\n" alone means
# "nobody answered", not necessarily "no light at this address".
LIGHT_PREFIX = b"!\n"


def reply_body(reply):
    """Return the reply bytes with the light's "!" + LF prefix removed."""
    if reply.startswith(LIGHT_PREFIX):
        return reply[len(LIGHT_PREFIX):]
    if reply == b"!":                 # prefix arrived without its LF
        return b""
    return reply


def has_answer(reply):
    """True if a light actually answered (not just the "!" + LF prefix)."""
    return reply_body(reply) != b""


def reply_text(reply):
    """Return the useful part of a reply as text, prefix and surrounding
    CR/LF removed.  Tabs and line breaks INSIDE multi-line replies (STAT,
    PSET) are kept.

        reply_text(b"!\n100\r\n")  ->  "100"
        reply_text(b"!\n\r\n")     ->  ""      (ACK)
        reply_text(b"!\n?\r\n")    ->  "?"     (NAK)
    """
    return reply_body(reply).decode("ascii", errors="replace").strip("\r\n ")


def is_ack(reply):
    """True if the reply is a plain ACK: an end-of-frame after the prefix.
    A bare "!" + LF prefix is NOT an ACK - nobody answered."""
    return has_answer(reply) and reply_text(reply) == ""


def strip_echo(sent, received):
    """Some USB RS-485 adapters echo our own transmission back to us.
    If the received bytes start with exactly what we sent, remove it.

    Returns (echo_bytes, reply_bytes).
    """
    if sent and received.startswith(sent):
        return sent, received[len(sent):]
    return b"", received


# Lookup tables used when decoding replies --------------------------------

# Appendix E: option codes in an LED SeaLite INFO string
# Sources: spec Rev C Appendix E, plus DeepSea's service guide "Configuring
# a Spare LSL-2000 Driver Board" (2025), which lists the codes FSET accepts.
LED_COLORS = {
    "CW00": "Cool White", "DW00": "Daylight White", "DW90": "Daylight White 90 CRI",
    "WW00": "Warm White", "R625": "Red 620-630 nm", "BL475": "Blue 475 nm",
    "B485": "Blue 465-485 nm", "G528": "Green 520-535 nm",
    "U365": "UV 365 nm", "U405": "UV 405 nm", "R850": "IR 850 nm",
}
BEAM_PATTERNS = {
    "WFD": "Wide Flood (115°)", "FLD": "Flood (75°)", "MED": "Medium (40°)",
    "SPT": "Spot (35°)", "75/40": "75/40 Dual Beam", "115/75": "115/75 Dual Beam",
}
DIMMING_CONTROLS = {
    # Codes FSET accepts (service guide)
    "NON": "No dimming", "5V": "Analog 0-5 V", "10V": "Analog 0-10 V",
    "20I": "Analog 4-20 mA", "SER": "Serial RS-232/485",
    # Older codes printed in the spec's Appendix E
    "A05": "Analog 0-5 V", "A10": "Analog 0-10 V", "A20": "Analog 4-20 mA",
    # Seen on an AC-powered LSL-1000; not in any DeepSea document we have
    "PV": "undocumented, likely phase dimming",
}
# Codes offered in the FSET "Dimming" box, in the service guide's order
FSET_DIMMING_CODES = ["SER", "NON", "5V", "10V", "20I", "PV"]
FSET_COLOR_CODES = ["DW00", "DW90", "WW00", "G528", "B485", "R625"]
FSET_BEAM_CODES = ["FLD", "WFD", "MED", "SPT"]

# FPRT serial port codes (service guide): interface, then a BAUD code
PORT_CODES = {"RS4": "RS-485", "RS2": "RS-232"}

CURVE_NAMES = ["YMIN (%)", "YMAX (%)", "A (turn-on)", "B (linear start)",
               "C (exp start)", "D (max point)", "e (exponent)"]
DEFAULT_CURVE = "1,100,1,5,70,95,4.5"
LINEAR_CURVE = "1,100,1,1,100,100,1"


def curve_output(x, ymin, ymax, a, b, c, d, e):
    """APPROXIMATE light output (%) for a control input x (0-100 %), used
    only to draw the dimming-curve graph.

    The spec (6.2.1) names the points but gives no formula, so this is
    fitted to its Figure 2 (default curve), matching it within a few %:
        x <  A       -> off
        A .. B       -> flat at YMIN   (stable turn-on)
        B .. C       -> straight line from YMIN up to the power curve at C
        C .. D       -> power curve  YMIN + (YMAX-YMIN) * (x / D) ** e
                        (about 26 % at C=70, 46 % at 80, 78 % at 90)
        x >= D       -> flat at YMAX   (stable full output)
    With the spec's linear example (1,100,1,1,100,100,1) this gives a
    straight line, as it should.
    """
    def power_curve(value):
        return ymin + (ymax - ymin) * (value / d) ** e if d > 0 else ymax

    if x < a:
        return 0.0
    if x <= b:
        return float(ymin)
    if x >= d:
        return float(ymax)
    if x <= c:                                # linear part, B .. C
        y_c = power_curve(c)
        if c == b:
            return y_c
        return ymin + (y_c - ymin) * (x - b) / (c - b)
    return power_curve(x)                     # exponential part, C .. D

STANDBY_STATES = {"0": "Active (full power)", "1": "Standby"}
CHANNEL_STATES = {"1": "Channel 1", "2": "Channel 2", "3": "Both channels"}
PULSE_MODES = {"0": "Off", "1": "Input Follow", "2": "Pulse Trigger"}
PULSE_EDGES = {"0": "Falling edge", "1": "Rising edge"}
PULSE_INVERT = {"0": "False (ON at edge)", "1": "True (OFF at edge)"}

# What a READ reply means for each command: (label, units or lookup table)
SIMPLE_READS = {
    "addr": ("Node address", ""),
    "temp": ("Internal temperature", " °C"),
    "rhum": ("Internal humidity", " %RH"),
    "lout": ("Light output", " %"),
    "stby": ("Standby", STANDBY_STATES),
    "chsw": ("Active LED channel", CHANNEL_STATES),
    "pmod": ("Pulse mode", PULSE_MODES),
    "pedg": ("Pulse edge", PULSE_EDGES),
    "pinv": ("Pulse invert", PULSE_INVERT),
    "plen": ("Pulse length", " ms"),
    "rset": ("Reset key", ""),
    "vtst": ("Video test pattern", {"0": "Off", "1": "On"}),
    # Factory commands from DeepSea's service guides (not in the spec)
    "ften": ("Termination resistor", {"0": "Off", "1": "On"}),
    "flmx": ("Max output limit (FLMX)", " (1-100)"),
    "fact": ("Factory unlock code", ""),
}


def _split_table(text):
    """Split a tab-delimited table reply into rows of trimmed cells,
    ignoring blank lines and trailing empty cells (BOOT? ends its value
    row with a stray tab: b"0:\\t0\\t1\\t\\r\\n")."""
    rows = []
    for line in text.replace("\r", "\n").split("\n"):
        if line.strip():
            cells = [cell.strip() for cell in line.split("\t")]
            while cells and cells[-1] == "":
                cells.pop()
            rows.append(cells)
    return rows


def parse_reply(command, access, reply, reply_expected=True):
    """Decode a reply into a short list of (label, value) pairs for the
    "Parsed data" panel.

    command        -- the 4-letter command ID that was sent, e.g. "lout"
    access         -- the access code that was sent, e.g. "?"
    reply          -- the bytes received (echo already removed)
    reply_expected -- False for broadcast/group commands

    The first pair is always ("Result", "ACK" / "NAK" / "DATA" / ...).
    """
    if not has_answer(reply):          # nothing, or only the "!" + LF prefix
        if not reply_expected:
            return [("Result", "No reply (normal for broadcast / group address)")]
        if reply:
            return [("Result", "NO REPLY - only the bus prefix !<LF> came back: "
                               "no light at this address, a bad checksum, or the "
                               "light ignores this command (e.g. CHSW? on FW 1.4.1006)")]
        return [("Result", "NO REPLY - check port, baud, address, wiring "
                           "(the light also stays silent on a bad checksum)")]

    # Drop the light's leading "!" and surrounding line breaks
    text = stripped = reply_text(reply)

    if stripped == "":
        # An empty line after a READ is empty data, not an ACK.
        # Real example: ADGR? with no groups answers b"!\n\r\n".
        if access == "?":
            if (command or "").lower() == "adgr":
                return [("Result", "DATA"), ("Group addresses", "(none)")]
            return [("Result", "DATA (empty)")]
        return [("Result", "ACK (command accepted)")]
    if stripped.strip() == "?":
        if (command or "").lower() == "fact" and access == "?":
            # Over the internal USB port the light has no unlock to give
            return [("Result", "No unlock needed (normal on the internal USB port)")]
        return [("Result", "NAK (light refused: unknown command or value out of range)")]

    pairs = [("Result", "DATA")]
    command = (command or "").lower()

    # Simple one-value replies ---------------------------------------------
    if command in SIMPLE_READS:
        label, extra = SIMPLE_READS[command]
        value = stripped.strip()
        if isinstance(extra, dict):
            pairs.append((label, value + " = " + extra.get(value, "unknown value")))
        else:
            pairs.append((label, value + extra))

    # FPRT returns "port,baud code", e.g. "RS4,1" -------------------------
    elif command == "fprt":
        parts = [p.strip() for p in stripped.split(",")]
        port = PORT_CODES.get(parts[0].upper(), parts[0])
        rate = BAUD_CODES.get(int(parts[1])) if len(parts) > 1 and parts[1].isdigit() else None
        pairs.append(("Serial port", "%s = %s%s" % (stripped, port, ", %d baud" % rate if rate else "")))

    # BAUD returns a code 1/2/3 ---------------------------------------------
    elif command == "baud":
        code = stripped.strip()
        rate = BAUD_CODES.get(int(code)) if code.isdigit() else None
        pairs.append(("Baud rate", "%s = %s bps" % (code, rate) if rate else code))

    # ADGR returns a comma list of groups -----------------------------------
    elif command == "adgr":
        pairs.append(("Group addresses", stripped.strip() or "(none)"))

    # CURV returns 7 comma separated numbers --------------------------------
    elif command == "curv":
        values = [v.strip() for v in stripped.split(",")]
        for name, value in zip(CURVE_NAMES, values):
            pairs.append((name, value))

    # INFO returns a comma separated identity string ------------------------
    elif command == "info":
        fields = [f.strip() for f in stripped.split(",")]
        names = ["Address", "Model", "Serial number", "PCB version", "Firmware version"]
        for name, value in zip(names, fields):
            pairs.append((name, value))
        options = fields[5:]
        is_light = len(fields) > 1 and fields[1].upper().startswith("LSL")
        for number, option in enumerate(options, start=1):
            if is_light and number == 1:
                pairs.append(("LED color", "%s = %s" % (option, LED_COLORS.get(option.upper(), "?"))))
            elif is_light and number == 2:
                pairs.append(("Beam pattern", "%s = %s" % (option, BEAM_PATTERNS.get(option.upper(), "?"))))
            elif is_light and number == 3:
                pairs.append(("Dimming control", "%s = %s" % (option, DIMMING_CONTROLS.get(
                    option.upper(), "not listed in spec Rev C"))))
            elif is_light and option.upper().startswith("CH"):
                pairs.append(("Output channels", option + (" = dual output (CHSW works)"
                                                           if "+" in option else "")))
            else:
                pairs.append(("Option %d" % number, option))

    # STAT returns a 2-row tab table: headers then values ------------------
    elif command == "stat":
        rows = _split_table(text)
        if len(rows) >= 2:
            for name, value in zip(rows[0], rows[1]):
                pairs.append((name, value))
        else:
            pairs.append(("Status", stripped))

    # PSET / BOOT return "N: <TAB>a <TAB>b" rows, PSET with a header row ----
    elif command in ("pset", "boot"):
        rows = _split_table(text)
        headers = []
        if rows and not rows[0][0].rstrip(":").isdigit():
            headers = rows.pop(0)[1:]          # drop the "Preset" column title
        if command == "boot" and rows:
            pairs.append(("Boot preset", rows[0][0].rstrip(":")))
        for row in rows:
            preset = row[0].rstrip(":")
            values = row[1:]
            if headers and len(headers) == len(values):
                described = ", ".join("%s=%s" % (h, v) for h, v in zip(headers, values))
            else:
                described = " ".join(values)
            pairs.append(("Preset " + preset, described))

    # Anything else: just show the text -------------------------------------
    else:
        pairs.append(("Data", stripped))

    return pairs
