"""Opt-in offline probes for two documented trace formats; no live capture."""
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest


def latency_trace():
    # Documented trace-cmd v6 latency variant. This contains no host data.
    header = (b"\tfield: u64 timestamp;\toffset:0;\tsize:8;\tsigned:0;\n"
              b"\tfield: local_t commit;\toffset:8;\tsize:8;\tsigned:1;\n"
              b"\tfield: int overwrite;\toffset:8;\tsize:1;\tsigned:1;\n"
              b"\tfield: char data;\toffset:16;\tsize:4080;\tsigned:0;\n")
    pack = struct.pack
    return (b"\x17\x08\x44tracing6\x00\x00\x08" + pack("<I", 4096) +
            b"header_page\0" + pack("<Q", len(header)) + header +
            b"header_event\0" + pack("<Q", 0) + pack("<IIIIQI", 0, 0, 0, 0, 0, 1) +
            b"latency  \0# tracer: synthetic\n"
            b"       fixture-42 [000] .... 1.000001: pennix_fixture_marker: value=17\n")


class OfflineTraceTests(unittest.TestCase):
    def run_report(self, command, name, content):
        with tempfile.TemporaryDirectory(prefix="pennix-offline-report-") as workspace:
            fixture = Path(workspace) / name
            fixture.write_bytes(content)
            result = subprocess.run([*command, "-i", str(fixture)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    @unittest.skipUnless(shutil.which("trace-cmd"), "requires trace-cmd")
    def test_trace_cmd_reads_synthetic_latency_trace(self):
        output = self.run_report(["trace-cmd", "report"], "trace.dat", latency_trace())
        self.assertIn("pennix_fixture_marker: value=17", output)

    @unittest.skipUnless(shutil.which("perf") and os.environ.get("PENNIX_TEST_PERF_FIXTURE"),
                         "requires perf and an explicitly supplied public fixture")
    def test_perf_reads_pinned_public_sample_events(self):
        # AOSP platform/system/extras@2b5a85e79458262c765b25fea40d086e7922d8d5
        # simpleperf/testdata/perf_with_symbols.data; run requires no network.
        with Path(os.environ["PENNIX_TEST_PERF_FIXTURE"]).open("rb") as stream:
            content = stream.read(64 * 1024)
        self.assertEqual(hashlib.sha256(content).hexdigest(),
                         "ec9130276cc88f6c59505fbae8ef21beb93dd78df2f1210928eae7c54ce1b377")
        output = self.run_report(["perf", "report", "--stats"], "perf.data", content)
        self.assertRegex(output, r"SAMPLE events:\s+935\b")
