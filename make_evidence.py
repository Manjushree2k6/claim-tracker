"""Regenerate /evidence (run: python make_evidence.py). Each case runs on an isolated in-memory copy."""
import testcases

for tc in ("TC1", "TC2", "TC3", "TC4"):
    res = testcases.run(tc)
    print(tc, "PASS" if res.passed else "FAIL", testcases.export_evidence(res, "evidence"))
