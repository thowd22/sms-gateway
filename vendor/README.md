# Vendored: python-messaging (MMS encoder)

`messaging/` is a subset of [pmarti/python-messaging](https://github.com/pmarti/python-messaging)
by Francois Aucamp and Pablo Marti, **licensed GPL-2** (see `messaging/COPYING`).

Only the MMS encoder is included; the SMS package and tests are removed.

## Why vendored rather than installed

It is not on PyPI under a usable name — the `messaging` package there is an
unrelated message-queue library — and the upstream encoder is Python 2 only
despite the README claiming 3.2 support. Reimplementing it was the alternative,
and the WSP well-known-value tables are precisely the part you cannot retype
from memory: a wrong content-type byte yields a PDU the MMSC rejects with no
useful diagnostic.

## Modifications (GPL-2 section 2a)

Changed by this project in August 2026, to run on Python 3.13:

- `except X, e:` -> `except X as e:` throughout
- `print` statements -> `print()` calls
- `xrange` -> `range`, `basestring` -> `str`
- `array.tostring()` -> `array.tobytes()`
- `obj.next()` -> `next(obj)`, plus a `__next__` alias on `PreviewIter`
  (py2 named the iterator method `next`; py3 requires `__next__`)
- `message.py`: part data files are opened `'rb'` rather than `'r'`. Reading a
  JPEG as text raised `UnicodeDecodeError` on the first byte.
- `mms_pdu.py` / `wsp_pdu.py`: added a `_byte()` helper so `ord()` call sites
  accept both py2 1-character strings and py3 ints, since iterating `bytes`
  now yields ints.

No functional or protocol changes were made. Verified by encoding a message and
decoding it back with the library's own decoder, then confirming a real carrier
MMSC accepted the resulting PDU.
