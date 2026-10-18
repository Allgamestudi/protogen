#!/usr/bin/env python3
"""Protogen controller - entry point.

Starts the matrix renderer thread and the serial reader thread,
then runs the Flask web app in the foreground.
"""
import time
import sys

import serial_proto
import matrix_renderer
import remote
import ears_effect
import hostname_guard
import hotspot
import web_app
from state import stop_event, flush_all


def main():
    hostname_guard.start()          # keeps the address http://proto.local
    hotspot.start()                 # own Wi-Fi when no known network is around
    serial_proto.start()
    remote.start()
    ears_effect.start()
    matrix_renderer.start()
    try:
        web_app.run()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        stop_event.set()
        flush_all()
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
