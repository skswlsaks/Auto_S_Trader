import unittest

from modeling.pattern_eval import match_events, wilson


class EventMatchingTest(unittest.TestCase):
    def test_global_matching_preserves_recall_when_nearest_first_would_fail(self):
        pairs = match_events([0, 5], [4, 9], tolerance=5)
        self.assertEqual(set(pairs), {(0, 0), (1, 1)})

    def test_repeated_alarms_cannot_inflate_recall(self):
        pairs = match_events([20], [19, 20, 21])
        self.assertEqual(pairs, [(0, 1)])

    def test_matching_cannot_cross_tolerance_or_reuse_event(self):
        self.assertEqual(match_events([20], [26]), [])
        self.assertEqual(match_events([20], [25]), [(0, 0)])
        self.assertEqual(match_events([], [20]), [])
        self.assertEqual(match_events([20], []), [])

    def test_timing_distance_breaks_equal_cardinality_ties(self):
        self.assertEqual(match_events([10, 30], [11, 29, 34]), [(0, 0), (1, 1)])


class IntervalTest(unittest.TestCase):
    def test_zero_and_perfect_scores_still_have_uncertainty(self):
        low = wilson(0, 40)
        high = wilson(40, 40)
        self.assertAlmostEqual(low[0], 0.)
        self.assertGreater(low[1], .08)
        self.assertLess(high[0], .92)
        self.assertAlmostEqual(high[1], 1.)
        self.assertIsNone(wilson(0, 0))


if __name__ == "__main__":
    unittest.main()
