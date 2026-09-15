"""rtl_heuristics: deterministic (no-LLM) RTL timing-closure analysis pipeline.

Pipeline: Yosys word-level elaboration -> bit-level delay model -> clock/CDC
inference -> structural pattern matching -> heuristic detectors -> ranked
issues + fixes (JSON) -> optional correlation with OpenSTA measurements.
"""
__version__ = "0.1.0"
