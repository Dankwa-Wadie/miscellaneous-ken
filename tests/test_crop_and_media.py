import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile
from PIL import Image

import scrape_reddit
import agent
import render
import studio_server


class RedditMediaAndCropTests(unittest.TestCase):
    def test_extract_reddit_media_gallery(self):
        post = {
            "is_gallery": True,
            "media_metadata": {
                "img1": {"s": {"u": "https://preview.redd.it/test1.jpg?width=1080&amp;crop=smart"}},
                "img2": {"s": {"u": "https://preview.redd.it/test2.png?width=1080&amp;crop=smart"}},
            },
            "gallery_data": {"items": [{"media_id": "img1"}, {"media_id": "img2"}]},
            "url": "https://www.reddit.com/gallery/abcde",
        }
        images, vid, primary, is_v, is_g = scrape_reddit.extract_reddit_media(post)
        self.assertTrue(is_g)
        self.assertFalse(is_v)
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0], "https://preview.redd.it/test1.jpg?width=1080&crop=smart")
        self.assertEqual(images[1], "https://preview.redd.it/test2.png?width=1080&crop=smart")
        self.assertEqual(primary, images[0])
        self.assertNotIn("&amp;", primary)

    def test_extract_reddit_media_packaged_video(self):
        video_url = (
            "https://packaged-media.redd.it/kgub62s2xndh1/pb/m2-res_1280p.mp4"
            "?m=DASHPlaylist.mpd&amp;var=sgpssan&v=1&e=1788980400&s=0fcfc55682fdf5f4ff1a29c9287d1cf423126325"
        )
        post = {
            "is_video": True,
            "media": {
                "reddit_video": {
                    "fallback_url": video_url
                }
            },
            "url_overridden_by_dest": video_url,
        }
        images, vid, primary, is_v, is_g = scrape_reddit.extract_reddit_media(post)
        self.assertTrue(is_v)
        self.assertFalse(is_g)
        self.assertIn("packaged-media.redd.it", vid)
        self.assertNotIn("&amp;", vid)
        self.assertEqual(primary, vid)

    def test_extract_reddit_media_text_post(self):
        post = {
            "is_video": False,
            "is_gallery": False,
            "url": "https://www.reddit.com/r/funny/comments/123/funny_joke/",
            "selftext": "Why did the chicken cross the road? To get to the other side.",
        }
        images, vid, primary, is_v, is_g = scrape_reddit.extract_reddit_media(post)
        self.assertFalse(is_v)
        self.assertFalse(is_g)
        self.assertEqual(images, [])
        self.assertEqual(vid, "")
        self.assertEqual(primary, "")  # Never returns subreddit or post URL as media URL

    def test_inspect_reddit_packaged_media_url(self):
        url = (
            "https://packaged-media.redd.it/kgub62s2xndh1/pb/m2-res_1280p.mp4"
            "?m=DASHPlaylist.mpd&var=sgpssan&v=1&e=1788980400&s=0fcfc55682fdf5f4ff1a29c9287d1cf423126325"
        )
        info = studio_server.inspect_reddit_url(url)
        self.assertTrue(info["is_video"])
        self.assertEqual(info["media_url"], url)
        self.assertEqual(info["video_url"], url)

    def test_render_build_overlay_aspect_bounds(self):
        with tempfile.TemporaryDirectory() as td:
            out_overlay = Path(td) / "overlay.png"
            account = render.Account()
            layout = dict(render.DEFAULT_LAYOUT)

            # Test 16:9
            box_16_9 = render.build_overlay("TEST HEADLINE", account, layout, out_overlay, video_aspect="16:9")
            self.assertGreater(box_16_9[2], box_16_9[3])  # width > height

            # Test 1:1
            box_1_1 = render.build_overlay("TEST HEADLINE", account, layout, out_overlay, video_aspect="1:1")
            self.assertEqual(box_1_1[2], box_1_1[3])  # width == height

            # Ensure box doesn't overflow canvas height (1920)
            self.assertLess(box_1_1[1] + box_1_1[3], 1920)

    def test_analyze_visual_crop_fallback_on_geometry(self):
        with tempfile.TemporaryDirectory() as td:
            # Create a vertical image (720x1280)
            im_path = Path(td) / "vertical.jpg"
            im = Image.new("RGB", (720, 1280), color=(10, 10, 10))
            im.save(im_path)

            # Without LLM providers, fallback should detect vertical media and recommend contain
            res = agent.analyze_visual_crop(im_path, config={"editorial": {"providers": []}})
            self.assertEqual(res["fit_mode"], "contain")
            self.assertEqual(res["aspect_ratio"], "1:1")
            self.assertTrue(res["has_top_text"])


if __name__ == "__main__":
    unittest.main()
