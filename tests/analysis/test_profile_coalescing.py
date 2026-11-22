"""LLVM evidence gate for same-launch cumulative snapshot representative selection."""

import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class ProfileCoalescingTests(unittest.TestCase):
    """Compares real region/segment hits from all samples against last-per-launch input."""

    def test_real_cumulative_profiles_preserve_region_and_line_hits(self):
        """Uses one instrumented binary, multiple threads and two independent launches."""
        environment = dict(os.environ, DEVELOPER_DIR="/Applications/Xcode263.app/Contents/Developer")
        source = Path(__file__).resolve().parents[1] / "fixtures/profile_coalescing/main.c"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "collector-fixture"
            environment["LLVM_PROFILE_FILE"] = str(root / "exit-%p.profraw")

            def run(*args):
                """Runs only fixed local fixture tools under the mandated Xcode."""
                return subprocess.run(args, env=environment, check=True, capture_output=True, timeout=60).stdout

            run("xcrun", "clang", "-fprofile-instr-generate", "-fcoverage-mapping", "-pthread", str(source), "-o", str(binary))
            for launch in range(2):
                run(str(binary), directory, str(launch))
            all_profiles = sorted(root.glob("[01]-*.profraw"))
            representatives = [root / "0-4.profraw", root / "1-4.profraw"]
            self.assertEqual(len(all_profiles), 10)
            exports = []
            for name, profiles in [("all", all_profiles), ("representatives", representatives)]:
                merged = root / (name + ".profdata")
                run("xcrun", "llvm-profdata", "merge", "-sparse", *map(str, profiles), "-o", str(merged))
                exports.append(json.loads(run("xcrun", "llvm-cov", "export", str(binary), "-instr-profile=" + str(merged))))
            self.assertEqual(self.hit_evidence(exports[0]), self.hit_evidence(exports[1]))
            # This measures a real raw sample, not an indexed .profdata surrogate.
            raw = representatives[-1].read_bytes()
            self.assertEqual(gzip.decompress(gzip.compress(raw, mtime=0)), raw)
            self.assertLess(len(gzip.compress(raw, mtime=0)), len(raw))

    def hit_evidence(self, export):
        """Normalizes counts to hit booleans without hiding region or segment geometry."""
        data = export["data"][0]
        regions = []
        for function in data["functions"]:
            for region in function["regions"]:
                fields = list(region)
                fields[4] = fields[4] > 0
                regions.append((function["name"], tuple(function["filenames"]), tuple(fields)))
        files = []
        for file in data["files"]:
            segments = []
            for segment in file["segments"]:
                fields = list(segment)
                fields[2] = fields[2] > 0
                segments.append(tuple(fields))
            files.append((file["filename"], segments))
        return regions, files
