"""Issue data model shared by detectors and the reporter."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict

SEVERITY_WEIGHT = {"critical": 5.0, "high": 3.0, "medium": 1.5, "low": 0.7, "info": 0.2}


@dataclass
class Fix:
    name: str
    algorithm: str                      # key into ALGORITHMS.md
    description: str
    steps: list = field(default_factory=list)
    rtl_before: str = ""
    rtl_after: str = ""
    sdc_change: str = ""
    expected_gain_ns: float | None = None
    expected_gain_note: str = ""
    latency_change: str = "none"        # 'none' | '+1 cycle' | ...
    equivalence: str = "combinational"  # what kind of equivalence proof is needed
    risk: str = "low"
    verification: list = field(default_factory=list)
    ppa: str = ""                       # expected area/power side effect


@dataclass
class Issue:
    id: str
    category: str
    severity: str
    title: str
    module: str
    signal: str = ""
    src: str = ""
    instances: int = 1
    instance_examples: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    fixes: list = field(default_factory=list)     # list[Fix], best first
    references: list = field(default_factory=list)
    measured: dict = field(default_factory=dict)  # filled by sta_link
    score: float = 0.0
    rank: int = 0
    uid: str = ""

    def to_dict(self):
        d = asdict(self)
        return d

    def compute_score(self):
        base = SEVERITY_WEIGHT.get(self.severity, 0.5)
        mult = 1.0 + 0.15 * min(self.instances, 40)
        ev = self.evidence
        neg = 0.0
        for k in ("est_slack_ns", "worst_est_slack_ns"):
            v = ev.get(k)
            if isinstance(v, (int, float)) and v < 0:
                neg = max(neg, -v)
        ms = self.measured.get("worst_slack_ns")
        if isinstance(ms, (int, float)) and ms < 0:
            neg = max(neg, -ms * 1.5)
        hn = self.measured.get("hot_net")
        if hn and hn.get("delay_ns", 0) > 0.2:
            neg = max(neg, hn["delay_ns"] * 1.5)
        self.score = round(base * mult * (1.0 + neg), 3)
        return self.score
