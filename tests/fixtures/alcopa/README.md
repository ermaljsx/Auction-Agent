# Alcopa fixtures

**These pages are synthetic.** They follow the card and detail markup that third-party scrapers quoted from captured Alcopa pages (Sept 2026, medium confidence). They are not copies of real pages and contain no real VIN or plate.

Use `python -m auction_agent capture --url <sale> --detail <lot>` to save real pages (VIN and plate are redacted) into `real/`. Then re-run the tests. Before committing anything from `real/`, review it by hand: it is git-ignored by default.

`detail_layout_2026.html` and `sale_card_multiline.html` follow the markup of real pages captured on 2026-11-07 (bare-text comment, damage list in a `data-source` attribute, `h4#warranty`, multi-line card prices). Every value in them is invented: no real VIN, plate, photo, product or staff id.
