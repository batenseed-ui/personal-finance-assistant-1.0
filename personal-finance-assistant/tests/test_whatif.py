"""Tests for the what-if simulator (POST /api/whatif).

Expected numbers are worked out here with plain arithmetic, independently of scoring.py.
Base profile (SAMPLE): age 34, retire 62, income 5500, expenses 3200, debt payments 600,
investing 400/month, cash 9000, investments 42000, debt 38000, health insurance only.
"""

import unittest

import scoring
from tests.test_app import SAMPLE, BaseCase, post


def future_value(current, monthly, years, annual=0.04):
    i = annual / 12
    n = int(round(years * 12))
    g = (1 + i) ** n
    return current * g + monthly * (g - 1) / i


def whatif(client, adjustments=None, profile=SAMPLE):
    body = {"profile": profile}
    if adjustments is not None:
        body["adjustments"] = adjustments
    return post(client, "/api/whatif", body)


def comp(result, key):
    return next(c for c in result["components"] if c["key"] == key)


class WhatIfResultTests(BaseCase):
    def test_no_adjustments_means_no_change(self):
        for adj in (None, {}, {"expense_cut": 0, "extra_investment": "", "extra_cash": None}):
            r = whatif(self.client, adj).get_json()
            self.assertTrue(r["ok"])
            self.assertEqual(r["delta"], 0)
            self.assertEqual(r["base_score"], r["scenario_score"])
            self.assertAlmostEqual(r["base_score"], 60.9, delta=0.4)
            self.assertTrue(all(c["change"] == 0 for c in r["components"]))
            self.assertEqual(r["warnings"], [])

    def test_expense_cut_hand_calculated(self):
        r = whatif(self.client, {"expense_cut": 500}).get_json()
        outgoings = 2700 + 600
        emergency = min(9000 / outgoings / 6, 1) * 20  # 9.09
        need = 2700 * 12 * 0.8 / 0.04  # 648,000
        retirement = min(future_value(42000, 400, 28) / need, 1) * 15
        self.assertAlmostEqual(comp(r, "emergency_fund")["after"], emergency, delta=0.1)
        self.assertAlmostEqual(comp(r, "retirement")["after"], retirement, delta=0.1)
        self.assertEqual(comp(r, "savings_rate")["after"], 20.0)       # already capped
        self.assertEqual(comp(r, "debt_burden")["change"], 0)          # debt payments untouched
        self.assertEqual(comp(r, "protection")["change"], 0)
        base_total = 20 + 9000 / 3800 / 6 * 20 + comp(r, "debt_burden")["before"] + comp(r, "net_worth")["before"] \
            + min(future_value(42000, 400, 28) / (3200 * 12 * 0.8 / 0.04), 1) * 15 + 5
        self.assertAlmostEqual(r["base_score"], base_total, delta=0.4)
        self.assertGreater(r["delta"], 2)
        self.assertAlmostEqual(r["scenario_score"] - r["base_score"], r["delta"], delta=0.11)

    def test_delaying_retirement_only_changes_retirement_points(self):
        r = whatif(self.client, {"delay_retirement_years": 3}).get_json()
        need = 3200 * 12 * 0.8 / 0.04  # 768,000
        expected = min(future_value(42000, 400, 31) / need, 1) * 15
        self.assertAlmostEqual(comp(r, "retirement")["after"], expected, delta=0.1)
        self.assertGreater(comp(r, "retirement")["change"], 0)
        for key in ("savings_rate", "emergency_fund", "debt_burden", "net_worth", "protection"):
            self.assertEqual(comp(r, key)["change"], 0, key)

    def test_extra_cash_raises_emergency_fund_and_net_worth(self):
        r = whatif(self.client, {"extra_cash": 5000}).get_json()
        self.assertAlmostEqual(comp(r, "emergency_fund")["after"], 14000 / 3800 / 6 * 20, delta=0.1)
        self.assertAlmostEqual(comp(r, "net_worth")["after"], 18000 / (2.25 * 66000) * 15, delta=0.1)
        self.assertEqual(comp(r, "retirement")["change"], 0)

    def test_extra_investing_only_changes_retirement(self):
        r = whatif(self.client, {"extra_investment": 300}).get_json()
        need = 3200 * 12 * 0.8 / 0.04
        self.assertAlmostEqual(comp(r, "retirement")["after"], min(future_value(42000, 700, 28) / need, 1) * 15, delta=0.1)
        self.assertEqual(comp(r, "savings_rate")["change"], 0)

    def test_unaffordable_investing_warns(self):
        r = whatif(self.client, {"extra_investment": 2000}).get_json()  # surplus is only 1700
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("surplus", r["warnings"][0])
        self.assertEqual(whatif(self.client, {"extra_investment": 100}).get_json()["warnings"], [])

    def test_combined_levers_beat_single_levers(self):
        both = whatif(self.client, {"expense_cut": 400, "delay_retirement_years": 2}).get_json()["delta"]
        one = whatif(self.client, {"expense_cut": 400}).get_json()["delta"]
        self.assertGreater(both, one)

    def test_scenario_never_exceeds_100(self):
        r = whatif(self.client, {"expense_cut": 3200, "extra_cash": 1e9, "delay_retirement_years": 20,
                                 "extra_investment": 5000}).get_json()
        self.assertLessEqual(r["scenario_score"], 100)

    def test_profile_is_not_mutated(self):
        p = dict(SAMPLE)
        before = dict(SAMPLE)
        clean, errors = scoring.validate_profile(p)
        self.assertFalse(errors)
        snapshot = dict(clean)
        scoring.compare_scenarios(clean, {"expense_cut": 500, "extra_cash": 100})
        self.assertEqual(clean, snapshot)
        self.assertEqual(p, before)

    def test_string_values_from_sliders_are_accepted(self):
        r = whatif(self.client, {"expense_cut": "250", "delay_retirement_years": "1"})
        self.assertEqual(r.status_code, 200)


