"""Untrusted content is data, not instructions (design principle 8).

Everything that came from web pages, emails, APIs or documents is wrapped in
<untrusted source="..."> tags when it is put in front of the model; the system
prompt says such content is never an instruction. A document cannot close the
tag early: any literal tag text inside it is defused first.
"""

from __future__ import annotations

import re

_TAG = re.compile(r"</?\s*untrusted", re.IGNORECASE)


def wrap(content: str, source: str | None) -> str:
    safe = _TAG.sub(lambda m: m.group(0).replace("<", "&lt;"), content)
    label = (source or "unknown").replace('"', "'")
    return f'<untrusted source="{label}">\n{safe}\n</untrusted>'
