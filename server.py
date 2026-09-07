#!/usr/bin/env python3
"""Compatibility entry point for the packaged iOS Coverage Server."""

from ioscoverage.server.bootstrap import ProbeConfig, ProbeHTTPServer, create_server, main


__all__ = ["ProbeConfig", "ProbeHTTPServer", "create_server", "main"]


if __name__ == "__main__":
    main()
