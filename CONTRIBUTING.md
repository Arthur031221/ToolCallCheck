# Contributing

Start with a small issue or a focused change. Please include a regression test for behavior changes and run `python -m unittest discover -s tests` before opening a pull request.

For endpoint behavior reports, include the sanitized report when it is safe to share. Check the HTML and trace files first because redaction is best effort.

## Rebuilding the media

Install the optional media tools with `python -m pip install --editable '.[media]'`, install Chromium with `python -m playwright install chromium`, then run `python scripts/render_media.py`. The script starts both synthetic endpoint runs, captures the generated HTML reports in Chromium, and writes the GIF and social card.
