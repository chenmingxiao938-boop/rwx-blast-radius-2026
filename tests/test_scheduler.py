 self.assertCountEqual(assigned, durations.keys())
 self.assertTrue(all(workers))
 self.assertLessEqual(len(workers), 8)

    def test_repeat_runs_and_input_orders_agree(self):
 test_ids = list(range(30))
 durations = {test_id: test_id % 5 for test_id in test_ids}
 expected = assign_workers(test_ids, durations)
        for _ in range(3):
 self.assertEqual(assign_workers(test_ids, durations), expected)
 self.assertEqual(assign_workers(test_ids[::-1], durations), expected)

    def test_duplicate_ids_are_scheduled_once(self):
 self.assertEqual(assign_workers([2, 1, 2, 1], {1: 3, 2: 3}), [[1], [2]])

    def test_missing_duration_uses_one_second(self):
 expected = assign_workers([1, 2, 3], {1: 1, 2: 0.5, 3: 0})
 self.assertEqual(assign_workers([1, 2, 3], {2: 0.5, 3: 0}), expected)
 self.assertEqual(expected, [[1, 2, 3]])

    def test_invalid_durations_use_one_second(self):
        for duration in (-4, float("nan"), float("inf"), -float("inf"), "5", None, True):
            with self.subTest(duration=duration):
 self.assertEqual(assign_workers([1, 2], {1: duration, 2: 0.5}), [[1, 2]])

    def test_invalid_test_ids_are_rejected(self):
        for test_id in (True, "1", 1.0, None):
            with self.subTest(test_id=test_id), self.assertRaises(TypeError):
                assign_workers([test_id], {})

    def test_cost_matches_official_formula(self):
 durations = {0: 9, 1: 8, 2: 7, 3: 6, 4: 5}
 workers = assign_workers(list(durations), durations)
 self.assertEqual(allocation_cost(workers, durations), 19)

    def test_parallelism_improves_expensive_tasks(self):
        durations = {1: 10, 2: 10}
        workers = assign_workers(list(durations), durations)
        self.assertEqual(workers, [[1], [2]])
        self.assertLess(allocation_cost(workers, durations), allocation_cost([[1, 2]], durations))

    def test_overhead_favors_one_worker_for_small_tasks(self):
        self.assertEqual(assign_workers([2, 0, 1], {0: 0.125, 1: 0.1, 2: 0}), [[0, 1, 2]])

    def test_equal_durations_have_stable_worker_order(self):
        durations = dict.fromkeys(range(6), 3)
        self.assertEqual(assign_workers([5, 1, 4, 2, 0, 3], durations), [[0, 3], [1, 4], [2, 5]])

    def test_equal_cost_prefers_fewer_workers(self):
        self.assertEqual(assign_workers([1, 2], {1: 2.001, 2: 2.0}), [[1, 2]])

    def test_sub_millisecond_durations_retain_precision(self):
        self.assertEqual(assign_workers([1, 2], {1: 2.0001, 2: 2.0001}), [[1], [2]])

    def test_large_finite_durations_do_not_overflow(self):
        self.assertEqual(assign_workers([1, 2], {1: 1e308, 2: 1e308}), [[1], [2]])


if __name__ == "__main__":
    unittest.main()
