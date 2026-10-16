"""
Unit tests for seasense_protocol.py.  Run with:

    python -m unittest test_protocol.py

The GitHub build runs these before making the .exe.
"""

import unittest

from seasense_protocol import (
    build_command, build_raw, bytes_to_display, checksum, identify_command,
    parse_escapes, parse_reply, strip_echo, expects_reply,
)

# Examples copied from the spec.  Four spec examples have wrong checksums
# (typos in the document) and are listed separately below.
SPEC_EXAMPLES = """
!010:addr?*F0 !010:addr=001*7F !010:adgr?*F3 !010:adgr=301*85 !010:adgr+301*73
!302:adgr+301*77 !010:adgr-301*75 !010:baud?*F1 !010:baud=3*22 !010:boot?*09
!010:boot=0*37 !301:boot=1*3B !010:info?*01 !010:pset?*11 !010:pset=1*40
!301:pset=2*44 !010:pset+16*64 !010:pset-3*32 !010:rhum?*11 !010:rset?*13
!010:stby?*17 !010:stby=1*46 !301:stby+*06 !000:stby=1*45 !301:stby-*08
!000:stby=0*44 !010:temp?*0B !010:lout?*19 !010:lout=100*A8 !005:lout+*09
!010:lout+2*37 !301:lout=50*7F !010:chsw?*0A !010:chsw=2*3A !010:chsw-*F8
!005:osdd*C4 !005:osds*D3 !020:vtst?*27 !005:vtst=1*59 !002:wbps*D3
""".split()

# (as printed in the spec, correct value)
SPEC_TYPOS = [
    ("!010:rset=4a93*DF", "12"),
    ("!010:stat?*01", "11"),
    ("!010:curv?*19", "15"),
    ("!005:chsw+*FC", "FA"),
]


class ChecksumTests(unittest.TestCase):
    def test_spec_examples(self):
        for example in SPEC_EXAMPLES:
            body, expected = example.split("*")
            self.assertEqual(checksum((body + "*").encode()), expected.encode(), example)

    def test_spec_typos_have_known_correct_values(self):
        for example, correct in SPEC_TYPOS:
            body = example.split("*")[0]
            self.assertEqual(checksum((body + "*").encode()), correct.encode(), example)


class BuildCommandTests(unittest.TestCase):
    def test_write_with_checksum(self):
        self.assertEqual(build_command(10, "lout", "=", 100), b"!010:lout=100*A8\r\n")

    def test_read_without_checksum(self):
        self.assertEqual(build_command("10", "LOUT", "?", use_checksum=False), b"!010:lout?\r\n")

    def test_immediate(self):
        self.assertEqual(build_command(5, "osdd"), b"!005:osdd*C4\r\n")


class EscapeTests(unittest.TestCase):
    def test_named(self):
        self.assertEqual(parse_escapes("!010:lout?<CR><LF>"), b"!010:lout?\r\n")

    def test_case_insensitive_and_tab(self):
        self.assertEqual(parse_escapes("a<tab>b<cr>"), b"a\tb\r")

    def test_hex_brackets_and_c_style(self):
        self.assertEqual(parse_escapes("x[0x0D][0a]\\r\\n\\x21\\\\"), b"x\r\n\r\n!\\")

    def test_unknown_name_kept(self):
        self.assertEqual(parse_escapes("<HELLO> <x>"), b"<HELLO> <x>")

    def test_build_raw_adds_checksum_before_eof(self):
        self.assertEqual(build_raw("!010:lout?<CR><LF>", add_checksum=True), b"!010:lout?*19\r\n")

    def test_build_raw_adds_eof(self):
        self.assertEqual(build_raw("!010:lout?", add_checksum=True, add_eof=True), b"!010:lout?*19\r\n")

    def test_build_raw_keeps_existing_checksum(self):
        self.assertEqual(build_raw("!010:lout?*19", add_checksum=True, add_eof=True), b"!010:lout?*19\r\n")

    def test_build_raw_exact(self):
        self.assertEqual(build_raw("hello"), b"hello")

    def test_display_round_trip(self):
        self.assertEqual(bytes_to_display(b"!010:lout?*19\r\n\x00\xff"), "!010:lout?*19<CR><LF><NUL>[0xFF]")


