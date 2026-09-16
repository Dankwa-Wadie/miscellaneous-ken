"""Template selection, both overlay renderers, and the image-sizing contract."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import render
import render_story


def layout(**overrides):
    return {**render_story.DEFAULT_STORY_LAYOUT, **overrides}


def make_image(path, size):
    Image.new("RGB", size, (40, 60, 90)).save(path)
    return str(path)


class TemplateSelectionTests(unittest.TestCase):
    def test_explicit_choice_wins_over_content(self):
        for forced in ("reaction_card", "classic"):
            got = render_story.resolve_story_template(
                layout(template=forced), "Headline", "Punchline", ["a.png", "b.png"])
            self.assertEqual(got, forced)

    def test_two_images_use_the_classic_card(self):
        # A pair is a side-by-side comparison; the reaction card has one hero.
        got = render_story.resolve_story_template(
            layout(), "Headline", "Punchline", ["a.png", "b.png"])
        self.assertEqual(got, "classic")

    def test_no_image_uses_the_classic_card(self):
        got = render_story.resolve_story_template(layout(), "Headline", "Punchline", [])
        self.assertEqual(got, "classic")

    def test_single_image_with_punchline_uses_the_reaction_card(self):
        got = render_story.resolve_story_template(
            layout(), "Headline", "The punchline", ["a.png"])
        self.assertEqual(got, "reaction_card")

    def test_ambiguous_post_is_stable_across_calls(self):
        # One image, no commentary: decided by hash, but never changes for the
        # same post — a re-render must not restyle an existing draft.
        first = render_story.resolve_story_template(layout(), "A joke", "", ["a.png"])
        for _ in range(5):
            self.assertEqual(
                render_story.resolve_story_template(layout(), "A joke", "", ["a.png"]),
                first)
        self.assertIn(first, ("reaction_card", "classic"))

    def test_ambiguous_posts_split_both_ways(self):
        picks = {render_story.resolve_story_template(layout(), f"Headline {i}", "", ["a.png"])
                 for i in range(40)}
        self.assertEqual(picks, {"reaction_card", "classic"})

    def test_theme_auto_follows_the_template(self):
        self.assertEqual(render_story.resolve_theme(layout(), "reaction_card"), "light")
        self.assertEqual(render_story.resolve_theme(layout(), "classic"), "dark")

    def test_caption_card_is_selectable(self):
        got = render_story.resolve_story_template(
            layout(template="caption_card"), "Headline", "Punchline", ["a.png"])
        self.assertEqual(got, "caption_card")

    def test_theme_override_wins(self):
        self.assertEqual(render_story.resolve_theme(layout(theme="dark"), "reaction_card"), "dark")
        self.assertEqual(render_story.resolve_theme(layout(theme="light"), "classic"), "light")


class ImageSizingTests(unittest.TestCase):
    """A tall screenshot must not be shrunk to the old fixed cap: the readable
    text inside it is the joke. Regression guard for both templates."""

    def test_single_tall_image_exceeds_the_fixed_cap(self):
        with tempfile.TemporaryDirectory() as td:
            src = make_image(Path(td) / "tall.png", (1080, 2076))
            cap = int(render_story.DEFAULT_STORY_LAYOUT["image_max_height"])
            sized = render_story.size_images([src], 912, 1486, layout())
            self.assertEqual(len(sized), 1)
            self.assertGreater(sized[0].height, cap)

    def test_spare_height_smaller_than_cap_keeps_the_cap(self):
        with tempfile.TemporaryDirectory() as td:
            src = make_image(Path(td) / "tall.png", (1080, 2076))
            sized = render_story.size_images([src], 912, 100, layout())
            self.assertEqual(sized[0].height,
                             int(render_story.DEFAULT_STORY_LAYOUT["image_max_height"]))

    def test_wide_image_is_bounded_by_width(self):
        with tempfile.TemporaryDirectory() as td:
            src = make_image(Path(td) / "wide.png", (1920, 1080))
            sized = render_story.size_images([src], 912, 1486, layout())
            self.assertLessEqual(sized[0].width, 912)


class CaptionCardTests(unittest.TestCase):
    def test_accents_split_into_runs(self):
        self.assertEqual(
            render.split_accents("plain *bold* tail"),
            [("plain ", False), ("bold", True), (" tail", False)])

    def test_punctuation_stays_with_the_accented_phrase(self):
        runs = render.split_accents("he is *the GOAT*. Right?")
        words = ["".join(t for t, _ in tok) for tok in render.tokenize_runs(runs)]
        self.assertIn("GOAT.", words)
        self.assertNotIn(".", words)

    def test_window_keeps_the_clip_aspect(self):
        account = render.Account(name="Ken", handle="@ken", verified=True)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "overlay.png"
            lay = {**render.DEFAULT_LAYOUT, **render_story.DEFAULT_STORY_LAYOUT}
            for aspect in (9 / 16, 1.0, 16 / 9):
                box = render.build_caption_overlay(
                    "A caption with *accent* text", account, lay, out,
                    video_aspect=aspect)
                _, _, w, h = box
                self.assertAlmostEqual(w / h, aspect, places=2)


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.account = render.Account(name="Miscellaneous Ken", handle="@mken", verified=True)

    def _overlay(self, builder, td, **kwargs):
        img = make_image(Path(td) / "hero.png", (1080, 1525))
        out = Path(td) / "overlay.png"
        box = builder("A hook headline", "The punchline", [img], self.account,
                      layout(**kwargs.pop("layout", {})), out, **kwargs)
        return box, out

    def test_classic_overlay_covers_the_canvas(self):
        with tempfile.TemporaryDirectory() as td:
            box, out = self._overlay(render_story.build_story_overlay, td)
            self.assertEqual(box, (0, 0, 1080, 1920))
            with Image.open(out) as im:
                self.assertEqual(im.size, (1080, 1920))

    def test_reaction_overlay_matches_the_classic_contract(self):
        with tempfile.TemporaryDirectory() as td:
            for theme in ("light", "dark"):
                box, out = self._overlay(render_story.build_reaction_overlay, td, theme=theme)
                self.assertEqual(box, (0, 0, 1080, 1920))
                with Image.open(out) as im:
                    self.assertEqual(im.size, (1080, 1920))
                    self.assertEqual(im.mode, "RGBA")

    def test_themes_differ(self):
        with tempfile.TemporaryDirectory() as td:
            _, light = self._overlay(render_story.build_reaction_overlay, td, theme="light")
            light_bytes = Path(light).read_bytes()
            _, dark = self._overlay(render_story.build_reaction_overlay, td, theme="dark")
            self.assertNotEqual(light_bytes, Path(dark).read_bytes())

    def test_subscribe_badge_changes_the_render(self):
        with tempfile.TemporaryDirectory() as td:
            _, off = self._overlay(render_story.build_reaction_overlay, td,
                                   layout={"show_subscribe": False})
            off_bytes = Path(off).read_bytes()
            _, on = self._overlay(render_story.build_reaction_overlay, td,
                                  layout={"show_subscribe": True})
            self.assertNotEqual(off_bytes, Path(on).read_bytes())


class EndToEndTests(unittest.TestCase):
    """Renders real video, so it needs ffmpeg and the mascot asset."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent.parent
        cls.mascot = cls.root / "assets" / "mascot.mp4"
        if not cls.mascot.exists():
            raise unittest.SkipTest("mascot asset not available")

    def test_each_template_renders_and_reports_itself(self):
        cfg = {"story_layout": {"background_dim": 0.4}}
        with tempfile.TemporaryDirectory() as td:
            img = make_image(Path(td) / "hero.png", (1080, 1525))
            for template in ("reaction_card", "classic"):
                out = Path(td) / f"{template}.mp4"
                result = render_story.render_story(
                    headline="A hook headline", commentary="The punchline",
                    images=[img], mascot=self.mascot, out=out,
                    config={**cfg, "story_layout": {**cfg["story_layout"],
                                                    "template": template}},
                    duration=2, workdir=Path(td))
                self.assertEqual(result.template, template)
                self.assertTrue(out.exists() and out.stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
