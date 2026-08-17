"""§4 forensic severities and the §8 verification score.

The behaviour that matters most here is the one the master prompt is emphatic
about: a check that could not run must never be scored as a pass, and a failed
check must cap the headline confidence.
"""

import pytest

from equity_analyst.analysis.forensics import (
    Check,
    ForensicResult,
    _cfo_ebitda_check,
    _cumulative_cash_check,
    _score,
)


def _quality(pairs):
    return [
        {"period_label": f"Y{i}", "yr_idx": i, "pat": pat, "cfo": cfo, "ebitda": ebitda,
         "cfo_to_ebitda": (cfo / ebitda) if ebitda else None,
         "cfo_to_pat": (cfo / pat) if pat else None}
        for i, (pat, cfo, ebitda) in enumerate(pairs, start=1)
    ]


def test_cash_backed_profits_pass():
    rows = _quality([(100, 130, 160)] * 10)
    assert _cumulative_cash_check(rows).status == "pass"


def test_profits_not_arriving_as_cash_fail():
    rows = _quality([(100, 40, 160)] * 10)
    check = _cumulative_cash_check(rows)
    assert check.status == "fail"
    assert "not arriving as cash" in check.detail


def test_missing_cash_flow_is_skipped_not_passed():
    check = _cumulative_cash_check([])
    assert check.status == "skipped"
    assert check.status != "pass"


def test_cfo_to_ebitda_floor():
    good = _cfo_ebitda_check(_quality([(100, 130, 160)] * 8), 0.70)
    bad = _cfo_ebitda_check(_quality([(100, 60, 160)] * 8), 0.70)
    assert good.status == "pass"
    assert bad.status == "fail"


# -- scoring ---------------------------------------------------------------


def _result(checks):
    res = ForensicResult(checks=checks)
    _score(res)
    return res


def test_skipped_checks_are_excluded_from_the_denominator():
    """Four passes plus four skips must not read as 50%."""
    checks = [Check(f"p{i}", f"P{i}", "pass", "") for i in range(4)]
    checks += [Check(f"s{i}", f"S{i}", "skipped", "") for i in range(4)]
    res = _result(checks)
    assert res.score == pytest.approx(100.0)
    assert len(res.skipped) == 4


def test_too_few_checks_yields_unknown_not_pass():
    res = _result([Check("a", "A", "pass", ""), Check("b", "B", "pass", "")])
    assert res.flag == "unknown"


def test_any_failure_sets_the_fail_flag():
    checks = [Check(f"p{i}", f"P{i}", "pass", "") for i in range(8)]
    checks.append(Check("x", "X", "fail", ""))
    assert _result(checks).flag == "fail"


def test_several_watches_downgrade_to_watch():
    checks = [Check(f"p{i}", f"P{i}", "pass", "") for i in range(5)]
    checks += [Check(f"w{i}", f"W{i}", "watch", "") for i in range(3)]
    assert _result(checks).flag == "watch"


def test_weights_matter():
    heavy_fail = _result(
        [Check("h", "H", "fail", "", weight=3.0)]
        + [Check(f"p{i}", f"P{i}", "pass", "") for i in range(4)]
    )
    light_fail = _result(
        [Check("l", "L", "fail", "", weight=0.5)]
        + [Check(f"p{i}", f"P{i}", "pass", "") for i in range(4)]
    )
    assert heavy_fail.score < light_fail.score


# -- verification ----------------------------------------------------------


def test_verification_reports_every_component(report):
    ver = report.verification
    keys = {s.key for s in ver.subscores}
    assert keys == {"completeness", "source", "model_agreement", "forensic", "recency"}
    assert 0 <= ver.total <= 100


def test_weights_sum_to_one():
    from equity_analyst.verification import WEIGHTS

    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_contributions_reconcile_to_the_total(report):
    ver = report.verification
    raw = sum(s.contribution for s in ver.subscores) * 100.0
    if ver.caps_applied:
        assert ver.total <= raw + 1e-6
    else:
        assert ver.total == pytest.approx(raw, abs=0.05)


def test_failed_forensics_caps_the_headline(report):
    """The sample company fails CFO/EBITDA, so the cap must be visible."""
    if report.forensic.flag != "fail":
        pytest.skip("sample bundle no longer trips a forensic failure")
    assert report.verification.total <= 60.0
    assert any("Capped at 60%" in c for c in report.verification.caps_applied)


def test_every_output_carries_a_verification_number(report):
    from equity_analyst.report import render

    html = render(report)
    assert "Verification score" in html
    assert f"{report.verification.total:.1f}%" in html