class ReplyTests(unittest.TestCase):
    def test_identify(self):
        self.assertEqual(identify_command(b"!010:lout?*19\r\n"), ("010", "lout", "?"))
        self.assertEqual(identify_command(b"garbage"), (None, None, None))

    def test_expects_reply(self):
        self.assertTrue(expects_reply("010"))
        self.assertFalse(expects_reply("000"))
        self.assertFalse(expects_reply("301"))

    def test_echo(self):
        self.assertEqual(strip_echo(b"AB", b"AB\r\n"), (b"AB", b"\r\n"))
        self.assertEqual(strip_echo(b"AB", b"\r\n"), (b"", b"\r\n"))

    def test_ack_nak_none(self):
        self.assertIn("ACK", parse_reply("lout", "=", b"\r\n")[0][1])
        self.assertIn("NAK", parse_reply("lout", "=", b"?\r\n")[0][1])
        self.assertIn("NO REPLY", parse_reply("lout", "=", b"")[0][1])
        self.assertIn("broadcast", parse_reply("lout", "=", b"", reply_expected=False)[0][1])

    def test_lout_cr_only(self):
        self.assertEqual(parse_reply("lout", "?", b"100\r")[1], ("Light output", "100 %"))

    def test_info(self):
        pairs = dict(parse_reply("info", "?", b"010,LSL-1000,12-049-000101,0A,1.1.1686,DW00,FLD,SER\r\n"))
        self.assertEqual(pairs["Model"], "LSL-1000")
        self.assertEqual(pairs["LED color"], "DW00 = Daylight White")
        self.assertEqual(pairs["Dimming control"], "SER = Serial RS-232/485")

    def test_stat(self):
        reply = b"RunTime \tPrCycle \tMaxTemp \tHumid \tFLAGS\r\n0.5hr \t24 \t93.7C \t44% \t0x0001\r\n\r\n"
        pairs = dict(parse_reply("stat", "?", reply))
        self.assertEqual(pairs["MaxTemp"], "93.7C")
        self.assertEqual(pairs["FLAGS"], "0x0001")

    def test_pset(self):
        reply = b"Preset\tSTBY\tLOUT\tCHSW\r\n0:\t0\t100\t1\r\n1:\t0\t50\t2\r\n\r\n"
        pairs = dict(parse_reply("pset", "?", reply))
        self.assertEqual(pairs["Preset 1"], "STBY=0, LOUT=50, CHSW=2")

    def test_curv(self):
        pairs = parse_reply("curv", "?", b"1,100,1,5,70,95,4.5\r\n")
        self.assertEqual(pairs[-1], ("e (exponent)", "4.5"))

    def test_real_light_replies(self):
        """Replies captured from a real LSL-1000 (FW 1.4.1001): every reply
        starts with "!" + LF, which the spec does not show."""
        from seasense_protocol import reply_text, is_ack
        self.assertEqual(reply_text(b"!\n1,100,10,15,50,99,4.00\r\n"), "1,100,10,15,50,99,4.00")
        self.assertTrue(is_ack(b"!\n\r\n"))
        self.assertTrue(is_ack(b"\r\n"))
        self.assertFalse(is_ack(b""))
        self.assertIn("NAK", parse_reply("lout", "=", b"!\n?\r\n")[0][1])
        self.assertIn("ACK", parse_reply("lout", "=", b"!\n\r\n")[0][1])
        self.assertEqual(parse_reply("curv", "?", b"!\n1,100,10,15,50,99,4.00\r\n")[1], ("YMIN (%)", "1"))
        self.assertEqual(parse_reply("lout", "?", b"!\n100\r\n")[1], ("Light output", "100 %"))
        self.assertEqual(parse_reply("temp", "?", b"!\n26.92\r\n")[1], ("Internal temperature", "26.92 \u00b0C"))
        info = dict(parse_reply("info", "?", b"!\n001,LSL-1000,12-049-05581,0A,1.4.1001,DW00,FLD,PV,CH1+2\r\n"))
        self.assertEqual(info["Address"], "001")
        self.assertEqual(info["Serial number"], "12-049-05581")
        self.assertTrue(info["Dimming control"].startswith("PV ="))
        self.assertTrue(info["Output channels"].startswith("CH1+2"))
        stat = dict(parse_reply("stat", "?", b"!\nRunTime\tPrCycle\tMaxTemp\tTemp1\tTemp2\tTemp3\tHumid\tFLAGS\r\n"
                                              b"32.6hr\t148\t103.5C\t26.9C\t25.0C\t24.5C\t54%\t0x0000\r\n"))
        self.assertEqual(stat["RunTime"], "32.6hr")
        self.assertEqual(stat["Temp3"], "24.5C")
        self.assertEqual(stat["FLAGS"], "0x0000")

    def test_prefix_only_is_not_an_answer(self):
        """Every light sends "!" + LF for every command, even one addressed
        to another node.  That alone must not count as a reply or an ACK."""
        from seasense_protocol import has_answer, is_ack
        self.assertFalse(has_answer(b"!\n"))
        self.assertFalse(is_ack(b"!\n"))
        self.assertTrue(has_answer(b"!\n001\r\n"))
        self.assertTrue(is_ack(b"!\n\r\n"))
        self.assertIn("NO REPLY", parse_reply("chsw", "?", b"!\n")[0][1])
        self.assertIn("no light has this address", parse_reply("chsw", "?", b"!\n")[0][1])

    def test_factory_replies(self):
        """FPRT / FTEN / FLMX replies, codes from DeepSea's service guides."""
        self.assertEqual(parse_reply("fprt", "?", b"!\nRS4,1\r\n")[1], ("Serial port", "RS4,1 = RS-485, 9600 baud"))
        self.assertEqual(parse_reply("fprt", "?", b"!\nRS2,3\r\n")[1], ("Serial port", "RS2,3 = RS-232, 57600 baud"))
        self.assertEqual(parse_reply("ften", "?", b"!\n1\r\n")[1], ("Termination resistor", "1 = On"))
        self.assertEqual(parse_reply("flmx", "?", b"!\n80\r\n")[1], ("Max output limit (FLMX)", "80 (1-100)"))
        info = dict(parse_reply("info", "?", b"!\n001,LSL-2000,12-049-12345,0A,1.4.1001,DW90,WFD,10V,CH1\r\n"))
        self.assertEqual(info["LED color"], "DW90 = Daylight White 90 CRI")
        self.assertEqual(info["Dimming control"], "10V = Analog 0-10 V")

    def test_curve_model(self):
        from seasense_protocol import curve_output
        # Linear curve from the spec: output follows input
        for x in (1, 25, 50, 99):
            self.assertAlmostEqual(curve_output(x, 1, 100, 1, 1, 100, 100, 1), 1 + 99 * (x - 1) / 99, places=6)
        # Default curve: off below A, YMIN at B, YMAX at D, rising in between
        default = (1, 100, 1, 5, 70, 95, 4.5)
        self.assertEqual(curve_output(0.5, *default), 0)
        self.assertEqual(curve_output(5, *default), 1)
        self.assertEqual(curve_output(95, *default), 100)
        values = [curve_output(x, *default) for x in range(5, 96)]
        self.assertEqual(values, sorted(values))
        # Close to the spec's Figure 2 (read off the graph by eye)
        for x, figure_value in ((40, 15), (70, 27), (80, 48), (90, 82)):
            self.assertAlmostEqual(curve_output(x, *default), figure_value, delta=5)

    def test_baud(self):
        self.assertEqual(parse_reply("baud", "?", b"3\r\n")[1], ("Baud rate", "3 = 57600 bps"))


if __name__ == "__main__":
    unittest.main()
