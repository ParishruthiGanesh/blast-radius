"""The worker split must follow the scorer's rules and never cost more than one worker."""

import random
import unittest

from blast import schedule


def scorer_cost(tests: dict, workers: list) -> int:
    if not workers:
        return 0
    return max(sum(tests[n] for n in worker) for worker in workers) + 2000 * len(workers)


class TestSchedule(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(schedule.plan({}), (0, []))

    def test_rules_and_cost(self):
        rng = random.Random(1)
        for _ in range(200):
            tests = {n: rng.choice([1, 5, 50, 500, 3000, 21000]) for n in rng.sample(range(2000), rng.randint(1, 300))}
            cost, workers = schedule.plan(tests)
            flat = [n for worker in workers for n in worker]
            self.assertEqual(sorted(flat), sorted(tests))  # every test once
            self.assertTrue(1 <= len(workers) <= 8)
            self.assertTrue(all(workers))
            self.assertEqual(cost, scorer_cost(tests, workers))
            self.assertLessEqual(cost, sum(tests.values()) + 2000)

    def test_one_long_test_gets_its_own_worker(self):
        tests = {0: 28817, **{n: 50 for n in range(1, 1500)}}
        cost, workers = schedule.plan(tests)
        self.assertIn([0], workers)
        self.assertLess(cost, 28817 + 2000 * 8)


if __name__ == "__main__":
    unittest.main()
