# SeaSense_Test
DSPL SeaSense Test App

A small stand-alone Windows GUI for bench-testing DeepSea Power & Light LED
SeaLite lights (LSL-1000 and similar) over RS-485 / RS-232 using the
SeaSense serial protocol (792-004-013-0A-01 Rev C).

## Getting the .exe

The Windows build is made automatically by GitHub Actions; no Python install needed.
Every push to `main` publishes a new release with the .exe attached.

- **Releases** (right side of the repo page) → latest release → download `SeaSenseTester_v1.0.N.exe`.
- Or **Actions** tab → latest "Build Windows exe" run → **Artifacts** → `SeaSenseTester_v1.0.N` (zip).

Windows SmartScreen may warn about an unsigned app the first time: *More info → Run anyway*.

## Running on Linux

The .exe is Windows-only, but the app runs on Linux from the source code
(it was developed and tested on Linux).

Install Python's Tk and serial support (Debian / Ubuntu / Raspberry Pi OS):

```
sudo apt install python3-tk python3-serial
```

Fedora:

```
sudo dnf install python3-tkinter python3-pyserial
```

Get the code, either *Source code (tar.gz)* from a release, or:

```
git clone https://github.com/TSaucier1/SeaSense_Test.git
```

```
cd SeaSense_Test
```

Run it:

```
python3 seasense_gui.py
```

USB serial adapters show as `/dev/ttyUSB0` (or `/dev/ttyACM0`) in the COM port
list. If Connect says "permission denied", add yourself to the `dialout` group
once, then log out and back in:

```
sudo usermod -aG dialout $USER
```

## Wiring (2-wire RS-485)

| Adapter terminal | Light   |
|------------------|---------|
| D+ / A / T/R+    | + / A   |
| D- / B / T/R-    | - / B   |
| GND              | GND     |

Serial settings are fixed by the spec: 8 data bits, no parity, 1 stop bit,
no flow control, 9600 baud by default. If nothing answers, try swapping A and B, and
check the light's port is set to RS-485 (see Factory settings below - lights set to
SER default to RS-232).

## Using it

1. Pick the **COM port** and **baud**, click **Connect**.
2. Set the **Node address** (top bar) to the light you want to talk to.
   Change it any time to control a different light on the same bus.
   `000` = all lights (broadcast, no reply), `301`–`332` = group addresses.
   **Scan bus** asks every address 001–255 and lists which lights answer (~40 s).
3. Use the tabs:

| Tab | What it does |
|-----|--------------|
| Light | Output slider (0–100 in 1 % steps), quick levels, ±1 / ±coarse, standby |
| Status | INFO, STAT, temperature, humidity, serial port + termination, decoded summary, optional auto-poll |
| Presets | Save / recall / clear presets 1–16, power-on boot preset |
| Dimming curve | Drag A, B, C, D and YMIN/YMAX on a graph, then **Send curve** |
| Setup | **One-step setup** (serial number from INFO, SER, RS-485 9600, termination on, read-back check), node address, groups, baud rate, factory reset |
| Factory | FACT unlock, unit configuration (FSET), serial port + termination (FPRT / FTEN), output limit (FLMX) |
| Dual / Strobe | CHSW and PMOD/PEDG/PINV/PLEN (lights with those options only; others NAK) |

**Slider:** sends `!ADR:lout=N` once the slider has been still for the
settle time (default 250 ms). Set the settle time to 0 to send every 1 % step
as fast as the light acknowledges.

**Raw traffic panel:** every byte sent (TX, blue) and received (RX, green),
with control characters shown as `<CR>`, `<LF>`, `<TAB>`; tick *Also show hex*
for hex. *Save log…* writes it to a text file.

**Send box:** type the ASCII part (e.g. `!001:temp?`) and press Enter.
With *Append \r\n* ticked (default) the box adds CR LF as the message
leaves. `\r`, `\n`, `<CR>`, `[0x0D]` style escapes are also understood if you
want other control characters. Up/Down recalls previous commands.

**Parsed replies:** each reply decoded as ACK / NAK / data with labels.

**Options (top right):**
- *Add checksum* — appends `*XX` to button/slider commands (the checksum is optional
  in the protocol). It is left off automatically if a command would exceed the
  spec's 31-character limit (a full dimming-curve write).
- *Hide RS-485 adapter echo* — some RS-485 adapters echo what they send;
  if so the echo is shown grey as `ECHO` instead of confusing the reply.

**Safety:** changing address, baud rate, saving/clearing presets, writing the
curve and factory reset all ask for confirmation. After a baud change the app
switches the COM port to the new rate automatically once the light ACKs.

## Notes on the spec

- Four checksum examples in Rev C are misprinted. Correct values:
  `!010:stat?*11`, `!010:curv?*15`, `!005:chsw+*FA`, `!010:rset=4a93*12`.
