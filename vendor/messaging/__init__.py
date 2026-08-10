"""Vendored subset of pmarti/python-messaging - MMS encoder only.

Ported from Python 2 by mechanical fixes (except-as, range, str, tobytes).
Kept because its WSP/MMS well-known-value tables are the part that must be
exactly right; retyping those from memory is how you get a PDU the MMSC
silently rejects.
"""
