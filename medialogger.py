#!/usr/bin/env python3
"""medialogger launcher. The UI import stays lazy so --selftest needs nothing but Python."""
import sys

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from medialogger_app.selftest import selftest
        selftest()
    else:
        from medialogger_app.ui import main
        main()
