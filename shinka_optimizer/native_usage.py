"""Account native completed calls and reserve budget for unresolved attempts."""
import json
from pathlib import Path


def totals(run):
    known, reserve, unknown = 0., 0., 0
    for p in Path(run).glob('investigations/*/usage.json'):
        try:
            u = json.loads(p.read_text())
        except (ValueError, OSError):
            continue
        if u.get('native_cost_usd') is not None:
            known += u['native_cost_usd']
        else:
            reserve += u.get('reserved_usd', .5)
            unknown += 1
    return dict(native_reported_cost=round(known, 6),
        unresolved_attempts=unknown, reserved_cost=round(reserve, 6),
        budget_accounted_cost=round(known+reserve, 6))
