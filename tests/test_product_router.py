"""
tests/test_product_router.py — V4 Phase 1 router rule tests.

Runs the RULE layer only (LLM judge disabled) so it is deterministic and
needs no API keys:

    ROUTER_LLM_ENABLED=false python -m pytest tests/test_product_router.py
    # or plain: ROUTER_LLM_ENABLED=false python -m unittest tests.test_product_router

Cases mirror the pins that were hand-linked on 28–29 Oct 2026 (the manual
run is the router's ground truth): garden pins -> Garden Kit, interior
pins -> Decor Kit, scenery/Christmas/tech pins -> clean (no destination).

Update (owner, 2 Nov 2026): pins carrying a Garden/Scenery THEME now route
to the prompt pack (they posted linkless before); unthemed scenery pins
still post clean via the product rules, unchanged.
"""

import os
import sys
import unittest

os.environ["ROUTER_LLM_ENABLED"] = "false"  # must precede router import

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mastermind.product_router import route_product  # noqa: E402

GARDEN_URL = "https://cozycanvasstudio.gumroad.com/l/nmkawd"
DECOR_URL = "https://cozycanvasstudio.gumroad.com/l/ictsf"
PACK_URL = "https://cozycanvasstudio.gumroad.com/l/igopxj"


def pin(title, description="", visual_prompt="", board_name="", niche="", tags=None):
    return {
        "title": title,
        "description": description,
        "visual_prompt": visual_prompt,
        "board_name": board_name,
        "niche": niche,
        "tags": tags or [],
    }


class TestProductRouter(unittest.TestCase):
    def test_cottage_garden_to_garden_kit(self):
        d = route_product(pin(
            "I Didn't Know A Morning Cottage Garden Could Look Like This",
            description="A dreamy cottage garden with roses and a stone garden path",
            visual_prompt="cottage garden full of blooms, garden gate, morning light",
            board_name="Spring Aesthetic & Nature",
        ))
        self.assertEqual(d["product_id"], "garden_kit")
        self.assertEqual(d["destination_url"], GARDEN_URL)
        self.assertTrue(d["product_cta"])

    def test_balcony_garden_to_garden_kit(self):
        d = route_product(pin(
            "Small Balcony Garden Ideas That Feel Like A Retreat",
            visual_prompt="balcony garden with potted plants and a garden swing",
            board_name="Cozy Balcony & Porch Ideas",
        ))
        self.assertEqual(d["product_id"], "garden_kit")

    def test_bedroom_to_decor_kit(self):
        d = route_product(pin(
            "Blush Pastel Bedroom Styling Ideas",
            description="Cozy bedroom decor with soft glam styling and layered bedding",
            visual_prompt="pastel bedroom interior, cozy corner, warm lamp light",
            board_name="Cozy Home Interiors",
        ))
        self.assertEqual(d["product_id"], "decor_kit")
        self.assertEqual(d["destination_url"], DECOR_URL)

    def test_living_room_to_decor_kit(self):
        d = route_product(pin(
            "Cream Living Room With A Reading Nook You'll Love",
            visual_prompt="bright living room interior styling, sofa and coffee table",
            board_name="dream home inspiration",
        ))
        self.assertEqual(d["product_id"], "decor_kit")

    def test_mountain_scenery_posts_clean(self):
        d = route_product(pin(
            "I Didn't Know Freedom Could Look Like This",
            description="A scenic mountain landscape at sunset, pure nature view",
            visual_prompt="mountain valley landscape, ocean in distance, scenic sky",
            board_name="Nature Views & Scenic Escapes",
        ))
        self.assertEqual(d["product_id"], "")
        self.assertEqual(d["destination_url"], "")

    def test_christmas_pin_posts_clean(self):
        # Owner's 29 Oct decision: kits don't fit the newest Christmas pins.
        d = route_product(pin(
            "Magical Christmas Tree Corner With Twinkling Lights",
            description="christmas xmas decor with a glowing tree",
            visual_prompt="christmas tree with ornaments and fairy lights",
            board_name="Christmas",
        ))
        self.assertEqual(d["product_id"], "")

    def test_tech_account_always_clean(self):
        d = route_product(pin(
            "Dream Garden Office Setup With Plants",
            visual_prompt="garden office desk setup",
        ), account_key="account_2")
        self.assertEqual(d["product_id"], "")

    def test_vague_pin_posts_clean(self):
        d = route_product(pin("Beautiful Aesthetic Vibes", description="so pretty"))
        self.assertEqual(d["product_id"], "")


