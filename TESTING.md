# Testing the SeaSense Light Tester

Three layers, from fastest to most real. Run them in this order; only the last
one needs a light.

| Layer | Command | Needs | Time |
|---|---|---|---|
| 1. Protocol unit tests | `python -m unittest test_protocol.py` | nothing | < 1 s |
| 2. App tests on a fake light | `python -m unittest discover -s tests -v` | a display (normal on Windows) | ~25 s |
| 3. Hardware-in-the-loop | `python tools/hil_test.py COM3` | the light | ~30 s read-only, ~1 min with writes |

Layers 1 and 2 also run on every push to `main` (GitHub Actions, before the .exe
is built), so a broken sequence cannot reach a release.

## What each layer covers

**1. `test_protocol.py`** - checksum (against the spec's examples), command
building, escapes, and decoding of every reply format captured from real lights.

**2. `tests/test_app.py`** - the real app, window hidden, connected to
`tools/fake_light.py`, a fake serial port that answers the way the real lights
did (every behaviour in it is noted with the firmware / port it was captured on).
Covers: Read all, slider settle / every-step, one-step setup (USB, bare board,
RS-485 with FACT unlock, FSET refused, user cancels), SAVE answering `?`, Factory
tab FPRT / FLMX, bus scan, address change, echoing adapter, closing mid-scan.

**3. `tools/hil_test.py`** - the real app against the real light:

- Read-only (default, safe): INFO, STAT, TEMP, RHUM, FPRT?, FTEN?, ADGR?, BOOT?,
  CURV?, PSET? decode correctly, no app errors, bus scan finds only this light.
- `--allow-write` adds: LOUT=10 read back and restored; **one-step setup**
  (SER, RS-485 9600, termination on - answers "yes" to its confirmation); then it
  asks you to **power-cycle the light once** and checks serial number, dimming,
  port and termination were kept.

It writes `hil_report_<time>.md` (PASS / FAIL table) and `hil_raw_<time>.txt`
(every byte both ways, plus the parsed view). Exit code 0 = all passed.

To try the app by hand without hardware: `python tools/fake_light.py`
(add `rs485` to simulate the serial pins with the FACT unlock).

## Procedure for a local Claude Code session (PC with the light attached)

Paste this into the session:

```
Repo: https://github.com/TSaucier1/SeaSense_Test (pull main first). Read TESTING.md.
Light: LSL-1000 on COM3 = its internal micro-USB port, mains OFF, node 001, 9600.

1. pip install -r requirements.txt
2. Run layers 1 and 2. If anything fails, fix it before touching hardware.
3. Make sure the SeaSense app is closed (COM3 must be free), then run:
     python tools/hil_test.py COM3
   Read the report. Any FAIL: open hil_raw_*.txt, find the exact bytes.
4. ONLY if I say yes, run the write tests:
     python tools/hil_test.py COM3 --allow-write
   It will pause and ask me to power-cycle the light; wait for me.
5. For every difference between the real light and tools/fake_light.py:
   - change fake_light.py to match the real bytes (note firmware, port and date
     next to the change),
   - add or adjust a test in tests/test_app.py or test_protocol.py,
   - fix the app if it handled the real reply wrongly,
   - re-run layers 1 and 2 until they pass.
6. Never send ADDR=, BAUD=, RSET, FPRT= / FSET= with different values, or
   anything outside hil_test.py, without asking me first.
7. Commit (attach the hil_report_*.md in the message or a docs/ folder) and push
   to main; the build runs layers 1-2 again and publishes the .exe.
Report back: the hil_report table, what changed, and anything still unverified.
```

## Still not covered by any layer

- Exact reply timing through the RS-485 adapter at 9600 baud (all byte captures
  so far were over USB). Run `python tools/seasense_probe.py COM<n>` through the
  adapter, or `hil_test.py` on that port read-only, and compare with the USB run.

Checked by hand, not automated (2026-10-16): the app works the same over the
RS-485 adapter as over USB, and the light visibly dims with LOUT (mains on,
USB disconnected).