class WhatIfValidationTests(BaseCase):
    def field_errors(self, adjustments, profile=SAMPLE):
        r = whatif(self.client, adjustments, profile)
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertFalse(body["ok"])
        return body["field_errors"]

    def test_negative_values(self):
        self.assertIn("extra_cash", self.field_errors({"extra_cash": -1}))

    def test_cut_larger_than_expenses(self):
        self.assertIn("expense_cut", self.field_errors({"expense_cut": 3201}))

    def test_delay_too_long(self):
        self.assertIn("delay_retirement_years", self.field_errors({"delay_retirement_years": 21}))

    def test_retirement_age_cannot_pass_100(self):
        self.assertIn("delay_retirement_years",
                      self.field_errors({"delay_retirement_years": 10}, dict(SAMPLE, retirement_age=95)))

    def test_text_bool_nan_and_infinity(self):
        errs = self.field_errors({"expense_cut": "abc", "extra_cash": True, "extra_investment": "nan",
                                  "delay_retirement_years": "inf"})
        self.assertEqual(set(errs), {"expense_cut", "extra_cash", "extra_investment", "delay_retirement_years"})

    def test_adjustments_must_be_an_object(self):
        self.assertIn("_", self.field_errors([1, 2]))

    def test_missing_or_bad_profile(self):
        r = post(self.client, "/api/whatif", {"adjustments": {}})
        self.assertEqual(r.status_code, 400)
        r = post(self.client, "/api/whatif", {"profile": "nope"})
        self.assertEqual(r.status_code, 400)

    def test_invalid_profile_reports_profile_fields(self):
        errs = self.field_errors({}, dict(SAMPLE, age="x"))
        self.assertIn("age", errs)

    def test_bad_json_body(self):
        r = self.client.post("/api/whatif", data="not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_unknown_adjustment_keys_are_ignored(self):
        r = whatif(self.client, {"made_up": 5, "expense_cut": 100})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
