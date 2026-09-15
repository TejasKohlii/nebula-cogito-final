"""ASAP dataflow timing, relative to source Q/port availability (ns).

This is an abstract scheduling model, not signoff STA: sampled node delays
can come from different paths. Missing or merged delays are never added.
Sequential elements cut dependencies; their input-ready and output times are
independent. Clock phase, setup, and asynchronous control checks remain STA's job.
"""
from collections import defaultdict, deque
import math


def annotate_schedule(graph, source_arrivals=None):
    """Attach `asap` dictionaries without changing existing timing or labels.

    Optional source_arrivals maps source ids to Q/input availability in ns;
    unspecified sources default to zero. These must share a time reference.
    """
    source_arrivals = source_arrivals or {}
    for nid, value in source_arrivals.items():
        if nid not in graph.nodes or not graph.nodes[nid].is_source:
            raise ValueError(f"Not a launch source: {nid}")
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Invalid source arrival: {nid}")
    incoming, outgoing = defaultdict(list), defaultdict(list)
    comb = {nid for nid, n in graph.nodes.items() if n.kind == "comb"}
    pending = dict.fromkeys(comb, 0)
    for edge in graph.edges.values():
        edge.attrs.pop("asap", None)
        # Exclude edges carrying only asynchronous reset/set controls.
        if edge.width and edge.async_width >= edge.width:
            continue
        incoming[edge.dst].append(edge)
        if edge.src in comb and edge.dst in comb:
            outgoing[edge.src].append(edge.dst)
            pending[edge.dst] += 1
    outputs = {}
    for nid, node in graph.nodes.items():
        node.attrs["asap"] = {"input_ready": None, "output_arrival": None,
                              "level": None, "limiting_predecessors": [], "status": "unknown"}
        if node.is_source:
            outputs[nid] = source_arrivals.get(nid, 0.0)
            node.attrs["asap"].update(output_arrival=outputs[nid], level=0, status="source")

    def evaluate(nid):
        node = graph.nodes[nid]
        schedule = node.attrs["asap"]
        edges = incoming[nid]
        values = [outputs.get(e.src) for e in edges]
        complete = all(v is not None for v in values)
        ready = max(values, default=0.0) if complete else None
        levels = [graph.nodes[e.src].attrs["asap"]["level"] for e in edges]
        level = (max(levels, default=0) + 1) if all(v is not None for v in levels) else None
        schedule.update(input_ready=ready, status="ready" if complete else "unknown_input")
        if not node.is_source:
            schedule["level"] = level
        schedule["limiting_predecessors"] = sorted(e.src for e in edges
            if ready is not None and math.isclose(outputs[e.src], ready, abs_tol=1e-9))
        for edge in edges:
            value = outputs.get(edge.src)
            edge.attrs["asap"] = {"arrival": value,
                "wait": ready - value if ready is not None and value is not None else None}
        if node.kind == "comb":
            delay = node.attrs.get("delay")
            valid = isinstance(delay, (int, float)) and math.isfinite(delay) and delay >= 0
            if node.attrs.get("combinational_loop"):
                schedule["status"] = "combinational_loop"
            elif not valid or node.attrs.get("delay_exact") is False:
                schedule["status"] = "unknown_delay"
            elif ready is not None:
                outputs[nid] = ready + delay
                schedule["output_arrival"] = outputs[nid]
            schedule["level"] = level

    queue = deque(sorted(nid for nid, degree in pending.items() if degree == 0))
    visited = set()
    while queue:
        nid = queue.popleft()
        visited.add(nid)
        evaluate(nid)
        for dst in outgoing[nid]:
            pending[dst] -= 1
            if pending[dst] == 0:
                queue.append(dst)
    for nid in comb - visited:
        graph.nodes[nid].attrs["asap"]["status"] = "cycle_or_downstream"
    for nid, node in graph.nodes.items():
        if node.is_sink:
            evaluate(nid)
    graph.stats["asap_timed_comb_nodes"] = sum(nid in outputs for nid in comb)
    return {"model": "max_plus", "units": "ns", "source_reference": "Q/port availability",
            "default_source_arrival": 0.0, "source_arrivals": dict(source_arrivals),
            "delay_basis": "sampled STA node delays; not a signoff timing bound",
            "unknown_policy": "propagate; merged and negative delays excluded",
            "cycle_or_downstream_nodes": sorted(comb - visited)}
