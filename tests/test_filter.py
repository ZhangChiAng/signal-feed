import unittest

from signalfeed.filter import KeywordFilter
from tests.helpers import news_item


class KeywordFilterTests(unittest.TestCase):
    def test_matches_selected_title_and_content_case_insensitively(self) -> None:
        keyword_filter = KeywordFilter(("GPT", "API"), ("title", "content"))
        self.assertTrue(
            keyword_filter.matches(news_item(title="New gPt model", content=""))
        )
        self.assertTrue(
            keyword_filter.matches(news_item(title="Other", content="An api update"))
        )
        self.assertFalse(
            keyword_filter.matches(news_item(title="Other", content="Nothing relevant"))
        )

    def test_short_keywords_use_ascii_boundaries(self) -> None:
        keyword_filter = KeywordFilter(("API", "IDE", "AI"), ("title",))
        self.assertFalse(
            keyword_filter.matches(news_item(title="rapid evidence chair"))
        )
        self.assertTrue(keyword_filter.matches(news_item(title="API, IDE and AI")))
        self.assertTrue(keyword_filter.matches(news_item(title="发布API能力")))

    def test_only_configured_fields_are_searched(self) -> None:
        keyword_filter = KeywordFilter(("API",), ("title",))
        self.assertFalse(
            keyword_filter.matches(news_item(title="Other", content="API"))
        )


if __name__ == "__main__":
    unittest.main()
