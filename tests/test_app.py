"""
test_app.py - runs the REAL app (window hidden) against fake lights.

No hardware needed.  Every sequence a user can start is driven the same way
a button click would, and the results are checked both in the app (log,
summary table, dialogs) and in the fake light's settings.

    python -m unittest discover -s tests -v

Needs a display for Tk (normal on Windows; on a headless Linux box use
xvfb-run).  If Tk cannot start, these tests are skipped, not failed.
"""

import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import serial                      # noqa: E402
import tkinter as tk               # noqa: E402

import seasense_gui as gui         # noqa: E402
from fake_light import FakeBus, FakeLight, FakeSerial   # noqa: E402


class AppTestCase(unittest.TestCase):
    """Starts the app on a fake bus.  Dialogs are replaced by recorders:
    every yes/no question is answered "yes" and every message is kept in
    self.dialogs so tests can check what the user would have seen."""

    port_type = "usb"
    lights = None          # None = one light at address 001
    echo = False
    show_window = False    # the slider only reports moves while it is on screen

    def setUp(self):
        self.bus = FakeBus(lights=self.lights() if self.lights else None, port_type=self.port_type)
        self.dialogs = []
        self.typed_serial = None           # what the user "types" when asked for a serial number

        def fake_serial(port, baud, **settings):
            self.serial = FakeSerial(self.bus, port, baud, echo=self.echo)
            return self.serial

        patches = [
            mock.patch.object(serial, "Serial", fake_serial),
            mock.patch.object(gui.messagebox, "askyesno", self._record("ask", True)),
            mock.patch.object(gui.messagebox, "showinfo", self._record("info")),
            mock.patch.object(gui.messagebox, "showwarning", self._record("warning")),
            mock.patch.object(gui.messagebox, "showerror", self._record("error")),
            mock.patch.object(gui.simpledialog, "askstring",
                              lambda *a, **k: self._record("askstring")(*a) or self.typed_serial),
            mock.patch.object(gui, "SCAN_TIMEOUT", 0.03),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        try:
            self.app = gui.App()
        except tk.TclError as error:
            if os.environ.get("REQUIRE_TK"):    # set on the build machine: never skip there
                raise
            self.skipTest("Tk cannot start here: %s" % error)
        if not self.show_window:
            self.app.withdraw()
        self.addCleanup(self._close)
        self.app.port_var.set("FAKE")
        self.app.connect()
        self.pump(0.1)

    def _record(self, kind, answer=None):
        def recorder(*args, **kwargs):
            self.dialogs.append((kind, " ".join(str(a) for a in args[1:])))
            return answer
        return recorder

    def _close(self):
        try:
            self.app.on_close()
        except tk.TclError:
            pass

    # -- helpers ---------------------------------------------------------------
    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.update()
            time.sleep(0.005)

    def pump_until(self, condition, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.update()
            if condition():
                return True
            time.sleep(0.005)
        self.fail("timed out; raw log:\n" + self.raw_log()[-3000:])

    def idle(self):
        """Wait until the worker has nothing left to send."""
        worker = self.app.worker
        self.pump(0.05)
        self.pump_until(lambda: worker.requests.empty() and not worker.busy
                        and worker.events.empty())
        self.pump(0.1)

    def raw_log(self):
        return self.app.raw_text.get("1.0", "end")

    def sent(self):
        return [line for line, _ in self.bus.log]

    def dialog(self, kind):
        return [text for k, text in self.dialogs if k == kind]


# ---------------------------------------------------------------------------
class ReadTests(AppTestCase):

    def test_read_all_fills_summary_and_port(self):
        self.app.read_all()
        self.idle()
        summary = {name: label.cget("text") for name, label in self.app.summary.items()}
        self.assertEqual(summary["Model"], "LSL-1000")
        self.assertEqual(summary["Serial number"], "12-049-05581")
        self.assertEqual(summary["Firmware version"], "1.4.1006")
        self.assertEqual(summary["Serial port"], "RS2,1 = RS-232, 9600 baud")
        self.assertEqual(summary["Termination resistor"], "0 = Off")
        self.assertEqual(summary["Internal temperature"], "24.88 °C")
        self.assertNotIn("ERROR", self.raw_log())

    def test_read_value_moves_slider_without_sending(self):
        self.bus.light().lout = 37
        self.app.send("lout", "?")
        self.idle()
        self.assertEqual(int(self.app.slider.get()), 37)
        self.assertFalse([line for line in self.sent() if "lout=" in line.lower()])

    def test_chsw_on_dual_unit_reports_no_reply(self):
        self.app.send("chsw", "?")
        self.idle()
        parsed = self.app.parsed_text.get("1.0", "end")
        self.assertIn("NO REPLY", parsed)


class SliderTests(AppTestCase):
    show_window = True

    def test_settle_time_sends_only_final_value(self):
        self.app.settle_ms_var.set("250")
        for value in range(100, 39, -1):
            self.app.slider.set(value)
            self.pump(0.01)
        self.idle()
        self.pump(0.4)
        self.idle()
        writes = [line for line in self.sent() if "lout=" in line]
        self.assertEqual(len(writes), 1, writes)
        self.assertEqual(self.bus.light().lout, 40)

    def test_zero_settle_sends_every_step(self):
        self.app.settle_ms_var.set("0")
        for value in range(41, 46):
            self.app.slider.set(value)
            self.pump(0.15)
        self.idle()
        writes = [line.split("=")[1].split("*")[0] for line in self.sent() if "lout=" in line]
        self.assertEqual(writes, ["41", "42", "43", "44", "45"])


class OneStepUsbTests(AppTestCase):
    """Internal USB port, FW 1.4.1006: no FACT unlock; SAVE answers 'System
    settings written' after FSET and '?' after FTEN / FPRT."""

    def test_configures_and_verifies(self):
        self.app.one_step_setup()
        self.pump_until(lambda: self.dialog("info") or self.dialog("warning") or self.dialog("error"))
        self.assertTrue(self.dialog("info"), self.dialogs)
        light = self.bus.light()
        self.assertEqual((light.serial_number, light.dimming, light.color, light.beam),
                         ("12-049-05581", "SER", "DW00", "FLD"))
        self.assertEqual(light.port, ("RS4", 1))
        self.assertEqual(light.termination, 1)
        for item in ("Serial number", "Dimming", "Serial port", "Termination"):
            self.assertIn("SETUP  OK   " + item, self.raw_log().replace("SETUP OK", "SETUP  OK"))

    def test_save_replies_match_hardware(self):
        # HIL run 2026-10-16, USB, FW 1.4.1006: the two SAVEs of one-step setup
        self.app.one_step_setup()
        self.pump_until(lambda: self.dialog("info") or self.dialog("error"))
        saves = [reply for line, reply in self.bus.log if line.lower() == "!001:save"]
        self.assertEqual(saves, ["System settings written\r\n", "?\r\n"])
        self.assertTrue(self.dialog("info"), self.dialogs)

    def test_save_refusal_does_not_stop_it(self):
        # Seen on the real board too: SAVE answered "?" even after FSET
        self.bus.refuse.add("save")
        self.app.one_step_setup()
        self.pump_until(lambda: self.dialog("info") or self.dialog("error"))
        self.assertFalse(self.dialog("error"))
        sent = [line.lower() for line in self.sent()]
        self.assertIn("!001:fprt=rs4,1", sent)            # reached the end of the sequence

    def test_fset_refused_stops_before_port_change(self):
        self.bus.refuse.add("fset")
        self.app.one_step_setup()
        self.pump_until(lambda: self.dialog("error"))
        self.assertIn("FSET", self.dialog("error")[0])
        self.assertFalse([line for line in self.sent() if "fprt=" in line.lower()])
        self.assertEqual(self.bus.light().port, ("RS2", 1))

    def test_user_can_cancel_at_confirmation(self):
        with mock.patch.object(gui.messagebox, "askyesno", lambda *a, **k: False):
            self.app.one_step_setup()
            self.pump(0.5)
            self.idle()
        self.assertFalse([line for line in self.sent() if "fset" in line.lower()])


class OneStepBareBoardTests(AppTestCase):
    lights = staticmethod(lambda: [FakeLight(bare=True)])

    def test_asks_for_serial_and_uses_defaults(self):
        self.typed_serial = "12-049-99999"
        self.app.one_step_setup()
        self.pump_until(lambda: self.dialog("info") or self.dialog("error") or self.dialog("warning"))
        self.assertTrue(self.dialog("askstring"))
        light = self.bus.light()
        self.assertEqual((light.serial_number, light.dimming, light.color, light.beam),
                         ("12-049-99999", "SER", "DW00", "FLD"))
        self.assertTrue(self.dialog("info"))

    def test_cancelled_serial_prompt_sends_nothing(self):
        self.typed_serial = None
        self.app.one_step_setup()
        self.pump(0.5)
        self.idle()
        self.assertFalse([line for line in self.sent() if "fset" in line.lower()])


class OneStepRs485Tests(AppTestCase):
    """External serial pins: factory commands need the FACT unlock."""
    port_type = "rs485"

    def test_unlocks_then_configures(self):
        self.app.one_step_setup()
        self.pump_until(lambda: self.dialog("info") or self.dialog("warning") or self.dialog("error"))
        self.assertTrue(self.dialog("info"), self.dialogs)
        sent = [line.lower() for line in self.sent()]
        self.assertLess(sent.index("!001:fact=7q2b"), sent.index(next(l for l in sent if l.startswith("!001:fset="))))
        self.assertEqual(self.bus.light().port, ("RS4", 1))


class FactoryTabTests(AppTestCase):

    def test_port_to_rs232_on_serial_pins_stops_quietly(self):
        self.bus.port_type = "rs485"
        self.app.fprt_port_var.set("RS2 = RS-232")
        self.app.factory_port()
        self.idle()
        self.assertEqual(self.bus.light().port, ("RS2", 1))
        self.assertNotIn("ERROR", self.raw_log())          # silence after FPRT is expected
        self.assertIn("If replies stopped after FPRT", self.raw_log())

    def test_flmx_write_and_read_back(self):
        self.app.flmx_var.set("5")
        self.app.factory_write_flmx()
        self.idle()
        self.assertEqual(self.bus.light().flmx, 5)
        self.assertEqual(self.app.flmx_var.get(), "5")


class BusTests(AppTestCase):
    lights = staticmethod(lambda: [FakeLight(1), FakeLight(5, "12-049-00005")])

    def test_scan_finds_only_real_lights(self):
        self.app.scan_bus()
        self.pump_until(lambda: "Scan finished" in self.raw_log(), timeout=60)
        self.assertIn("Lights found at: 001, 005", self.raw_log())

    def test_address_change_follows_the_light(self):
        self.app.address_var.set("005")
        self.app.new_address_var.set("007")
        self.app.change_address()
        self.idle()
        self.assertEqual(self.app.address_var.get(), "007")
        self.assertEqual(sorted(l.address for l in self.bus.lights), [1, 7])


class EchoAdapterTests(AppTestCase):
    echo = True                    # adapter repeats what we send

    def test_replies_still_parsed(self):
        self.app.send("temp", "?")
        self.idle()
        self.assertIn("ECHO", self.raw_log())
        self.assertIn("24.88", self.app.parsed_text.get("1.0", "end"))


class CloseTests(AppTestCase):

    def test_close_releases_port_even_mid_scan(self):
        self.app.scan_bus()
        self.pump(0.3)
        port = self.serial
        self.app.on_close()
        self.assertFalse(port.is_open)


if __name__ == "__main__":
    unittest.main()
