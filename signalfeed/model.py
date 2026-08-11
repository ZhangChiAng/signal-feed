"""Domain models shared across SignalFeed components."""

from dataclasses import dataclass


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
