#!/usr/bin/env python3
"""Export SWE-bench leaderboard columns to CSV.

Exports the columns requested by the user:
- model
- % resolved
- avg. cost
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://www.swebench.com/"


def scrape_rows() -> list[dict[str, str]]:
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=120000)
        page.wait_for_timeout(2500)
        rows = page.evaluate(
            """() => {
                const table = document.querySelector('table');
                if (!table) return [];
                const parsed = Array.from(table.querySelectorAll('tbody tr'))
                  .map(tr => Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim().replace(/\s+/g, ' ')))
                  .filter(r => r.length >= 4 && r[1] && !r[1].startsWith('No entries match'))
                  .map(r => ({ model: r[1], resolved: r[2], avg_cost: r[3] }));
                return parsed;
            }"""
        )
        browser.close()
        return rows


def write_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["model", "% resolved", "avg. cost"])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "model": row.get("model", ""),
                    "% resolved": row.get("resolved", ""),
                    "avg. cost": row.get("avg_cost", ""),
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        default="data/swebench_model_resolved_cost.csv",
        help="Output CSV path",
    )
    args = parser.parse_args()

    rows = scrape_rows()
    write_csv(rows, Path(args.output))
    print(f"Wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
