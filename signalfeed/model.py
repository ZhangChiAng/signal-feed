"""Domain models shared across SignalFeed components."""

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class NewsItem:
    """A normalized feed entry.

    Every field is a string so collectors can be swapped without changing the
    filtering, storage, or notification boundaries.
    """

    source: str
    item_id: str
    title: str
    content: str
    url: str
    published_at: str
    author: str
    category: str
    guid: str


@dataclass(frozen=True, slots=True)
class ChineseSummary:
    title_zh: str
    bullets_zh: tuple[str, ...]

    def apply_to(self, item: NewsItem) -> NewsItem:
        content = "\n".join(f"• {bullet}" for bullet in self.bullets_zh)
        return replace(item, title=self.title_zh, content=content)