- Real LSL-1000 lights (firmware 1.4.x) start every reply with `!<LF>`, which the spec
  does not show. The app strips it before decoding. Every light also sends that `!<LF>` for
  commands addressed to *other* nodes, so a bare `!<LF>` means "no light at this address".
  The bus scan counts a light only when `addr?` comes back with that same address.
- The spec shows `lout?` and `chsw?` replies ending in `<CR>` only; the app
  treats a reply as finished after 50 ms of silence, so any ending works.
- The dimming-curve graph is an **approximation** fitted to the spec's
  Figure 2 (the spec gives no formula): flat at YMIN from A to B, straight line
  from B to C, power curve `YMIN + (YMAX-YMIN)·(x/D)^e` from C to D, flat at YMAX
  after D. Serial dimming only uses B..D (LOUT 1 = B, LOUT 100 = D).

## Factory settings - from DeepSea's service guides

None of these are in the SeaSense protocol spec. They come from DeepSea's
service guides ("Configuring a Spare LSL-2000 Driver Board", "Changing Serial
Configurations (RS-485 to RS-232)", "Changing the Termination Resistor Setting",
"Update Maximum LED Output Settings over USB", "Modify LED SeaLite Settings over
USB", "Setting an LSL-2000 for a Custom Configuration") and from DeepSea support.
The app's **Factory** tab runs each sequence for you, sends everything without a
checksum (as the guides do), and stops if a step is refused.

### One-step setup (Setup tab)

For a serial RS-485 light, best over the internal USB port. Reads `INFO?` for the
serial number, LED color and beam (asks you to type the serial number if a freshly
flashed board has none; color / beam fall back to `DW00` / `FLD`), shows the plan,
then sends:

FACT unlock (if the light asks) -> `FSET=<serial>,SER,<color>,<beam>` -> `SAVE` ->
`FTEN=1` -> `FPRT=RS4,1` -> `SAVE` -> `INFO?` / `FPRT?` / `FTEN?`

and reports OK / FAIL for serial number, dimming, port and termination. It stops
if FSET or FTEN is refused; a `?` reply to SAVE is normal and does not stop it.
Power-cycle the light afterwards, connect on RS-485 and use *Read all* to confirm.

### LSL-1000 vs LSL-2000

The SeaSense protocol is the same for both (the spec covers "LSL-10XX, LSL-20XX"),
but they differ in power and in which guides apply:

- **LSL-1000 is AC powered** (100-140 VAC). Its electronics can be referenced to
  the mains: on our bench, plugging USB into a mains-powered LSL-1000 shut it down.
  Only use its internal USB port with mains disconnected, or through a USB isolator.
- **LSL-2000 / 2025 / 2050 / 2075 are DC powered** (10-48 VDC).
- The FSET / FPRT / FTEN guide is written for the LSL-2000 driver board. FSET and
  FPRT were confirmed on an LSL-1000 (FW 1.4.1001) on our bench and by DeepSea support.
- FLMX has a separate chart for each model (below).
- The LSL-2000 driver replacement kit (S11101) is for DC lights only. The endcap
  kits (S11105, S11106) and the light engine guide cover both models.

### Two ways to connect

- **External serial pins** on the bulkhead connector, through an RS-485 or RS-232
  converter (whichever the light's port is set to). A USB isolator is recommended.
- **Internal micro-USB port** on the driver board (housing open). Windows sees it
  as a USB serial adapter; 9600 8N1. LED0 blinks at 1 Hz once the light is running.
  DeepSea: do **not** power the driver board's DC terminals while on USB unless
  the PC is properly isolated - it can damage the PC.

### Unlocking factory settings (FACT)

On the external serial pins, factory commands only work after an unlock. Ask for
a one-time code, then send it straight back:

```
!001:FACT?
```

```
!001:FACT=XXXX
```

(`XXXX` = the 4 characters the light replied with.)

**When FACT is needed:** over the external RS-232 / RS-485 pins, yes. Over the
internal micro-USB port, no - there `FACT?` just answers `?` and factory commands
work straight away (confirmed on an LSL-1000, FW 1.4.1001). The app sends `FACT?`
first and only unlocks if the light gives a code, so it works either way.

### Unit configuration (FSET)

Serial number, dimming type, LED color, beam pattern - note the order differs
from the INFO reply:

```
!001:FSET=12-049-12345,SER,DW00,WFD
```

```
!001:SAVE
```

SAVE answers `System settings written`. Read INFO afterwards to confirm.

| Dimming | Code | | LED color | Code | | Beam | Code |
|---|---|---|---|---|---|---|---|
| No dimming | NON | | Daylight White | DW00 | | Flood (75°) | FLD |
| 0-5 V | 5V | | Daylight White 90 CRI | DW90 | | Wide Flood (115°) | WFD |
| 0-10 V | 10V | | Warm White | WW00 | | Medium (40°) | MED |
| 4-20 mA | 20I | | Green 520-535 nm | G528 | | Spot (35°) | SPT |
| Serial RS-232/485 | SER | | Blue 465-485 nm | B485 | | | |
| | | | Red 620-630 nm | R625 | | | |

Our AC-powered LSL-1000 reported `PV`, which is not in any DeepSea document we
have (probably phase / TRIAC dimming); `FSET=...,SER,...` changed it to serial.

### Serial port (FPRT) and termination resistor (FTEN)

**After FSET to SER the port defaults to RS-232 at 9600.** For RS-485:

```
!001:FPRT=RS4,1
```

`RS4` = RS-485, `RS2` = RS-232; then the baud code `1` = 9600, `2` = 19200,
`3` = 57600. FPRT takes effect immediately: if you send it through the external
port you are changing, the light stops answering there - power it down and
reconnect on the other converter. Read back with `!001:FPRT?`.

`FTEN` is the **RS-485 termination resistor**, not a "factory enable":

```
!001:FTEN=1
```

`1` = on, `0` = off, `!001:FTEN?` reads it. Turn it on for a single light or the
last light at the end of the cable; off for lights in the middle of a multi-light
bus.

**SAVE after FTEN / FPRT:** on a replacement LSL-1000 driver board (FW 1.4.1006,
internal USB port) `SAVE` answered `?` after FTEN and after FPRT, yet both settings
were kept - a later probe read back `RS4,1` and termination `1`. They store
themselves. SAVE answered `System settings written` after FSET on the original
board (FW 1.4.1001), but `?` after FSET on the FW 1.4.1006 board (that time the
values were unchanged). So the app never treats a SAVE reply as a failure: it
reads the settings back instead. Confirm with `!001:FPRT?` and `!001:FTEN?`, and
after a power cycle with *Read all*.

**Verified end to end:** that replacement board arrived with a bare flash and was
set up entirely from this app (FSET with serial number / SER / color / beam, then
RS-485 + termination); INFO, FPRT? and FTEN? confirmed the result. A second
bare board was then set up the same way with the fixed sequence (v1.0.12 and
later) and worked as expected, with no manual steps.

### Maximum output limit (FLMX)

```
!001:FLMX=80
```

```
!001:SAVE
```

`!001:FLMX?` reads it. DeepSea's guide has one chart per model (input power,
estimates - measure before relying on them):

| Model | Power | FLMX range on chart | Input power |
|---|---|---|---|
| LSL-1000 | AC, 100-140 VAC | 10-100 | ~27 W at 10 to ~135-170 W at 100, rising with line voltage |
| LSL-2000 | DC, 20-48 V supply | ~1-100 | ~1.2 W per step, ~117 W at 100 (measured max ~108 W) |
| LSL-2000 | DC, 10-24 V supply | ~1-50 useful | capped near 60 W; settings above ~50 add nothing |

### Custom settings files

DeepSea supplies custom configuration files to send with a terminal program,
with a **100 ms character delay and 500 ms line delay**. *Send file...* (next to
the Send box) does exactly that, sending each line as written in the file.

## Running from source / building locally

Needs Python 3.8+ on Windows.

```
pip install -r requirements.txt
```

```
python seasense_gui.py
```

To build the .exe yourself:

```
pip install pyinstaller
```

```
pyinstaller --onefile --windowed --name SeaSenseTester seasense_gui.py
```

The result is `dist\SeaSenseTester.exe`.

Unit tests (protocol only, no hardware needed):

```
python -m unittest test_protocol.py
```

## Probing a light (tools/seasense_probe.py)

A small read-only script, separate from the app, that sends each query and
records every byte that comes back with timestamps (saved to `probe_*.json`).
Use it to check exact reply formats on a new light or firmware. Close the app
first so the COM port is free:

```
python tools/seasense_probe.py COM3
```

It refuses `=`, `+` or `-` commands unless `--allow-write` is given.

Findings from an LSL-1000 (FW 1.4.1006) over its internal USB port, now in
the unit tests: every reply arrives as one chunk 1-16 ms after the command;
`ADGR?` with no groups answers an empty line (empty data, not an ACK);
`BOOT?` ends its value row with a stray tab; `CHSW?` on a CH1+2 unit, a wrong
checksum and a broadcast read all get only the `!<LF>` prefix; there is no
`SAVE?` query. Reply timing over RS-485 has not been measured yet.

## Files

| File | Purpose |
|------|---------|
| `seasense_gui.py` | The window and the serial worker thread |
| `seasense_protocol.py` | Command building, checksum, escapes, reply decoding (no GUI) |
| `test_protocol.py` | Unit tests, including the spec's checksum examples |
| `tools/seasense_probe.py` | Read-only probe that records a light's exact replies |
| `.github/workflows/build.yml` | Builds the Windows .exe on every push |
