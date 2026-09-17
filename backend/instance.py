"""Per-instance identity for Ada backends.

ADA_INSTANCE_ID pins the MDDB memory collections (snapshots, device
confidence/safety, recorded events) to a stable name instead of deriving
them from HOME_ASSISTANT_URL, so memory survives URL, scheme, and host
changes.

Fail fast when it is missing or invalid: a misconfigured service must
never silently write to the wrong collections.
"""

from __future__ import annotations

import os
import re

_INSTANCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def ada_instance_id() -> str:
    instance = os.environ.get("ADA_INSTANCE_ID", "").strip()
    if not instance:
        raise RuntimeError(
            "ADA_INSTANCE_ID is not set; add it to the service env file "
            "(e.g. ADA_INSTANCE_ID=tony). Refusing to start without a stable "
            "memory-collection identity."
        )
    if not _INSTANCE_ID_RE.match(instance):
        raise RuntimeError(
            f"ADA_INSTANCE_ID={instance!r} is invalid; expected {_INSTANCE_ID_RE.pattern}"
        )
    return instance
