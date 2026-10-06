"""libv2_records.py is the schemas repository's record logic
(tools/libv2_records.py), vendored byte for byte: the packager and the
migration must write the same records for the same facts, because the
deletion gate is computed from them. A copy that drifts fails here. To
take a new one, copy the file from the schemas repository at the commit
that has it and set the values below."""

from __future__ import annotations

import hashlib
from pathlib import Path

from packager import libv2_records as rec

# zaentrum/schemas 92effa9e6103735cf175ddf1c239758c9aaeae06, tools/libv2_records.py
SHA256 = "3954f76ae4b8f709e1edc0524a13dde477b5a5a32aa4775a7d8922a33b6326e8"
LIBV2_RECORDS = 1


def test_the_record_logic_is_the_schemas_repositorys_byte_for_byte() -> None:
    assert hashlib.sha256(Path(rec.__file__).read_bytes()).hexdigest() == SHA256
    assert rec.LIBV2_RECORDS == LIBV2_RECORDS
