"""`flnd.query` and the kinds of `flnd.finder`: parsing, checks, the OR split, limits and lookups.

The params are an example site's, shaped like a manga reader's; lookups are plain functions that
count their calls. Nothing touches the network.
"""

import datetime
import unittest

from flnd.finder import Choice, Date, Name, Number, Param, Query, QueryError, Term, Text
from flnd.query import MAX_SEARCHES, plan
from flnd.source import Range

GENRES = [("Vanilla", "1"), ("Romance", "2"), ("Example", "3"), ("Đam mỹ", "4")]


class Lookups:
    """Lookups that answer from fixed lists and record each name they were asked for."""

    def __init__(self):
        self.asked: list[str] = []

    def genres(self, site, name):
        self.asked.append(name)
        return GENRES

    def authors(self, site, name):
        self.asked.append(name)
        return [("example creator", "10"), ("Example Creator", "11"), ("example_user", "12")]


def params(lookups: Lookups) -> dict[str, Param]:
    return {
        "title": Param(Text, "words in the title"),
        "genre": Param(Name, "a genre", negate=True, lookup=lookups.genres),
        "author": Param(Name, "an author", lookup=lookups.authors),
        "parody": Param(Name, "the work it parodies"),
        "character": Param(Name, "a character"),
        "chapters": Param(Number, "how many chapters", compare=False),
        "views": Param(Number, "how many views"),
        "updated": Param(Date, "last updated"),
        "kind": Param(Choice("page", "user"), "what to find"),
        "tag": Param(Text, "a tag", any_of=True, negate=True),
    }


class QueryCase(unittest.TestCase):
    def setUp(self):
        self.lookups = Lookups()
        self.params = params(self.lookups)

    def plan(self, text: str, **options) -> list[Query]:
        return plan(text, self.params, **options)

    def refused(self, text: str) -> str:
        with self.assertRaises(QueryError) as caught:
            self.plan(text)
        return str(caught.exception)


class ReadTest(QueryCase):
    def test_free_words_and_phrases_are_the_text(self):
        self.assertEqual(self.plan('example words "a phrase"'), [Query("example words a phrase")])

    def test_a_param_value_is_read_by_its_kind(self):
        found = self.plan('title:"two words" chapters:3 updated:2026-01-02 kind:PAGE')
        self.assertEqual(found[0].terms, (Term("title", ("two words",)), Term("chapters", (3,)),
                                          Term("updated", (datetime.date(2026, 1, 2),)),
                                          Term("kind", ("page",))))

    def test_ranges_and_comparisons_become_included_bounds(self):
        cases = {"views:[3 TO 8]": (3, 8), "views:{3 TO 8}": (4, 7), "views:[3 TO *]": (3, None),
                 "views:>5": (6, None), "views:>=5": (5, None), "views:<10": (None, 9),
                 "updated:[2026-01-01 TO 2026-06-30]": (datetime.date(2026, 1, 1),
                                                        datetime.date(2026, 6, 30))}
        for text, (low, high) in cases.items():
            with self.subTest(text):
                param = text.split(":")[0]
                self.assertEqual(self.plan(text)[0].terms, (Term(param, low=low, high=high),))

    def test_time_is_set_on_every_search(self):
        time = Range(since=datetime.date(2026, 1, 1))
        found = self.plan("parody:a OR parody:b", time=time)
        self.assertEqual([query.time for query in found], [time, time])

    def test_plus_and_side_by_side_mean_and(self):
        self.assertEqual(self.plan("+a b AND c"), [Query("a b c")])


