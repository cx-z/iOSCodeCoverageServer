"""Static browser shell for authenticated Cycle listing and MR creation."""

from ioscoverage.web.assets import render_document


def render_cycle_list_page() -> bytes:
    """Returns a data-free same-origin shell that fetches Cycles with operator auth."""

    return render_document("cycles")
