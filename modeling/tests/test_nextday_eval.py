import copy
import unittest

from modeling.nextday_eval import REPO, prior_sessions, replay_daily
from modeling.pattern_eval import PROTOCOL, audit_protocol


class NextDaySelectionTest(unittest.TestCase):
    def test_previous_session_skips_weekend_and_holiday(self):
        dates = ["2026-10-02", "2026-10-08", "2026-10-12"]
        self.assertEqual(prior_sessions(dates[1:], dates),
                         {"2026-10-08": "2026-10-02", "2026-10-12": "2026-10-08"})

    @unittest.skipUnless((REPO / "trading/nextday_watch.mjs").exists(), "trading/ scanners are not part of this checkout")
    def test_next_day_prices_cannot_change_prior_day_selection(self):
        import pandas as pd
        bars = [{"date": d.strftime("%Y-%m-%d"), "open": 100., "high": 101., "low": 99., "close": 100., "volume": 1000.}
                for d in pd.bdate_range("2026-01-01", periods=26)]
        bars[-2].update(open=100., high=108., low=99., close=107.)
        selection_day = bars[-2]["date"]
        first = replay_daily([{"symbol": "005930", "bars": bars}], [selection_day])
        altered = copy.deepcopy(bars)
        altered[-1].update(open=200., high=400., low=1., close=350., volume=999999.)
        second = replay_daily([{"symbol": "005930", "bars": altered}], [selection_day])
        prefix = replay_daily([{"symbol": "005930", "bars": bars[:-1]}], [selection_day])
        self.assertEqual(first, second)
        self.assertEqual(first, prefix)
        self.assertTrue(first["rows"][0]["selected"])
        self.assertEqual(first["rows"][0]["reference"]["date"], selection_day)

    def test_cohort_protocol_does_not_mutate_unfiltered_baseline(self):
        before = copy.deepcopy(PROTOCOL)
        self.assertEqual(audit_protocol()["version"], "lecture-shape-audit-v1")
        self.assertEqual(audit_protocol({"policy": "union"})["version"], "lecture-shape-audit-nextday-v1")
        self.assertEqual(PROTOCOL, before)


if __name__ == "__main__":
    unittest.main()
