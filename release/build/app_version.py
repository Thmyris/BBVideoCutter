"""Prints APP_VERSION from BBVideoCutter.py, for the build scripts (the
installer's version)."""
import os
import re
import sys

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "BBVideoCutter.py")
with open(path, encoding="utf-8") as f:
    m = re.search(r'^APP_VERSION = "([^"]+)"', f.read(), re.M)
if not m:
    sys.exit("APP_VERSION not found in BBVideoCutter.py")
print(m.group(1))
