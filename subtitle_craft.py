#!/usr/bin/env python3
"""
subtitle_craft.py - Root CLI entrypoint for Subtitle Craft Agent Skill.
Complies with the Agent Plugins 1.0 Specification.
"""

import sys
from pathlib import Path

# Ensure package root is in sys.path
root_dir = Path(__file__).parent.resolve()
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from scripts.generate_subtitles import main

if __name__ == "__main__":
    main()
