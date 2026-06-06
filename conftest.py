"""Put the project root (flat main.py / tv.py modules) on sys.path for tests."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
