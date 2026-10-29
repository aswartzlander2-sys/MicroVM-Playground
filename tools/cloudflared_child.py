#!/usr/bin/env python3
"""Linux-only cloudflared child that dies if its supervising dashboard dies."""
import ctypes, os, signal, sys
parent=os.getppid()
if ctypes.CDLL(None,use_errno=True).prctl(1, signal.SIGTERM) != 0: sys.exit(1)
if os.getppid()!=parent: sys.exit(1)
os.execv(sys.argv[1],[sys.argv[1],'tunnel','--no-autoupdate','run'])
