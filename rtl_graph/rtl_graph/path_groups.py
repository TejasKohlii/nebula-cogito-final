"""Compress repeated bit-level paths without losing endpoint accounting."""


def compress_paths(graph, meta):
    groups = {}
    paths = sorted(meta.get("critical_paths", []), key=lambda p: (p["slack"], p["index"]))
    for path in paths:
        nodes = tuple(path.get("nodes") or [])
        # Unmapped ends cannot establish that two paths follow the same route.
        identity = nodes if nodes and path.get("start_node") and path.get("end_node") else (
            nodes, path.get("startpoint"), path.get("endpoint"))
        key = (identity, path.get("launch_clock"), path.get("capture_clock"),
               path.get("path_group"), path.get("check"), bool(path.get("nodes_exact")), path["slack"] < 0)
        if key not in groups:
            groups[key] = {**path, "index": len(groups), "instances": 0, "members": []}
        group = groups[key]
        group["instances"] += path.get("instances", 1)
        group["members"].extend(path.get("members") or [{
            "startpoint": path.get("startpoint"), "endpoint": path.get("endpoint"),
            "slack": path.get("slack")}])
    meta["critical_paths"] = list(groups.values())
    meta["node_paths"] = {}
    endpoint_paths = {}
    for node in graph.nodes.values():
        node.attrs.update(critical_path=0, instances=0)
    for path in meta["critical_paths"]:
        for member in path["members"]:
            endpoint_paths[member["endpoint"]] = path["index"] + 1
        for nid in dict.fromkeys(path.get("nodes") or []):
            meta["node_paths"].setdefault(nid, []).append(path["index"])
            node = graph.nodes.get(nid)
            if node is not None and not node.attrs["critical_path"]:
                node.attrs.update(critical_path=path["index"] + 1, instances=path["instances"])
    for endpoint in meta.get("timing_endpoints", []):
        endpoint["critical_path"] = endpoint_paths.get(endpoint["endpoint"], 0)
    graph.stats["critical_path_groups"] = len(groups)
    graph.stats["critical_path_instances"] = sum(p["instances"] for p in groups.values())
    if "timing_summary" in meta:
        meta["timing_summary"].update(
            critical_path_groups=len(groups), critical_path_instances=graph.stats["critical_path_instances"])
