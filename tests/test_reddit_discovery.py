import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile
import json

import agent


class RedditDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp.name) / "state.json"
        self.state = agent.State(self.state_file)

    def tearDown(self):
        self.temp.cleanup()

    def test_state_reddit_cursor_rotation(self):
        self.assertEqual(self.state.get_reddit_cursor("AI & LLMs"), 0)
        self.state.set_reddit_cursor("AI & LLMs", 2)
        self.assertEqual(self.state.get_reddit_cursor("AI & LLMs"), 2)
        self.state.save()

        # Reload from disk
        reloaded = agent.State(self.state_file)
        self.assertEqual(reloaded.get_reddit_cursor("AI & LLMs"), 2)

    @patch("agent.requests.get")
    def test_fetch_reddit_json_handles_rate_limits(self, mock_get):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"x-ratelimit-reset": "0.1"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {"x-ratelimit-remaining": "50", "x-ratelimit-reset": "1"}
        resp_200.json.return_value = {
            "data": {
                "children": [
                    {
                        "data": {
                            "title": "A funny video",
                            "score": 1200,
                            "permalink": "/r/funny/comments/123/video/",
                            "is_video": True,
                            "media": {"reddit_video": {"fallback_url": "https://v.redd.it/123/dash"}},
                            "created_utc": 1600000000,
                        }
                    }
                ]
            }
        }
        mock_get.side_effect = [resp_429, resp_200]

        with patch("time.sleep"):  # don't actually sleep in tests
            items = agent.fetch_reddit_json("funny", sort="hot", limit=1, retries=2)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["data"]["title"], "A funny video")
        self.assertEqual(mock_get.call_count, 2)

    @patch("agent.fetch_reddit_json")
    def test_discover_reddit_categorized_video_and_story(self, mock_fetch):
        mock_fetch.return_value = [
            # Video candidate
            {
                "data": {
                    "title": "Amazing Robot Demo",
                    "score": 850,
                    "permalink": "/r/ClaudeAI/comments/abc/demo/",
                    "is_video": True,
                    "media": {"reddit_video": {"fallback_url": "https://v.redd.it/abc"}},
                    "created_utc": 1600000000,
                }
            },
            # Story (image) candidate
            {
                "data": {
                    "title": "Striking 3D Render",
                    "score": 2500,
                    "permalink": "/r/blender/comments/xyz/render/",
                    "is_video": False,
                    "url": "https://i.redd.it/example.jpg",
                    "selftext": "Rendered with Cycles in Blender 4.2",
                    "created_utc": 1600000000,
                }
            },
        ]

        categories = {
            "Tech": ["ClaudeAI"],
            "Art": ["blender"]
        }

        candidates = agent.discover_reddit_categorized(
            categories=categories,
            state=self.state,
            rotate_per_cat=1,
            limit=5,
            min_score=500,
            story_min_score=2000,
        )

        self.assertEqual(len(candidates), 4)  # 2 per subreddit across 2 categories
        videos = [c for c in candidates if c.kind == "video"]
        stories = [c for c in candidates if c.kind == "story"]

        self.assertEqual(len(videos), 2)
        self.assertEqual(len(stories), 2)
        self.assertEqual(videos[0].source, "reddit:Tech:r/ClaudeAI")
        self.assertEqual(stories[0].source, "reddit:Tech:r/ClaudeAI")
        self.assertIn("https://i.redd.it/example.jpg", stories[0].images)


if __name__ == "__main__":
    unittest.main()
