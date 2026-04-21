#!/usr/bin/env python3
"""快捷入口"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.app.cli import main
main()
