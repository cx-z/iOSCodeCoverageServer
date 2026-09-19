"""Static browser shell for administrator account maintenance."""

from ioscoverage.web.assets import render_document


def render_user_admin_page() -> bytes:
    """Returns a data-free same-origin shell that requires an admin Session API."""

    return render_document("users")