BLOG = "https://pinteresto.vercel.app"


def themed_pin(theme, title="A Pretty Pin", **kw):
    """A strategy dict carrying a Prompts_Master theme, like node_cmo emits."""
    strategy = pin(title, **kw)
    strategy["theme"] = theme
    return strategy


class TestBlogThemeMapping(unittest.TestCase):
    """Pin → Blog mapping (owner, 1 Nov 2026): theme decides before products."""

    def test_bedroom_theme_to_bedroom_post(self):
        d = route_product(themed_pin(
            "Bedroom",
            title="Blush Pastel Bedroom Styling Ideas",
            visual_prompt="pastel bedroom interior, cozy corner",
        ))
        self.assertEqual(d["destination_url"], BLOG + "/blog/top-bedroom-products")
        self.assertEqual(d["product_method"], "blog_theme")
        self.assertEqual(d["product_cta"], "")  # no kit CTA glued onto a blog pin

    def test_living_room_theme_to_living_room_post(self):
        d = route_product(themed_pin("Living room", title="Cream Living Room Ideas"))
        self.assertEqual(d["destination_url"], BLOG + "/blog/top-living-room-products")

    def test_vanity_theme_variant_to_vanity_post(self):
        d = route_product(themed_pin("Vanity / mirror corner", title="Mirror Corner Glow"))
        self.assertEqual(d["destination_url"], BLOG + "/blog/top-vanity-products")

    def test_gaming_theme_on_tech_account_to_desk_post(self):
        # Desk mapping beats the HomeDecor-only kit scope: gaming pins live
        # on account_2 and must still reach the cozy-desk post.
        d = route_product(themed_pin(
            "Desk: Gaming setup", title="Pink Gaming Setup Tour",
        ), account_key="account_2")
        self.assertEqual(d["destination_url"], BLOG + "/blog/cozy-desk-setup-finds")

    def test_desk_study_theme_to_desk_post(self):
        d = route_product(themed_pin("Desk: Cozy / study / workspace", title="Cozy Study Desk"))
        self.assertEqual(d["destination_url"], BLOG + "/blog/cozy-desk-setup-finds")

    def test_garden_theme_to_prompt_pack_despite_garden_kit_fit(self):
        # Owner, 2 Nov 2026: garden/scenery pins carry the prompt pack —
        # this still beats the Garden Kit the product rules would pick
        # (before 2 Nov these themes posted linkless).
        d = route_product(themed_pin(
            "Garden / cottage scenery",
            title="I Didn't Know A Morning Cottage Garden Could Look Like This",
            visual_prompt="cottage garden full of blooms, garden gate, morning light",
        ))
        self.assertEqual(d["product_id"], "prompt_pack")
        self.assertEqual(d["destination_url"], PACK_URL)
        self.assertEqual(d["product_method"], "prompt_pack")
        # Honest copy only: the pack has exactly 350 prompts, never 400+.
        self.assertIn("350", d["product_cta"])
        self.assertNotIn("400", d["product_cta"])

    def test_scenery_theme_to_prompt_pack(self):
        d = route_product(themed_pin(
            "Scenery (no purchasable decor)",
            title="Mountain Views Forever",
            visual_prompt="mountain valley landscape, scenic sky",
        ))
        self.assertEqual(d["product_id"], "prompt_pack")
        self.assertEqual(d["destination_url"], PACK_URL)
        self.assertEqual(d["product_method"], "prompt_pack")
        self.assertTrue(d["product_cta"])

    def test_unmapped_theme_falls_through_to_product_rules(self):
        # Kitchen has no blog post — the old behavior must be untouched:
        # a bedroom-flavoured pin still routes to the Decor Kit.
        d = route_product(themed_pin(
            "Kitchen",
            title="Blush Pastel Bedroom Styling Ideas",
            description="Cozy bedroom decor with soft glam styling and layered bedding",
            visual_prompt="pastel bedroom interior, cozy corner, warm lamp light",
        ))
        self.assertEqual(d["product_id"], "decor_kit")
        self.assertEqual(d["destination_url"], DECOR_URL)


