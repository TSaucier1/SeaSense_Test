# SeaSense protocol: field notes beyond the spec

Reference for developers integrating DeepSea LED SeaLite lights into a larger
control system. Everything here is behaviour that is **missing from, different
from, or not obvious in** the *SeaSense Serial Protocol Specification*
792-004-013-0A-01 Rev C (2020). Read the spec first; this file covers the gaps.

## Where this comes from

| Unit | Firmware | Notes |
|---|---|---|
| LSL-1000 (AC, 100-140 VAC), original driver board | 1.4.1001 | Shipped with dimming option `PV`; reconfigured to `SER`. Board later destroyed (wiring mistake, see §7) |
| LSL-1000 replacement driver boards (2 boards) | 1.4.1006 | Arrived with a bare flash, configured from the SeaSense Light Tester app |

Connections used: the light's **internal micro-USB port** (shows up in Windows as
"USB Serial Device") and the **external RS-485 pins** through a Gearmo FTDI
USB-RS-485 adapter and an RS-485 isolator. Other sources: DeepSea service guides
("Configuring a Spare LSL-2000 Driver Board", "Changing Serial Configurations",
"Changing the Termination Resistor Setting", "Update Maximum LED Output Settings
over USB", "Setting an LSL-2000 for a Custom Configuration") and DeepSea support
(email, Oct 2026). Dates: 13-16 Oct 2026.

**Confidence tags** used below:

- **[Captured]** exact bytes recorded on hardware. Evidence in `docs/`, and
  reproduced byte for byte in `test_protocol.py` / `tests/test_app.py`.
- **[Observed]** seen on hardware (screenshots, manual tests), bytes not archived.
- **[DeepSea]** from DeepSea's service guides or support, not tested by us.
- **[Unverified]** our best inference. Treat as an assumption until tested.

Unless stated otherwise, captures are from FW 1.4.1006 over the internal USB port.

---

## 1. Reply framing

1. **Every reply starts with `!` + LF (`21 0A`).** The spec shows replies
   starting directly with data. **[Captured]**
   - ACK = `!\n\r\n` (spec: `\r\n`)
   - NAK = `!\n?\r\n` (spec: `?\r\n`)
   - Data = `!\n<data>\r\n`, for example `!\n24.88\r\n`
2. **A bare `!\n` with nothing after it means nobody answered.** It is not an
   empty reply and not an ACK. **[Captured]** It comes back for:
   - a command addressed to a node that does not exist (`!002:INFO?` with only 001 present)
   - a **wrong checksum** (spec says the node stays silent; it actually sends `!\n`)
   - a broadcast read (`!000:STBY?`), which the spec forbids
   - `CHSW?` on a dual-output (CH1+2) unit, which simply ignores it (§3)
3. **The `!\n` appears to be sent for every command frame on the bus**, whoever
   it is addressed to. **[Captured]** over USB with one light. **[Unverified]**
   what happens on a multi-drop RS-485 bus with several lights: each may transmit
   `!\n` for every frame, and the frames could collide. Test this before
   deploying more than one light per bus, and don't rely on the prefix to mean
   "my light is present".
4. **An empty line after the prefix is ambiguous.** `!\n\r\n` is an ACK for a
   write, but for a read it is **empty data**. `ADGR?` with no group
   memberships answers exactly `!\n\r\n`. Decide by the access code you sent
   (`?` = data, anything else = ACK). **[Captured]**
5. **`LOUT?` ends in CR LF**, not CR only as the spec shows (`100<CR>`).
   **[Captured]** Accept either.
6. **Replies can arrive split across reads.** Most replies arrived in one chunk,
   but one came as `!` and then `\n?\r\n` about 1 ms later. Always buffer until
   the reply is complete. **[Captured]**

## 2. Knowing when a reply is complete

1. **Multi-line replies have no consistent terminator.**
   - **STAT:** 2 lines (header + values). The spec says an extra blank line
     ends the table; the real light sends **no blank line**. **[Captured]**
   - **PSET?:** a header + 16 rows, also **no blank line**. **[Captured]**
   - **BOOT?:** a header + 1 row. **[Captured]**

   So "stop at the first CR LF" truncates them, and "wait for a blank line"
   hangs.
2. **What works:** read until the line has been quiet for a short gap after the
   last byte. The Tester uses a **50 ms quiet gap**, a **0.5 s** first-byte
   timeout and a **2 s** cap per reply. If all you have is the `!\n` prefix (or
   your own adapter echo), keep waiting until the first-byte timeout before
   calling it "no reply". **[Captured]** over USB.
3. **Timing (USB):** the first byte arrives 1-17 ms after the command, and the
   whole reply usually in one chunk. **[Captured]** Through the RS-485 adapter at
   9600 baud (about 1.04 ms per character) the app behaved the same in manual
   tests **[Observed]**, but the byte timing has **not been captured**. Allow
   more margin there.
4. **Send one command at a time.** The protocol is strict command/response. With
   a short timeout, a late reply can land on the *next* command. This happened
   in testing: a scan reported the wrong addresses. Match replies to requests
   where you can (§3.1).

## 3. Per-command reply details

| Command | Spec says | Real light | Tag |
|---|---|---|---|
| `ADDR?` | address | zero-padded: `!\n001\r\n` | Captured |
| `INFO?` | 8 fields for LSL | 9 fields: extra 4th option `CH1+2` on dual-output units: `001,LSL-1000,12-049-05581,0A,1.4.1006,DW00,FLD,SER,CH1+2` | Captured |
| `INFO?` on a bare board | n/a | blank fields: `001,LSL-1000,,0A,1.4.1006,,,,CH1+2` (serial number, color, beam, dimming all empty) | Observed |
| `STAT?` | 5 columns, `0.5hr <TAB>24 ...` with spaces | 8 tab-separated columns, no spaces, no trailing blank line: `RunTime PrCycle MaxTemp Temp1 Temp2 Temp3 Humid FLAGS` / `1.1hr 12 102.3C 24.9C 22.2C 21.2C 29% 0x0000` | Captured |
| `TEMP?` | integer °C (`27`) | two decimals: `24.88` | Captured |
| `RHUM?` | integer % (`40`) | two decimals: `29.10` | Captured |
| `LOUT?` | `100<CR>` | `100<CR><LF>` | Captured |
| `CURV?` | `1,100,1,5,70,95,4.5` | exponent with 2 decimals: `1,100,0,3,53,92,4.50` (also this unit's factory curve, not the spec default) | Captured |
| `BOOT?` | `(PRESET): <TAB>params...` | header row first, then a row **ending in a stray tab**: `Preset\tSTBY\tLOUT\r\n0:\t0\t1\t\r\n`. Drop trailing empty cells | Captured |
| `PSET?` | header, row `0:` (factory default), then presets; CHSW column on dual units; blank line at end | header, **rows 1-16 only** (no `0:` row), **no CHSW column even on a CH1+2 unit**, no blank line | Captured |
| `ADGR?` (no groups) | empty list + EoF | `!\n\r\n`, which looks like an ACK (§1.4) | Captured |
| `BAUD?` | code | `!\n1\r\n` (code 1 = 9600) | Captured |
| `CHSW?` on CH1+2 unit | `1<CR>` | **no answer** (bare `!\n`); CHSW writes untested | Captured |
| `HELP?`, `SAVE?`, unknown | NAK | NAK `!\n?\r\n`. There is **no SAVE query** | Captured |

1. **Scanning for lights:** `ADDR?` answers with the light's own address. Count
   a light as present only if the reply **equals the address you asked**.
   Anything else (`!\n` alone, a late reply for another address) means absent.
   **[Captured]**
2. **Case:** commands work in upper or lower case (`!001:lout?*19`,
   `!001:INFO?`). **[Captured]**
3. **Original board (FW 1.4.1001, mains-powered AC LSL-1000)** had dimming
   `PV` and curve `1,100,10,15,50,99,4.00`. **[Observed]** `PV` isn't in any
   DeepSea document; it is probably phase (TRIAC) dimming. **[Unverified]**

## 4. Spec errors

1. **Four checksum examples in Rev C are misprinted.** Correct values (verified
   by calculation, and the light accepts our computed checksums): **[Captured]**
   - `!010:stat?*11` (spec prints `*01`)
   - `!010:curv?*15` (spec prints `*19`)
   - `!005:chsw+*FA` (spec prints `*FC`)
   - `!010:rset=4a93*12` (spec prints `*DF`)
2. **31-character command limit:** the light accepted
   `!001:FSET=12-049-05581,SER,DW00,FLD` (35 characters, no checksum).
   **[Captured]** A full `CURV=` write with a checksum is 32 characters. The
   Tester leaves the checksum off whenever a command would exceed 31 characters.
   Whether the light rejects long commands *with* a checksum is untested.

## 5. Factory commands (not in the spec at all)

These come from DeepSea's service guides and support, plus our tests. None
appear in Rev C.

| Command | Purpose | Notes |
|---|---|---|
| `FACT?` / `FACT=<code>` | unlock factory commands | `FACT?` returns a one-time 4-character code; send it straight back with `FACT=`. **Needed on the external RS-232/485 pins [DeepSea]; not needed on the internal USB port**, where `FACT?` answers `?` and factory commands work without an unlock **[Captured]**. One unlock covers several following commands **[DeepSea]** |
| `FSET=<serial>,<dimming>,<color>,<beam>` | unit identity / dimming type | **Field order differs from INFO** (serial, dimming, color, beam). Takes effect immediately: INFO shows the new values before any SAVE **[Captured]** |
| `FPRT=<RS4\|RS2>,<baud code>`, `FPRT?` | serial interface + baud | `RS4` = RS-485, `RS2` = RS-232. Baud codes as BAUD: 1 = 9600, 2 = 19200, 3 = 57600. Reply `RS4,1` **[Captured]**. **Takes effect at once:** on the external port being changed, the light stops answering immediately; DeepSea says to send nothing more and power-cycle **[DeepSea]**. Over USB the link is unaffected **[Captured]**. Kept across a power cycle without SAVE **[Captured]** |
| `FTEN=<0\|1>`, `FTEN?` | **RS-485 termination resistor** | Not a "factory enable". `1` = on (single light, or last light on the cable), `0` = off (lights mid-bus) **[DeepSea]**. Kept across a power cycle without SAVE **[Captured]** |
| `FLMX=<1-100>`, `FLMX?` | maximum output (power) limit | Then `SAVE` **[DeepSea]**. Read verified (`100`) **[Captured]**; write untested on hardware. LSL-1000 (AC) chart: about 27 W at 10 to about 135-170 W at 100 depending on line voltage. LSL-2000 (DC) chart: about 1.2 W per step, about 117 W at 100 on 20-48 V; on a 10-24 V supply it caps near 60 W, so values above about 50 do nothing **[DeepSea]** |
| `SAVE` (no access code) | write settings to non-volatile memory | See §6. Not to be confused with `PSET+n` (save a user preset) |

1. **FSET codes accepted** (service guide) differ from the INFO codes printed in
   the spec's Appendix E: **[DeepSea]**
   - Dimming: `NON`, `5V`, `10V`, `20I`, `SER` (spec: `A05`, `A10`, `A20`, `SER`)
   - Color: `DW00`, `DW90`, `WW00`, `G528`, `B485`, `R625` (spec also lists
     `BL475`, `CW00`, UV, IR)
   - Beam: `FLD`, `WFD`, `MED`, `SPT`
2. **After FSET to `SER`, a bare board's port defaults to RS-232 at 9600.**
   **[DeepSea + Observed]** It won't answer on RS-485 until `FPRT=RS4,1`. This
   cost us most of a day.
3. **Configuring a light from blank** (verified twice, on bare boards over USB):
   **[Captured]**
   ```
   !001:FSET=<serial>,SER,<color>,<beam>
   !001:SAVE
   !001:FTEN=1
   !001:FPRT=RS4,1
   !001:SAVE
   ```
   Then read back with `INFO?`, `FPRT?`, `FTEN?`, power-cycle, and read again.
   **Send FTEN before FPRT**, so that losing the link after FPRT on an external
   port doesn't drop the FTEN write.
4. **Custom settings files** from DeepSea are meant to be sent with a
   **100 ms delay per character and 500 ms per line**. **[DeepSea]**
5. **Checksums on factory commands:** all of the above were sent *without* a
   checksum, as in DeepSea's guides. Whether they accept one is untested.

## 6. SAVE: what its reply means

**`?` from SAVE means "nothing to save", not "refused".** **[Captured]**
(`docs/probe_20261016_223932.json`)

| Sequence | SAVE reply |
|---|---|
| SAVE, SAVE | `?`, `?` |
| FSET with the values it already has, then SAVE | `?` |
| LOUT=10, LOUT=1 (back to the old value), then SAVE | `System settings written` |
| SAVE again | `?` |
| FTEN=1 (unchanged), then SAVE | `?` |
| FTEN / FPRT changed on a bare board, then SAVE | `?`, but both settings were kept after a power cycle **[Observed]** |
| FSET with new values, then SAVE (FW 1.4.1001) | `System settings written` **[Observed]** |

1. SAVE answers `System settings written` only if something was written since
   the last SAVE.
2. **Any LOUT write counts**, even one that ends at the previous value.
3. **FSET with unchanged values doesn't count.**
4. **FTEN and FPRT never count**: they store themselves immediately.
5. **Never treat SAVE's reply as success or failure.** Verify by reading the
   setting back, and after a power cycle for anything that must persist. Our
   first one-step setup failed by stopping on a `?` from SAVE on an
   already-configured light.
6. **[Unverified]** FSET with *new* values, then SAVE, on FW 1.4.1006 (assumed
   "written", as on 1.4.1001). Also unknown:
   - whether `STBY=`, `CURV=`, `PSET`, `BOOT=` or `ADGR` writes count as unsaved changes
   - whether an unsaved LOUT survives a power cycle (the spec's BOOT/PSET model
     suggests the power-on level comes from the boot preset)
   - whether SAVE after every LOUT change wears the flash

   For a dimming slider in an integrated system, don't SAVE on every move.

## 7. Ports, power and models

1. **Internal micro-USB port** (driver board): enumerates as a USB serial port.
   Runs at 9600 8N1 regardless of the external port settings, and needs no
   FACT unlock (§5). The board runs from USB power alone, so it can be
   configured without its DC/AC supply. **[Captured]** DeepSea: don't power the
   DC terminals while on USB unless the PC is isolated. **[DeepSea]**
2. **LSL-1000 is AC-powered (100-140 VAC); LSL-2000/2025/2050/2075 are DC
   (10-48 V).** **[DeepSea]** The LSL-1000's electronics can be mains-referenced:
   plugging the USB cable into a mains-powered LSL-1000 shut it down.
   **[Observed]** **Never connect the internal USB port with mains applied.**
3. **RS-485:** 2-wire, half duplex. Adapters may echo your own transmission. If
   received bytes start with exactly what you sent, strip it. A USB isolator or
   isolated RS-485 adapter is recommended. **[DeepSea]**
4. **Wiring warning:** the first driver board was destroyed by a mix-up between
   the RS-485 cable shield and the opto-isolator's grounds. **[Observed]** Keep the
   shield bonded to chassis or earth at **one end only**, never to RS-485 signal
   ground, and run signal ground through the isolated side.
5. **USB-RS-485 adapters label A/B inconsistently.** If nothing answers, swap the
   pair before suspecting anything else.

## 8. Behaviours the spec covers but an integrator should plan for

1. **Changing the address:** the light stops answering at the old address
   immediately. Update your own address table as soon as the ACK arrives.
   **[Unverified]** that the ACK itself comes from the old address (assumed).
2. **Changing baud with BAUD=:** per the spec, the ACK comes at the old rate and
   then the light switches. Reopen your port at the new rate after the ACK.
   **[Unverified]** on hardware.
3. **Factory reset (RSET):** two-step key exchange; nothing else may be sent in
   between. Our implementation sends the key straight back with no other
   traffic. **[Unverified]** on hardware.
4. **Broadcast (000) and group (301-332) writes** get no reply beyond `!\n`
   (§1.2). Confirm with individual reads.
5. **No unsolicited traffic** was ever seen: the light only talks when spoken to.
   **[Captured]**

## 9. Checklist for an integration driver

- [ ] Strip the leading `!\n`; treat a bare `!\n` as "no answer".
- [ ] Decide ACK vs empty data by the access code you sent.
- [ ] Detect the end of a reply by a quiet gap (≥ 50 ms; more on RS-485), not by
      CR LF or a blank line.
- [ ] One command in flight at a time; match replies to the request.
- [ ] Strip adapter echo when present.
- [ ] Accept CR or CR LF line endings; drop trailing empty table cells.
- [ ] Parse numbers with decimals for TEMP / RHUM / CURV.
- [ ] Scan by matching `ADDR?` replies to the address asked.
- [ ] Factory commands: send `FACT?`, unlock only if a 4-character code comes back.
- [ ] Never judge success by SAVE's reply; read the value back.
- [ ] Send FPRT last in any configuration sequence.
- [ ] Don't SAVE on every dimming step.
- [ ] Test multi-light RS-485 buses for collisions from the `!\n` prefix.

## Evidence in this repository

- `docs/probe_20261016_223932.json`: the SAVE sequence, byte for byte with timing.
- `docs/hil_raw_20261016_221122.txt`, `docs/hil_report_*.md`: full
  hardware-in-the-loop runs (read-only and write, including a power cycle).
- `test_protocol.py` (`test_real_light_replies`, `test_probe_capture_fw_1_4_1006`,
  `test_factory_replies`): captured replies as unit tests.
- `tools/fake_light.py`: a working model of all of the above, used by
  `tests/test_app.py`.
- `tools/seasense_probe.py`: re-capture any of this on a new light or firmware.
