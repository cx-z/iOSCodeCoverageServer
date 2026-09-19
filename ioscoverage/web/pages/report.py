"""Dependency-free browser shell for one authenticated local Cycle report."""

import json
import uuid

from ioscoverage.web.assets import render_document


def render_coverage_report_page(cycle_id: str) -> bytes:
    """Renders a data-free shell whose JSON requests use anonymous read or Web Session."""

    normalized = str(uuid.UUID(cycle_id))
    if normalized != cycle_id:
        raise ValueError("cycle_id must be canonical")
    return render_document(
        "report", {"__CYCLE_ID_JSON__": json.dumps(cycle_id)}
    )
