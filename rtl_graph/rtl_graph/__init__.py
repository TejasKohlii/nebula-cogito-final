"""rtl_graph -- abstract register/combinational/port graph of an RTL design,
annotated with OpenSTA timing (delays, slacks, critical/CDC/FSM/violating labels).

Pipeline:  Yosys (elaborate+flatten -> JSON)  ->  netlist.Design
           -> abstract.AbstractGraph (ports / registers / comb nodes)
           -> clocks (domain tracing, CDC)
           -> sta (query OpenSTA or parse a report) -> annotate
           -> export (JSON / DOT / GraphML / SVG)
"""
__version__ = "0.1.0"
