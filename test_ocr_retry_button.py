"""Smoke test for the «🔄 Другой моделью» retry mechanism.

Exercises the pure helpers that build the retry-button row and the
short-token mapping without needing a live Telegram bot.
"""
import os
import sys

# Force tesseract path so importing ocr.py doesn't try to hit OpenRouter.
os.environ["OCR_PROVIDER"] = "tesseract"
# Bot needs SOMETHING in BOT_TOKEN to import.
os.environ.setdefault("BOT_TOKEN", "fake")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    import bot
    from ocr import AI_FALLBACK_MODELS

    # ── _retry_token_for: deterministic, fits in callback_data ──────────
    t1 = bot._retry_token_for("AgADSomeFileId123")
    t2 = bot._retry_token_for("AgADSomeFileId123")
    t3 = bot._retry_token_for("DIFFERENT_ID")
    assert t1 == t2, "token must be deterministic"
    assert t1 != t3, "different file_ids → different tokens"
    assert len(t1) == 16, f"token must be 16 chars, got {len(t1)}"
    assert all(c in "0123456789abcdef" for c in t1), "token must be hex"

    # ── callback_data length: must fit in Telegram's 64-byte cap ────────
    cb = f"retryocr:{t1}"
    assert len(cb.encode("utf-8")) <= 64, f"callback_data too long: {cb!r}"

    # ── _retry_button_row: stashes state and returns a one-button row ───
    class FakeCtx:
        def __init__(self):
            self.user_data: dict = {}

    ctx = FakeCtx()
    row = bot._retry_button_row(
        ctx,
        file_id="AgADSomeFileId123",
        tried_models=[],
        target_tournament=None,
        reporter_id=42,
        caption="",
        panel_kind="own",
    )
    assert len(row) == 1, "expected one button"
    btn = row[0]
    assert "Другой моделью" in btn.text
    assert btn.text.endswith(f"(1/{len(AI_FALLBACK_MODELS)})"), \
        f"counter wrong: {btn.text!r}"
    assert btn.callback_data == f"retryocr:{t1}"

    # State stashed
    state_key = f"ocr_retry_{t1}"
    assert state_key in ctx.user_data
    state = ctx.user_data[state_key]
    assert state["file_id"] == "AgADSomeFileId123"
    assert state["tried"] == []
    assert state["reporter_id"] == 42
    assert state["panel_kind"] == "own"

    # ── After all models tried → button row is empty ────────────────────
    row_empty = bot._retry_button_row(
        ctx,
        file_id="x",
        tried_models=list(AI_FALLBACK_MODELS),
        target_tournament=None,
        reporter_id=1,
        caption="",
        panel_kind="own",
    )
    assert row_empty == [], "no untried models → no button"

    # ── Counter advances correctly ──────────────────────────────────────
    row_mid = bot._retry_button_row(
        ctx,
        file_id="y",
        tried_models=[AI_FALLBACK_MODELS[0]],
        target_tournament=None,
        reporter_id=1,
        caption="",
        panel_kind="own",
    )
    assert len(row_mid) == 1
    assert row_mid[0].text.endswith(f"(2/{len(AI_FALLBACK_MODELS)})"), \
        row_mid[0].text

    print("All retry-button smoke tests passed.")


if __name__ == "__main__":
    main()
