"""
hil_test.py - hardware-in-the-loop test: runs the REAL app against a REAL light.

It opens the app window, drives the same sequences the buttons do, checks the
results and writes a report.  Nothing is clicked by hand except one power
cycle in the write tests.

    python tools/hil_test.py COM3                  read-only checks (safe)
    python tools/hil_test.py COM3 --allow-write    + LOUT, one-step setup and a
                                                   power-cycle persistence check

Options:
    --address 001        node address of the light (default 001)
    --baud 9600          COM port baud rate (default 9600)
    --serial 12-049-...  serial number to use if the light reports none
                         (freshly flashed board); otherwise it is read from INFO
    --scan 1-10          address range for the bus scan check (default 1-10)

Close the app first so the COM port is free.  The write tests change the
light's configuration to: SER dimming, RS-485 at 9600, termination on (the
one-step setup) - run them over the internal USB port, with mains OFF.

Output (in the current folder):
    hil_report_<time>.md   PASS / FAIL table
    hil_raw_<time>.txt     every byte sent and received, plus the parsed view
Exit code 0 = all passed, 1 = something failed.
"""

import argparse
import os
import sys
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import seasense_gui as gui        # noqa: E402
import seasense_protocol as sp    # noqa: E402


class Runner:
    def __init__(self, args):
        self.args = args
        self.results = []           # (test, expected, actual, passed)
        self.dialogs = []
        self.app = gui.App()
        self.app.title(self.app.title() + "  [HIL TEST - do not click]")

    # ---- plumbing -------------------------------------------------------------
    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.update()
            time.sleep(0.005)

    def idle(self, timeout=30):
        """Wait until every queued command has been answered."""
        end = time.monotonic() + timeout
        self.pump(0.1)
        while time.monotonic() < end:
            worker = self.app.worker
            if worker is None or (worker.requests.empty() and not worker.busy and worker.events.empty()):
                self.pump(0.2)
                return
            self.pump(0.05)
        raise RuntimeError("timed out waiting for the light")

    def check(self, name, expected, actual, passed=None):
        if passed is None:
            passed = str(actual).strip().upper() == str(expected).strip().upper()
        self.results.append((name, expected, actual, passed))
        print("%-4s %-38s expected %-28s got %s" % ("PASS" if passed else "FAIL", name, expected, actual))
        return passed

    def summary(self, name):
        return self.app.summary[name].cget("text")

    def connect(self):
        self.app.port_var.set(self.args.port)
        self.app.baud_var.set(str(self.args.baud))
        self.app.address_var.set(self.args.address)
        self.app.connect()
        self.pump(0.3)
        return self.check("Open COM port", "connected", "connected" if self.app.worker else "failed")

    def read_all(self):
        for label in self.app.summary.values():
            label.config(text="-")
        self.app.read_all()
        self.idle()

    # ---- read-only tests ------------------------------------------------------------
    def read_only(self):
        self.read_all()
        self.check("INFO: model", "LSL-xxxx", self.summary("Model"),
                   self.summary("Model").startswith("LSL"))
        self.check("INFO: firmware version read", "x.x.x", self.summary("Firmware version"),
                   self.summary("Firmware version").count(".") == 2)
        self.check("TEMP decoded", "number °C", self.summary("Internal temperature"),
                   self.summary("Internal temperature").endswith("°C"))
        self.check("RHUM decoded", "number %RH", self.summary("Internal humidity"),
                   self.summary("Internal humidity").endswith("%RH"))
        self.check("STAT decoded", "RunTime value", self.summary("RunTime"),
                   self.summary("RunTime") not in ("", "-"))
        self.check("FPRT? decoded", "RSx,n = ...", self.summary("Serial port"),
                   self.summary("Serial port")[:2] == "RS")
        self.check("FTEN? decoded", "0 or 1", self.summary("Termination resistor"),
                   self.summary("Termination resistor")[:1] in ("0", "1"))
        raw = self.app.raw_text.get("1.0", "end")
        self.check("No app errors during Read all", "0 ERROR lines", "%d ERROR lines" % raw.count("ERROR"),
                   "ERROR" not in raw)

        # Reads the parser has special handling for
        for command, test in (("adgr", "Group addresses"), ("boot", "Boot preset"),
                              ("curv", "e (exponent)"), ("pset", "Preset 1")):
            replies = []
            self.app.send(command, "?", on_reply=replies.append)
            self.idle()
            pairs = dict(sp.parse_reply(command, "?", replies[0] if replies else b""))
            self.check("%s? decoded" % command.upper(), test, pairs.get(test, "(missing)"), test in pairs)

        # Bus scan over a small range: only the configured light should answer
        first, last = (int(x) for x in self.args.scan.split("-"))
        self.app.found_addresses = []
        self.app.scanning = True
        for number in range(first, last + 1):
            address = sp.format_address(number)
            self.app.send("addr", "?", address=address, timeout=gui.SCAN_TIMEOUT,
                          scan=address, last_scan=(number == last))
        self.idle(timeout=120)
        expected = sp.format_address(self.args.address)
        self.check("Bus scan %s finds only this light" % self.args.scan, expected,
                   ", ".join(self.app.found_addresses) or "none")

    # ---- write tests (only with --allow-write) ----------------------------------
    def write_tests(self):
        # 1. Output level: set 10, read back, restore
        replies = []
        self.app.send("lout", "?", on_reply=replies.append)
        self.idle()
        original = sp.reply_text(replies[0]) if replies else "100"
        self.app.set_output(10)
        self.idle()
        replies = []
        self.app.send("lout", "?", on_reply=replies.append)
        self.idle()
        self.check("LOUT=10 then LOUT?", "10", sp.reply_text(replies[0]) if replies else "(no reply)")
        if original.isdigit():
            self.app.set_output(int(original))
            self.idle()

        # 2. One-step setup, answering "yes" to its confirmation
        self.dialogs.clear()
        with mock.patch.object(gui.messagebox, "askyesno", lambda *a, **k: True):
            self.app.one_step_setup()
            end = time.monotonic() + 30
            while time.monotonic() < end and not self.dialogs:
                self.pump(0.05)
        self.idle()
        kinds = [kind for kind, _ in self.dialogs]
        self.check("One-step setup result", "info (all OK)", ", ".join(kinds) or "no dialog",
                   kinds == ["info"])
        for kind, text in self.dialogs:
            print("   %s: %s" % (kind, text.replace("\n", " | ")))
        expected = self.read_config()

        # 3. Persistence across a power cycle
        self.app.disconnect()
        print("\n>>> Power-cycle the light now (for the USB port: unplug and replug it).")
        input(">>> When it is back (COM port present again), press Enter... ")
        self.pump(0.5)
        if not self.connect():
            return
        self.read_all()
        after = self.read_config()
        for key in ("serial", "dimming", "port", "termination"):
            self.check("After power cycle: %s kept" % key, expected[key], after[key])

    def read_config(self):
        info = {}
        self.read_all()
        info["serial"] = self.summary("Serial number")
        info["dimming"] = self.summary("Dimming control").split(" ")[0]
        info["port"] = self.summary("Serial port").split(" ")[0]
        info["termination"] = self.summary("Termination resistor").split(" ")[0]
        return info

    # ---- run everything ------------------------------------------------------------
    def run(self):
        record = lambda kind: (lambda *a, **k: self.dialogs.append((kind, " ".join(map(str, a[1:])))))
        patches = [
            mock.patch.object(gui.messagebox, "askyesno", lambda *a, **k: False),   # writes need --allow-write
            mock.patch.object(gui.messagebox, "showinfo", record("info")),
            mock.patch.object(gui.messagebox, "showwarning", record("warning")),
            mock.patch.object(gui.messagebox, "showerror", record("error")),
            mock.patch.object(gui.simpledialog, "askstring", lambda *a, **k: self.args.serial),
        ]
        for patch in patches:
            patch.start()
        try:
            if self.connect():
                print("\n--- Read-only tests ---")
                self.read_only()
                if self.args.allow_write:
                    print("\n--- Write tests ---")
                    self.write_tests()
        except Exception as error:      # report it rather than lose the log
            self.check("Test run completed", "no exception", repr(error), False)
        finally:
            self.save()
            for patch in patches:
                patch.stop()
            self.app.on_close()
        return all(passed for *_, passed in self.results)

    def save(self):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        failed = [r for r in self.results if not r[3]]
        with open("hil_report_%s.md" % stamp, "w", encoding="utf-8") as report:
            report.write("# SeaSense HIL test %s\n\n" % time.strftime("%Y-%m-%d %H:%M"))
            report.write("App %s, port %s @ %s, address %s, writes %s\n\n" % (
                gui.APP_VERSION, self.args.port, self.args.baud, self.args.address,
                "ENABLED" if self.args.allow_write else "off"))
            report.write("**%d passed, %d failed**\n\n" % (len(self.results) - len(failed), len(failed)))
            report.write("| Result | Test | Expected | Actual |\n|---|---|---|---|\n")
            for name, expected, actual, passed in self.results:
                report.write("| %s | %s | %s | %s |\n" % ("PASS" if passed else "**FAIL**", name,
                                                         expected, str(actual).replace("|", "/")))
        with open("hil_raw_%s.txt" % stamp, "w", encoding="utf-8") as raw:
            raw.write(self.app.raw_text.get("1.0", "end"))
            raw.write("\n===== Parsed replies =====\n")
            raw.write(self.app.parsed_text.get("1.0", "end"))
        print("\n%d passed, %d failed.  Saved hil_report_%s.md and hil_raw_%s.txt"
              % (len(self.results) - len(failed), len(failed), stamp, stamp))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port")
    parser.add_argument("--address", default="001")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--serial", default=None)
    parser.add_argument("--scan", default="1-10")
    parser.add_argument("--allow-write", action="store_true")
    args = parser.parse_args()
    sys.exit(0 if Runner(args).run() else 1)


if __name__ == "__main__":
    main()
