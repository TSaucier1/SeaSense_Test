"""
seasense_probe.py - read-only probe for a SeaSense light (NOT part of the app).

For each command: send it with CR LF, record every byte received for LISTEN
seconds with a timestamp per chunk, then print repr() of the bytes and the
time until the first and last byte.  Results are also saved to a JSON file.

    python seasense_probe.py COM3            (default 9600 8N1)
    python seasense_probe.py COM3 "!001:LOUT?"   (only the given commands)

Only READ (?) commands are in the default list.  The script refuses anything
with = + - in it unless --allow-write is given.
"""

import json
import sys
import time

import serial

LISTEN = 1.0          # seconds to record after each command
PAUSE = 0.3           # quiet time between commands

COMMANDS = [
    "!001:INFO?", "!001:STAT?", "!001:TEMP?", "!001:RHUM?", "!001:LOUT?",
    "!001:STBY?", "!001:CURV?", "!001:CHSW?", "!001:BOOT?", "!001:PSET?",
    "!001:ADGR?", "!001:BAUD?", "!001:ADDR?",
    "!001:FACT?", "!001:FPRT?", "!001:FTEN?", "!001:FLMX?",
    "!001:SAVE?",        # does a SAVE query exist?
    "!001:HELP?",        # unknown command
    "!001:lout?*19",     # lowercase, correct checksum
    "!001:lout?*00",     # wrong checksum
    "!002:INFO?",        # address with no light
    "!000:STBY?",        # broadcast read
]


def probe(port, command):
    data = command.encode("ascii") + b"\r\n"
    port.reset_input_buffer()
    port.write(data)
    port.flush()
    t0 = time.perf_counter()          # time measured from end of write
    chunks = []
    while time.perf_counter() - t0 < LISTEN:
        waiting = port.in_waiting
        if waiting:
            chunks.append((time.perf_counter() - t0, port.read(waiting)))
        else:
            time.sleep(0.001)
    return data, chunks


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    allow_write = "--allow-write" in sys.argv
    port_name = args[0] if args else "COM3"
    commands = args[1:] or COMMANDS

    for command in commands:
        access = command.split(":", 1)[-1][4:5]
        if access in "=+-" and access and not allow_write:
            sys.exit("Refusing write command %r (use --allow-write)" % command)

    results = []
    with serial.Serial(port_name, 9600, bytesize=8, parity="N", stopbits=1, timeout=0) as port:
        time.sleep(0.2)
        port.reset_input_buffer()
        for command in commands:
            sent, chunks = probe(port, command)
            raw = b"".join(c for _, c in chunks)
            print("=" * 70)
            print("SENT  %r" % sent)
            print("RECV  %r" % raw)
            if chunks:
                print("first byte %.1f ms, last chunk %.1f ms, %d chunks"
                      % (chunks[0][0] * 1000, chunks[-1][0] * 1000, len(chunks)))
                for t, c in chunks:
                    print("   %7.1f ms  %r" % (t * 1000, c))
            else:
                print("(silence)")
            results.append({"command": command, "sent": repr(sent), "raw": repr(raw),
                            "chunks": [[round(t * 1000, 1), repr(c)] for t, c in chunks]})
            time.sleep(PAUSE)
    # port is closed here by the "with" block
    stamp = time.strftime("%Y%m%d_%H%M%S")
    with open("probe_%s.json" % stamp, "w") as f:
        json.dump(results, f, indent=1)
    print("\nSaved probe_%s.json, port closed." % stamp)


if __name__ == "__main__":
    main()
