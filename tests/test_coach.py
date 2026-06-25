"""Tests for the active coach — deterministic surface→test mapping, no model, no hallucination."""

from friday.core import bounty, coach


def test_suggest_tests_maps_api_to_access_control():
    s = coach.suggest_tests([{"matched_at": "http://x.com/api/v1/users"}])
    assert any(x["cls"] == "Access control" for x in s)              # /api → IDOR/BFLA


def test_suggest_tests_graphql_and_ssrf_params():
    s = coach.suggest_tests([], ["http://x.com/graphql", "http://x.com/p?url=http://a"])
    classes = {x["cls"] for x in s}
    assert "GraphQL" in classes and "SSRF / open redirect" in classes


def test_suggest_tests_dedupes_same_surface_class():
    s = coach.suggest_tests([], ["http://x.com/api"] * 5)
    hits = [x for x in s if x["surface"] == "http://x.com/api" and x["cls"] == "Access control"]
    assert len(hits) == 1                                            # one suggestion, not five


def test_suggest_tests_respects_cap():
    many = [f"http://x.com/api/{i}?url=y&admin=1" for i in range(20)]
    assert len(coach.suggest_tests([], many, cap=5)) == 5


def test_render_plan_has_always_section():
    p = coach.render_plan(coach.suggest_tests([], ["http://x.com/admin"]))
    assert "Manual tests to try next" in p and "On every live host" in p


def test_report_appends_coach_plan():
    f = [{"id": "x", "name": "X", "severity": "medium", "matched_at": "http://x.com/api/orders/1"}]
    r = bounty.draft_bounty_report("x.com", f, surfaces=["http://x.com"])
    assert "Manual tests to try next" in r and "Access control" in r


def test_report_coaches_even_with_zero_findings():
    r = bounty.draft_bounty_report("x.com", [], surfaces=["http://x.com/admin"])
    assert "No findings to report" in r and "Manual tests to try next" in r
