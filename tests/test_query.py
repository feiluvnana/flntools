import unittest
from dataclasses import dataclass

from shared import Query
from shared._query import Plan


@dataclass
class Item:
    name: str
    kind: str
    size: int


ITEMS = [Item("a", "image", 3), Item("b", "video", 9), Item("c", "image", 1), Item("d", "image", 7)]


class QueryTest(unittest.TestCase):
    def test_get_runs_filters_sort_and_limit(self):
        query = Query(lambda: ITEMS).filter(kind="image").sort("size", desc=True).limit(2)
        self.assertEqual([item.name for item in query.get()], ["d", "a"])

    def test_a_predicate_and_equality_chain(self):
        query = Query(lambda: ITEMS).filter(lambda item: item.size > 2).filter(kind="image")
        self.assertEqual([item.name for item in query.get()], ["a", "d"])

    def test_option_methods_return_new_queries(self):
        base = Query(lambda: ITEMS)
        base.filter(kind="video")
        self.assertEqual(base.count, 4)

    def test_first_and_count(self):
        query = Query(lambda: ITEMS).filter(kind="image")
        self.assertEqual(query.count, 3)
        self.assertEqual(query.first, ITEMS[0])
        self.assertIsNone(query.filter(kind="audio").first)

    def test_mappings_filter_by_key_and_sort_by_function(self):
        rows = [{"n": 2}, {"n": 1}, {"m": 0}]
        self.assertEqual(Query(lambda: rows).filter(n=2).get(), [{"n": 2}])
        self.assertEqual(Query(lambda: rows[:2]).sort(lambda row: row["n"]).get(), [{"n": 1}, {"n": 2}])

    def test_the_source_is_read_each_time(self):
        rows = [1]
        query = Query(lambda: rows)
        rows.append(2)
        self.assertEqual(query.get(), [1, 2])

    def test_a_source_runs_the_whole_plan(self):
        plans = []

        class Pushed:
            def run(self, plan):
                plans.append(plan)
                return ["x"]

            def count(self, plan):
                return 42 + len(plan.equal) * 0

        query = Query(Pushed()).filter(kind="a").limit(3)
        self.assertEqual(query.get(), ["x"])
        self.assertEqual(query.count, 42)
        self.assertEqual(plans[0], Plan(equal=(("kind", "a"),), limit=3))

    def test_a_negative_limit_is_refused(self):
        with self.assertRaises(ValueError):
            Query(lambda: ITEMS).limit(-1)


if __name__ == "__main__":
    unittest.main()
