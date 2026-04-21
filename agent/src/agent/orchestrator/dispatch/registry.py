"""Collector selection registry.

Maps hypothesis label prefixes to the right Go collector service name.
Selection rules (from the arch doc):

    metric.*  → prom
    log.*     → loki
    pod.*     → kube
    event.*   → kube
    <default> → prom
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COLLECTOR_PROM = "prom"
COLLECTOR_LOKI = "loki"
COLLECTOR_KUBE = "kube"

_PREFIX_MAP: dict[str, str] = {
    "metric.": COLLECTOR_PROM,
    "log.": COLLECTOR_LOKI,
    "pod.": COLLECTOR_KUBE,
    "event.": COLLECTOR_KUBE,
}

_DEFAULT_COLLECTOR = COLLECTOR_PROM

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def select_collector(hypothesis_text: str) -> str:
    """Return the collector name that best matches *hypothesis_text*.

    Iterates through the prefix map in definition order and returns on the
    first match. Falls back to ``prom`` when no prefix matches.

    Parameters
    ----------
    hypothesis_text:
        The ``Hypothesis.text`` field from the current focus hypothesis.

    Returns
    -------
    str
        One of ``"prom"``, ``"loki"``, or ``"kube"``.
    """
    lower = hypothesis_text.lower()
    for prefix, collector in _PREFIX_MAP.items():
        if lower.startswith(prefix):
            return collector
    return _DEFAULT_COLLECTOR
