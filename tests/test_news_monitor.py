from __future__ import annotations

import unittest

from news_monitor import parse_feed_bytes, rank_news_items


RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <item>
      <title>Trump says Iran faces new pressure</title>
      <link>https://example.com/a</link>
      <pubDate>Mon, 01 Jan 2026 10:00:00 GMT</pubDate>
      <description>Donald Trump comments on Iran and oil markets.</description>
    </item>
    <item>
      <title>Football headline</title>
      <link>https://example.com/b</link>
      <pubDate>Mon, 01 Jan 2026 09:00:00 GMT</pubDate>
      <description>Not relevant.</description>
    </item>
  </channel>
</rss>
"""


class NewsMonitorTests(unittest.TestCase):
    def test_parse_feed_bytes_reads_rss_items(self) -> None:
        items = parse_feed_bytes(RSS_SAMPLE, default_source="Example")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["source"], "Example")
        self.assertIn("Trump says Iran", items[0]["title"])

    def test_rank_news_items_prioritises_trump_and_iran(self) -> None:
        parsed = parse_feed_bytes(RSS_SAMPLE, default_source="Example")
        ranked = rank_news_items(parsed, keywords=["trump", "iran", "oil"], limit=5)
        self.assertEqual(len(ranked), 1)
        self.assertIn("Trump", ranked[0]["title"])
        self.assertGreaterEqual(ranked[0]["score"], 6)


if __name__ == "__main__":
    unittest.main()
