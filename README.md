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
| Status | INFO, STAT, temperature, humidity, decoded summary, optional auto-poll |
| Presets | Save / recall / clear presets 1–16, power-on boot preset |
| Dimming curve | Drag A, B, C, D and YMIN/YMAX on a graph, then **Send curve** |
| Setup | Change node address, group membership, light baud rate, factory reset |
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

(`XXXX` = the 4 characters the light replied with.) The guides for the internal
USB port do not use FACT. The app always unlocks first; it is harmless on USB.

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
bus. Follow with `!001:SAVE`.

### Maximum output limit (FLMX)

```
!001:FLMX=80
```

```
!001:SAVE
```

Range 10-100; `!001:FLMX?` reads it. DeepSea's LSL-1000 chart shows input power
from about 27 W at 10 to roughly 135-170 W at 100, depending on line voltage
(100-140 VAC). Their note: these are estimates, measure before relying on them.

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

## Files

| File | Purpose |
|------|---------|
| `seasense_gui.py` | The window and the serial worker thread |
| `seasense_protocol.py` | Command building, checksum, escapes, reply decoding (no GUI) |
| `test_protocol.py` | Unit tests, including the spec's checksum examples |
| `.github/workflows/build.yml` | Builds the Windows .exe on every push |
