from difflib import SequenceMatcher
import ocr

def test_strip_badge_keeps_gamertag_digits():
    assert ocr._strip_badge_number("2.OS777") == "2.OS777"
    assert ocr._strip_badge_number("AuraBroAura88888") == "AuraBroAura88888"
    assert ocr._strip_badge_number("Zardes-27") == "Zardes-27"

def test_strip_badge_removes_separated_level():
    assert ocr._strip_badge_number("Kaef 100") == "Kaef"
    assert ocr._strip_badge_number("100 Kaef") == "Kaef"

def test_canonical_nick():
    assert ocr.canonical_nick("2.0S77") == "20s77"
    assert ocr.canonical_nick("2.OS777") == "20s777"
    assert SequenceMatcher(None, ocr.canonical_nick("2.0S77"), ocr.canonical_nick("2.OS777")).ratio() >= 0.55

def test_score_reconciled_from_goals():
    p = {"score1": 9, "score2": 0, "goals": [{"side": "home"}] * 5}
    ocr._ai_post_process(p)
    assert (p["score1"], p["score2"]) == (5, 0)