def trend_pin(trend, title="A Pretty Pin", **kw):
    """A strategy dict stamped by trend_cmo: strategy["trend"], no theme."""
    strategy = pin(title, **kw)
    strategy["trend"] = trend
    return strategy


CHRISTMAS_POST = BLOG + "/blog/christmas-decor-finds"
NEW_YEAR_POST = BLOG + "/blog/new-year-home-reset-essentials"


class TestBlogTrendMapping(unittest.TestCase):
    """Trend pin → Blog mapping (owner, 1 Nov 2026): christmas links, rest unchanged."""

    def test_christmas_trend_to_christmas_post(self):
        d = route_product(trend_pin(
            "christmas",
            title="Magical Christmas Tree Corner With Twinkling Lights",
            description="christmas xmas decor with a glowing tree",
            visual_prompt="christmas tree with ornaments and fairy lights",
            board_name="Christmas",
        ))
        self.assertEqual(d["destination_url"], CHRISTMAS_POST)
        self.assertEqual(d["product_id"], "blog:christmas-decor-finds")
        self.assertEqual(d["product_method"], "blog_trend")
        self.assertEqual(d["product_confidence"], 1.0)
        self.assertEqual(d["product_cta"], "")  # no kit CTA glued onto a blog pin

    def test_christmas_trend_case_and_spacing_insensitive(self):
        d = route_product(trend_pin(" Christmas ", title="Twinkling Tree Corner"))
        self.assertEqual(d["destination_url"], CHRISTMAS_POST)
        self.assertEqual(d["product_method"], "blog_trend")

    def test_new_year_trend_to_new_year_post(self):
        # Trend key is the sheet's stamped name "New Year" (normalized to
        # "new year" by the router) → the New Year reset post (12 Nov 2026).
        d = route_product(trend_pin(
            "New Year",
            title="Fresh Organized Entryway For The New Year",
            description="new year home reset organized entryway baskets",
            visual_prompt="organized entryway with woven baskets and shelves",
            board_name="New Year",
        ))
        self.assertEqual(d["destination_url"], NEW_YEAR_POST)
        self.assertEqual(d["product_id"], "blog:new-year-home-reset-essentials")
        self.assertEqual(d["product_method"], "blog_trend")
        self.assertEqual(d["product_confidence"], 1.0)
        self.assertEqual(d["product_cta"], "")  # no kit CTA glued onto a blog pin

    def test_new_year_trend_case_and_spacing_insensitive(self):
        d = route_product(trend_pin("  NEW YEAR  ", title="Tidy Pantry Reset"))
        self.assertEqual(d["destination_url"], NEW_YEAR_POST)
        self.assertEqual(d["product_method"], "blog_trend")

    def test_halloween_trend_behaves_exactly_as_before(self):
        # Halloween has no trend mapping: its decision must be identical
        # to the same pin carrying no trend stamp at all (posts clean —
        # the traffic signals treat halloween as reach content).
        stamped = trend_pin(
            "halloween",
            title="Spooky Halloween Porch With Glowing Pumpkins",
            description="halloween decor with carved pumpkins and candles",
            visual_prompt="halloween porch, pumpkins and candles",
            board_name="Halloween",
        )
        plain = dict(stamped)
        del plain["trend"]
        self.assertEqual(route_product(stamped), route_product(plain))
        d = route_product(stamped)
        self.assertEqual(d["product_id"], "")
        self.assertEqual(d["destination_url"], "")
        self.assertNotEqual(d["product_method"], "blog_trend")

    def test_regular_themed_pin_unaffected_by_trend_mapping(self):
        d = route_product(themed_pin(
            "Bedroom",
            title="Blush Pastel Bedroom Styling Ideas",
            visual_prompt="pastel bedroom interior, cozy corner",
        ))
        self.assertEqual(d["destination_url"], BLOG + "/blog/top-bedroom-products")
        self.assertEqual(d["product_method"], "blog_theme")


if __name__ == "__main__":
    unittest.main()
