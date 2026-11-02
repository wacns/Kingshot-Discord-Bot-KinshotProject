# :crown: Kingshot Discord Bot

Kingshot Discord Bot that supports alliance management, event reminders and attendance tracking, gift code redemption, minister appointment planning and more. This bot is free, open source and self-hosted. It is specifically designed for [Kingshot](https://www.centurygames.com/games/kingshot/).

## 🚀 Getting Started

To get started with the bot, head over to the [wiki](https://github.com/kingshot-project/Kingshot-Discord-Bot/wiki) for instructions and other information.

If you have any issues with the bot, head over to the [common issues](https://github.com/kingshot-project/Kingshot-Discord-Bot/wiki/Getting-Help) page or join our [discord server](https://discord.gg/apYByj6K2m) for support.

## 🛠️ Working on the bot

Set up a development environment once (Python 3.11 or newer):

```
python -m venv bot_venv
bot_venv\Scripts\activate            # Windows  (Linux/macOS: source bot_venv/bin/activate)
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Before pushing, run the code checker and the tests:

```
python -m ruff check .
python -m pytest tests
python -m pytest tests/sim --sim
```

The second command runs the Discord menu tests. Keep it a separate run: mixed into the main run they leave shared state behind and other tests fail. The two screenshot-OCR suites (`test_layer2_ocr.py`, `test_attendance_ocr_layer2.py`) are slow; skip them with `--ignore` while iterating. See [tests/README.md](tests/README.md) for details.

GitHub runs the same checks on every push and pull request to `main`, plus a full run including the slow suites every Monday.
