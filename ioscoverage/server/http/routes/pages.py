"""Public data-free HTML route projections."""

from http import HTTPStatus

from ioscoverage.web.pages.cycles import render_cycle_list_page
from ioscoverage.web.pages.login import render_login_page
from ioscoverage.web.pages.probe import render_probe_page
from ioscoverage.web.pages.users import render_user_admin_page


class PageRoutesMixin:
    """Delegates page projection to the web layer without exposing persistence."""

    def _handle_login_page(self) -> None:
        """Shows authentication only when no active browser Session exists."""

        principal = self._resolve_principal()
        if principal is not None and principal.kind == "web_user":
            self._send_temporary_redirect("/cycles/")
            return
        self._send_html(HTTPStatus.OK, render_login_page())

    def _handle_cycle_page(self) -> None:
        """Serves the Cycle shell only to an active browser Session."""

        principal = self._resolve_principal()
        if principal is None or principal.kind != "web_user":
            self._send_temporary_redirect("/login/")
            return
        self._send_html(HTTPStatus.OK, render_cycle_list_page())

    def _render_upload_page(self) -> bytes:
        """Builds the dependency-free Safari upload page from web-owned assets."""

        return render_probe_page(
            self._load_recent_probes(), self.config.max_file_bytes
        )

    def _handle_users_page(self) -> None:
        """Returns the data-free administrator shell without reading account storage."""

        self._send_html(HTTPStatus.OK, render_user_admin_page())
