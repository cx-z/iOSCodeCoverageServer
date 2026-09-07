#!/usr/bin/env python3
"""Compatibility entry point for single-Build LLVM analysis."""

from ioscoverage.cli.analyze_single_build import main


if __name__ == "__main__":
    raise SystemExit(main())
