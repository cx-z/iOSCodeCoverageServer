"""Public data-free HTML route projections."""

from ioscoverage.web.pages.probe import render_probe_page


class PageRoutesMixin:
    """Delegates page projection to the web layer without exposing persistence."""

    def _render_upload_page(self) -> bytes:
        """Builds the dependency-free Safari upload page from web-owned assets."""

        return render_probe_page(
            self._load_recent_probes(), self.config.max_file_bytes
        )
