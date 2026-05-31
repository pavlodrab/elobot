"""Tesseract-fallback OCR smoke test.

Runs against the bundled fixture screenshot to verify:
  - score parses correctly (no clock leakage like "1:17")
  - league plate is detected via fuzzy keyword (gвардиол / VSA)
  - goals panel is parsed: name + minute + colour-keyed side

Skips silently if pytesseract / Pillow / the fixture is missing — keeps
CI green on machines without the OCR stack.
"""
import os
import sys

# Force tesseract path (no AI calls).
os.environ["OCR_PROVIDER"] = "tesseract"
# Ensure we run from the repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FIXTURE = "tests_fixtures/horizontal_match.jpg"

def main():
    try:
        from PIL import Image  # noqa
    except ImportError:
        print("[skip] Pillow not installed.")
        return
    try:
        import pytesseract  # noqa
        pytesseract.get_tesseract_version()
    except Exception:
        print("[skip] tesseract binary not available.")
        return
    if not os.path.exists(FIXTURE):
        print(f"[skip] fixture {FIXTURE} missing.")
        return

    from ocr import (
        parse_match_screenshot,
        _is_match_score_pair,
        _parse_score,
        _clean_scorer_name,
        _parse_minute,
        _is_green_ball,
        _is_blue_ball,
        _pick_league_plate,
        _ai_extract_json_block,
        _ai_loose_json_loads,
        _ai_parse_response,
    )

    # AI-response tolerance helpers — exercise the slop that vision-LLMs
    # actually emit (markdown fences, trailing commas, prose around JSON,
    # single-quoted strings, partially valid output).
    assert _ai_extract_json_block('Here it is: {"a":1, "b":[1,2,3]} bye') == \
        '{"a":1, "b":[1,2,3]}'
    assert _ai_extract_json_block('no json at all') is None
    assert _ai_loose_json_loads('{"a":1,}') == {"a": 1}
    assert _ai_loose_json_loads("{'a':1}") == {"a": 1}
    assert _ai_loose_json_loads("not json") is None
    fenced = """```json
    {"score1":2,"score2":1,"team1":"A","team2":"B","league":"VSA","goals":[]}
    ```"""
    parsed = _ai_parse_response(fenced)
    assert parsed["score1"] == 2 and parsed["score2"] == 1, parsed
    # Prose + trailing comma — both bandages must apply.
    sloppy = ('Sure! Here you go:\n'
              '{"score1":1,"score2":0,"team1":"X",'
              '"team2":"Y","league":"RI","goals":[],}'
              '\nLet me know if you need more.')
    parsed2 = _ai_parse_response(sloppy)
    assert parsed2["score1"] == 1 and parsed2["league_plate"] == "RI", parsed2

    # Pure helpers — fast, deterministic.
    assert _is_match_score_pair(1, 1, ":") is True
    assert _is_match_score_pair(1, 17, ":") is False, "1:17 must not be a score"
    assert _is_match_score_pair(0, 30, ":") is False, "0:30 must not be a score"
    assert _is_match_score_pair(90, 0, ":") is False
    assert _is_match_score_pair(3, 2, "-") is True
    assert _parse_score("1 - 1") == (1, 1)
    assert _parse_score("01:17 1 - 1") == (1, 1), "clock-leakage handled"
    assert _parse_score("90:00") is None

    # Stoppage / glitched-icon names.
    assert _clean_scorer_name("Brahim ros (we)") == "Brahim"
    assert _clean_scorer_name("Rafael Leao . cic |") == "Rafael Leao"
    assert _clean_scorer_name("Brahim Cie") == "Brahim", "trim 3-char trailing"
    assert _clean_scorer_name("ron oF") is None, "garbage rejected"

    assert _parse_minute("81'") == 81
    assert _parse_minute("90'+1") == 91
    assert _parse_minute("SO+1") == 91, "S→9 substitution"

    # Colours.
    assert _is_green_ball((0, 200, 100))
    assert not _is_green_ball((255, 255, 255))
    assert _is_blue_ball((30, 130, 220))
    assert not _is_blue_ball((100, 100, 100))

    # League fuzzy picker — picks the candidate matching a known league
    # hint (лиг/гварди/vsa/ri), and rejects garbage when no candidate
    # has a hint. Previously fell back to "longest non-empty", which
    # leaked banner text like ``OY |e @ СДНЁМ ПОБЕДЫ`` and ``| у | НЕТЛИГИ``
    # into the league field.
    assert _pick_league_plate("Пена Грандекначи", "Лига Гвардиолыча") == \
        "Лига Гвардиолыча"
    assert _pick_league_plate("", "garbage stuff") is None
    assert _pick_league_plate("OY |e @ СДНЁМ ПОБЕДЫ", "") is None
    assert _pick_league_plate("| у | НЕТЛИГИ", "") is None
    assert _pick_league_plate("VSA premier", "") == "VSA premier"

    # End-to-end on the fixture screenshot.
    res = parse_match_screenshot(FIXTURE)
    assert res.score == "1:1", f"expected 1:1 got {res.score}"
    assert res.tournament_type == "vsa", f"got {res.tournament_type}"
    assert "гвардиол" in (res.league_plate or "").lower(), \
        f"league not detected: {res.league_plate!r}"
    assert len(res.goals) == 2, f"expected 2 goals, got {len(res.goals)}"
    home_goals = [g for g in res.goals if g["side"] == "home"]
    away_goals = [g for g in res.goals if g["side"] == "away"]
    assert len(home_goals) == 1 and len(away_goals) == 1
    assert "Brahim" in (home_goals[0]["name"] or "")
    assert "Rafael" in (away_goals[0]["name"] or "")
    assert home_goals[0]["minute"] == 81
    assert away_goals[0]["minute"] == 91

    # ── _ai_post_process safety net ─────────────────────────────────────
    # When a vision model dumps the small-font league line into team1 or
    # team2, _ai_post_process should lift it into ``league`` and clear
    # the bogus nickname so downstream fuzzy-matching doesn't pretend
    # to find a registered player named "Локомотив Амстердам".
    from ocr import _ai_post_process, _looks_like_league
    assert _looks_like_league("Локомотив Амстердам")
    assert _looks_like_league("Лига Гвардиолыча")
    assert _looks_like_league("УДП Украина")
    assert _looks_like_league("VSA premier")
    assert _looks_like_league("НЕТ ЛИГИ")
    assert not _looks_like_league("OliverBax")
    assert not _looks_like_league("РД_Aleksfifa")
    assert not _looks_like_league("YUPII")
    assert not _looks_like_league("")
    assert not _looks_like_league(None)

    case = {
        "team1": "РД_Aleksfifa", "team2": "Локомотив Амстердам",
        "league": None,
    }
    _ai_post_process(case)
    assert case["team1"] == "РД_Aleksfifa"
    assert case["team2"] == ""           # bogus nick wiped
    assert case["league"] == "Локомотив Амстердам"

    case2 = {"team1": "OliverBax", "team2": "boze", "league": "Лига X"}
    _ai_post_process(case2)
    assert case2 == {"team1": "OliverBax", "team2": "boze", "league": "Лига X"}, \
        "valid responses must pass through untouched"

    print("All OCR-tesseract smoke tests passed.")

if __name__ == "__main__":
    main()
