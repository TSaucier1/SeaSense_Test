# SeaSense_Test
DSPL SeaSense Test App

A small stand-alone Windows GUI for bench-testing DeepSea Power & Light LED
SeaLite lights (LSL-1000 and similar) over RS-485 / RS-232 using the
SeaSense serial protocol (792-004-013-0A-01 Rev C).

## Getting the .exe

The Windows build is made automatically by GitHub Actions; no Python install needed.
Every push to `main` publishes a new release with the .exe attached.

- **Releases** (right side of the repo page) → latest release → download `SeaSenseTester.exe`.
- Or **Actions** tab → latest "Build Windows exe" run → **Artifacts** → `SeaSenseTester-windows` (zip).

Windows SmartScreen may warn about an unsigned app the first time: *More info → Run anyway*.

## Wiring (2-wire RS-485)

| Adapter terminal | Light   |
|------------------|---------|
| D+ / A / T/R+    | + / A   |
| D- / B / T/R-    | - / B   |
| GND              | GND     |

Serial settings are fixed by the spec: 8 data bits, no parity, 1 stop bit,
no flow control. Lights ship at 9600 baud. If nothing answers, try swapping A and B.

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
  does not show. The app strips it before decoding.
- The spec shows `lout?` and `chsw?` replies ending in `<CR>` only; the app
  treats a reply as finished after 50 ms of silence, so any ending works.
- The dimming-curve graph is an **approximation** fitted to the spec's
  Figure 2 (the spec gives no formula): flat at YMIN from A to B, straight line
  from B to C, power curve `YMIN + (YMAX-YMIN)·(x/D)^e` from C to D, flat at YMAX
  after D. Serial dimming only uses B..D (LOUT 1 = B, LOUT 100 = D).

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
