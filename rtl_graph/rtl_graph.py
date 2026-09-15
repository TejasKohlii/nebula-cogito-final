#!/usr/bin/env python3
"""Entry point: python3 rtl_graph.py <subcommand> ..."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rtl_graph.cli import main
main()
