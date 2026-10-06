"""The iBOS vocabulary: its namespace and the document shipped in this package.
See ``README.md``, "The iBOS vocabulary"."""

from __future__ import annotations

from bricklogger.sdk.declaration import Vocabulary

NAMESPACE = "https://brick.cx2.dk/schema/ibos#"
"""The namespace; the document is published at the URL without the fragment."""

VOCABULARY = Vocabulary(
    prefix="ibos",
    namespace=NAMESPACE,
    package="bricklogger_ibos",
    resource="ibos.ttl",
)