class SplitTest(QueryCase):
    def test_an_or_the_site_cannot_send_runs_as_several_searches(self):
        found = self.plan("genre:(vanilla OR romance) -genre:example")
        self.assertEqual([query.terms for query in found], [
            (Term("genre", ("1",)), Term("genre", ("3",), negated=True)),
            (Term("genre", ("2",)), Term("genre", ("3",), negated=True)),
        ])

    def test_an_or_across_params_distributes_over_the_and(self):
        found = self.plan("(parody:example OR character:example) chapters:1")
        self.assertEqual([query.terms for query in found], [
            (Term("parody", ("example",)), Term("chapters", (1,))),
            (Term("character", ("example",)), Term("chapters", (1,))),
        ])

    def test_an_and_within_a_param_is_one_search_of_several_terms(self):
        found = self.plan("genre:(vanilla AND romance)")
        self.assertEqual(found, [Query("", (Term("genre", ("1",)), Term("genre", ("2",))))])

    def test_de_morgan_turns_a_negated_or_into_negated_terms(self):
        found = self.plan("-(genre:vanilla OR genre:romance)")
        self.assertEqual(found, [Query("", (Term("genre", ("1",), negated=True),
                                            Term("genre", ("2",), negated=True)))])

    def test_a_negated_and_becomes_an_or_of_negations(self):
        found = self.plan("NOT (genre:vanilla AND genre:romance)")
        self.assertEqual([query.terms for query in found], [
            (Term("genre", ("1",), negated=True),), (Term("genre", ("2",), negated=True),)])

    def test_a_double_negation_cancels(self):
        found = self.plan("-(-author:example_user)")
        self.assertEqual(found, [Query("", (Term("author", ("12",)),))])

    def test_an_any_of_param_sends_its_or_whole(self):
        found = self.plan("tag:(a OR b) OR tag:c OR parody:d")
        self.assertEqual([query.terms for query in found], [(Term("tag", ("a", "b", "c")),),
                                                             (Term("parody", ("d",)),)])

    def test_same_searches_and_terms_are_kept_once(self):
        self.assertEqual(self.plan("a OR a"), [Query("a")])
        self.assertEqual(self.plan("parody:x parody:x"), [Query("", (Term("parody", ("x",)),))])

    def test_eight_searches_pass_and_nine_are_refused_with_the_count(self):
        eight = " OR ".join(f"w{n}" for n in range(MAX_SEARCHES))
        self.assertEqual(len(self.plan(eight)), MAX_SEARCHES)
        message = self.refused("(a OR b OR c) (d OR e OR f)")
        self.assertIn("needs 9 searches", message)


class RefuseTest(QueryCase):
    def test_what_the_site_cannot_do_is_refused_naming_the_part(self):
        cases = {
            "colour:red": "'colour:red': no parameter colour; this site takes title, genre",
            "chapters:>5": "'chapters:>5': this site can't compare chapters",
            "-author:x": "'author:x': this site can't exclude by author",
            "-word": "'word': this site can't exclude words",
            "views:many": "'views:many': views takes a whole number",
            "updated:2026-13-01": "updated takes a day (YYYY-MM-DD)",
            "kind:other": "'kind:other': kind takes one of page, user",
            "title:[a TO b]": "'title:[a TO b]': title takes no range",
            "views:[9 TO 3]": "the range is empty",
            "[1 TO 3]": "a range or comparison needs a parameter",
            "exa*": "'exa*': flnd queries take no wildcards",
            "word~2": "flnd queries take no fuzzy",
            "parody:(character:x)": "a parameter can't hold another",
            "": "the query is empty",
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertIn(expected, self.refused(text))

    def test_a_syntax_error_says_where(self):
        self.assertIn("can't read the query: unexpected ')' at position 6", self.refused("genre:)"))

    def test_a_refusal_comes_before_any_lookup(self):
        self.refused("genre:vanilla colour:red")
        self.refused("(genre:a OR genre:b OR genre:c) (views:1 OR views:2 OR views:3)")
        self.assertEqual(self.lookups.asked, [])


class LookupTest(QueryCase):
    def test_names_match_ignoring_case_accents_and_spaces(self):
        found = self.plan('genre:VANILLA genre:"dam  my"')
        self.assertEqual(found[0].terms, (Term("genre", ("1",)), Term("genre", ("4",))))

    def test_each_name_is_asked_once_and_kept_in_the_cache(self):
        cache: dict[str, str] = {}
        self.plan("genre:vanilla OR (genre:vanilla parody:x)", cache=cache)
        self.assertEqual(self.lookups.asked, ["vanilla"])
        self.assertEqual(cache, {"genre:vanilla": "1"})
        self.plan("genre:Vanilla", cache=cache)
        self.assertEqual(self.lookups.asked, ["vanilla"])

    def test_a_name_with_several_matches_stops_with_the_candidates(self):
        message = self.refused('author:"example creator"')
        self.assertIn("several of author are named example creator", message)
        self.assertIn("example creator (10), Example Creator (11)", message)

    def test_an_unknown_name_shows_near_ones(self):
        message = self.refused("author:example")
        self.assertIn("no author named example; like it: example creator", message)
        self.assertTrue(self.refused("genre:nothing").endswith("no genre named nothing"))

    def test_the_site_is_passed_to_the_lookup(self):
        seen = []

        def genres(site, name):
            seen.append(site)
            return GENRES

        self.params["genre"] = Param(Name, "a genre", lookup=genres)
        self.plan("genre:vanilla", site="the site")
        self.assertEqual(seen, ["the site"])


class KindTest(unittest.TestCase):
    def test_a_kind_may_be_declared_as_a_class_or_an_instance(self):
        self.assertIsInstance(Param(Number, "n").reader, Number)
        choice = Choice("a", "b")
        self.assertIs(Param(choice, "c").reader, choice)


if __name__ == "__main__":
    unittest.main()
