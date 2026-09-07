"""Projection for the public Safari upload probe page."""

import html
from typing import Mapping, Sequence

from ioscoverage.web.assets import render_document


def render_probe_page(
    recent_probes: Sequence[Mapping[str, object]], max_file_bytes: int
) -> bytes:
    """Escapes recent probe metadata and injects it into the trusted page shell."""

    rows = []
    for probe in recent_probes:
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td><code>{}</code></td></tr>".format(
                html.escape(str(probe.get("received_at", ""))),
                html.escape(str(probe.get("source_ip", ""))),
                html.escape(str(probe.get("original_filename", ""))),
                html.escape(str(probe.get("size", ""))),
                html.escape(str(probe.get("sha256", ""))),
            )
        )
    recent_rows = "".join(rows) or '<tr><td colspan="5">暂无成功上传记录</td></tr>'
    maximum_mebibytes = "{:g}".format(max_file_bytes / (1024 * 1024))
    return render_document(
        "probe",
        {
            "__MAX_MEBIBYTES__": maximum_mebibytes,
            "__RECENT_ROWS__": recent_rows,
        },
    )
