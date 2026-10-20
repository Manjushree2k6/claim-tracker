"""Shared helpers. Tests are plain functions so they run with pytest OR run_tests.py."""
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service import ClaimService  # noqa: E402


def svc() -> ClaimService:
    return ClaimService.fresh()


@contextmanager
def raises(exc_type):
    try:
        yield
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__} to be raised")


def propose_tc1(s: ClaimService):
    st = s.get_claim_status("CLM-1001", "POL-5001", "Priya Kumar", True)
    assert st.state.value == "OK"
    return s.propose_upload("CLM-1001", "final_invoice_CLM-1001.pdf")
