#!/usr/bin/env python3
"""Compatibility entry point for safe runtime-data cleanup."""

from ioscoverage.cli.cleanup_runtime_data import main


if __name__ == "__main__":
    raise SystemExit(main())
