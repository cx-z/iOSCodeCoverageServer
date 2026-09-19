"""Static browser shell for platform account login and registration."""

from ioscoverage.web.assets import render_document


def render_login_page() -> bytes:
    """Returns the data-free authentication page without reading account state."""

    return render_document("login")
