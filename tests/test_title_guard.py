"""
tests/test_title_guard.py — CMO title humanizer / family-guard tests.

Owner complaint (2 Nov 2026): pin titles robot jaise lagne lage the —
LLM do hi frames ghis raha tha: "The kind of X that ..." aur "I didn't
know a X could feel this magical". Exact-repeat guard unhe pakad nahi
sakta tha (har baar noun badal jata hai), is liye ab title ki FAMILY bhi
history se match hoti hai.

Ye tests sirf guard/human-title functions chalate hain — koi LLM call,
koi API key nahi. node_cmo ke heavy imports (config / sheets / boards)
stub kar diye gaye hain taaki bare Python par bhi chalein:

    python -m unittest tests.test_title_guard
    # ya plain: python tests/test_title_guard.py
"""
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _stub_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


# ── node_cmo ke import-time deps stub karo (asli files untouched) ────────────
_stub_module(
    "config",
    CEREBRAS_API_KEY="", CEREBRAS_CMO_MODEL="",
    GEMINI_API_KEY="", GEMINI_CMO_MODEL="",
)
_mastermind = _stub_module("mastermind")
_mastermind.__path__ = [os.path.join(ROOT, "mastermind")]
_stub_module("mastermind.state", MastermindState=dict)
_stub_module(
    "sheets",
    get_prompts_master=lambda *a, **k: [],
    load_style_tracker=lambda *a, **k: {},
    save_style_tracker=lambda *a, **k: None,
    load_prompt_tracker=lambda *a, **k: {},
    save_prompt_tracker=lambda *a, **k: None,
)
_tools = _stub_module("tools")
_tools.__path__ = [os.path.join(ROOT, "tools")]
_stub_module(
    "tools.firebase_boards",
    format_boards_for_prompt=lambda *a, **k: "",
    format_trends_for_prompt=lambda *a, **k: "",
)

from mastermind import node_cmo as n  # noqa: E402

ROW = {"label": "Enchanted Spring Cottage",
       "tags": "CherryBlossoms, CottageGarden",
       "niche_affinity": "garden"}


class TitleGuardCase(unittest.TestCase):
    """Har test ki apni khaali title-history file."""

    def setUp(self):
        fd, self._hist_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self._hist_path)          # load -> {} (file missing = empty)
        self._old_path = n._TITLE_HISTORY_PATH
        n._TITLE_HISTORY_PATH = self._hist_path

    def tearDown(self):
        n._TITLE_HISTORY_PATH = self._old_path
        if os.path.exists(self._hist_path):
            os.remove(self._hist_path)

    def guard(self, title, account="account_1"):
        result = {"title": title, "visual_style": "mint_cottage_garden"}
        return n._ensure_unique_title(account, dict(result), sheet_row=dict(ROW))["title"]


class TestTitleFamily(TitleGuardCase):
    def test_family_classification(self):
        self.assertEqual(n._title_family("The kind of cozy desk that makes you exhale"), "kind_of")
        self.assertEqual(n._title_family("I didn't know a garden could feel this magical"), "didnt_know")
        self.assertEqual(n._title_family("A corner that makes evenings feel this gentle"), "feel_this")
        self.assertEqual(n._title_family("Rain against the glass and nowhere else to be"), "")
        self.assertEqual(n._title_family("Sunlight pooling on a bed that is hard to leave"), "")


class TestRutGuard(TitleGuardCase):
    def test_kind_of_blocked_when_family_recent(self):
        n._remember_title("account_1", "The kind of sunlit cottage that softens your heart")
        # account_1 ki history bhari hai -> usi account par rut title badlega
        out = self.guard("The kind of golden bedroom you never want to leave")
        self.assertNotEqual(out, "The kind of golden bedroom you never want to leave")
        self.assertNotEqual(n._title_family(out), "kind_of")

    def test_didnt_know_blocked_when_family_recent(self):
        n._remember_title("account_1", "I didn't know a vanity could feel this magical")
        out = self.guard("I didn't know a bedroom could feel this magical")
        self.assertNotEqual(n._title_family(out), "didnt_know")
        self.assertNotIn("feel this magical", out.lower())

    def test_first_rut_title_allowed_when_history_clean(self):
        # Family kabhi-kabhi chal sakti hai — pehli baar par rok nahi.
        t = "The kind of little balcony that makes you slow down"
        self.assertEqual(self.guard(t), t)

    def test_fresh_varied_title_untouched(self):
        t = "Rain against the glass and nowhere else to be"
        self.assertEqual(self.guard(t), t)

    def test_exact_repeat_still_replaced(self):
        t = "Sunlight pooling on a bed that is hard to leave"
        n._remember_title("account_1", t)
        self.assertNotEqual(self.guard(t), t)

    def test_family_window_is_per_account(self):
        n._remember_title("account_1", "The kind of sunlit cottage that softens your heart")
        t = "The kind of cozy desk that makes you exhale"
        # account_2 ki history saaf hai -> uske liye ye title chalega
        self.assertEqual(self.guard(t, account="account_2"), t)


class TestHumanTitle(TitleGuardCase):
    def test_no_kind_of_template_for_any_seed(self):
        for seed in range(40):
            out = n._human_title("Enchanted Spring Cottage", ["CherryBlossoms"], "garden", seed=seed)
            self.assertNotEqual(n._title_family(out), "kind_of", f"seed={seed}: {out}")

    def test_variety_across_seeds(self):
        outs = {n._human_title("Cozy Bedroom", [], "bedroom", seed=s) for s in range(9)}
        self.assertGreaterEqual(len(outs), 6)

    def test_avoid_set_is_respected(self):
        for seed in range(20):
            out = n._human_title("Gaming Desk", [], "tech", seed=seed, avoid={"feel_this"})
            self.assertNotEqual(n._title_family(out), "feel_this")


if __name__ == "__main__":
    unittest.main(verbosity=2)
