"""Render the CLI demo assets used in the README.

The transcript comes from the deterministic analysis output. It is rendered with
Pillow so the repository does not depend on a terminal recorder. Regenerate with:

    uv run python scripts/render_cli_demo.py
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ops_pilot.analysis import analyze_incident
from ops_pilot.data import DataStore

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "assets"

WIDTH, HEIGHT = 1120, 760
PAD = 26
TITLE_H = 42
TEXT_X = 42
TEXT_Y = 82
FONT_SIZE = 19
LINE_H = 27
MAX_LINES = (HEIGHT - TEXT_Y - 26) // LINE_H

BG = "#0d1117"
WINDOW = "#0f141b"
TITLE_BG = "#161b22"
TEXT = "#c9d1d9"
DIM = "#8b949e"
GREEN = "#7ee787"
CYAN = "#79c0ff"
BLUE = "#58a6ff"
YELLOW = "#e3b341"
OK = "#3fb950"
RED = "#ff7b72"
WHITE = "#f0f6fc"

FONT_CANDIDATES = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]


def _font() -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, FONT_SIZE)
    return ImageFont.load_default()


@dataclass(frozen=True)
class Line:
    text: str
    color: str = TEXT
    bold: bool = False


def _fmt(value: float, unit: str) -> str:
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"


def transcript() -> list[list[Line]]:
    store = DataStore.load()
    bad = analyze_incident(store, "INC-1001")
    unknown = analyze_incident(store, "INC-1005")

    latency = next(item for item in bad["metrics"] if item["metric"] == "latency_p95_ms")
    errors = next(item for item in bad["metrics"] if item["metric"] == "error_rate_pct")
    action = bad["recommended_action"]

    screen_one = [
        Line("$ uv run ops-pilot", GREEN),
        Line("OpsPilot — approval-gated incident commander", WHITE, True),
        Line("Thread: cli-demo", DIM),
        Line("Commands: /incidents, /audit, /new, /quit", DIM),
        Line(""),
        Line("  INC-1001  SEV-1 checkout-api  Checkout p95 latency spike", TEXT),
        Line("  INC-1002  SEV-1 payments-db   Payments DB connection pool exhaustion", TEXT),
        Line("  INC-1003  SEV-1 pricing-api   Pricing API 5xx spike", TEXT),
        Line("  INC-1004  SEV-2 search-api    Search disk pressure", TEXT),
        Line("  INC-1005  SEV-3 auth-api      Auth latency complaint", TEXT),
        Line(""),
        Line("You: Investigate INC-1001", CYAN),
        Line(
            "OpsPilot: Deploy CHG-2001 (web@2026.11.14-3) landed 10 min before the spike.",
            WHITE,
        ),
        Line(
            f"  latency_p95_ms : baseline {_fmt(latency['baseline_median'], 'ms')}"
            f" -> peak {_fmt(latency['peak'], 'ms')}  ({latency['ratio']:.2f}x)",
            DIM,
        ),
        Line(
            f"  error_rate_pct : baseline {_fmt(errors['baseline_median'], '%')}"
            f" -> peak {_fmt(errors['peak'], '%')}  ({errors['ratio']:.2f}x)",
            DIM,
        ),
        Line("Hypothesis: Bad deploy  (high confidence, score 0.95)", YELLOW),
        Line(
            f"Draft RMD-0001: {action['action']} -> {action['target']}",
            BLUE,
        ),
    ]

    screen_two = [
        Line("You: apply", CYAN),
        Line("HUMAN APPROVAL REQUIRED", YELLOW, True),
        Line(
            f"  apply_remediation: {action['action']} -> {action['target']}", TEXT
        ),
        Line("  incident: INC-1001   draft: RMD-0001", DIM),
        Line(
            "  reason: Deploy CHG-2001 preceded the latency/error spike.", DIM
        ),
        Line(
            "  evidence: CHG-2001, L-1001, metric:checkout-api:latency_p95_ms", DIM
        ),
        Line(""),
        Line("Approve? [y/N] y", GREEN),
        Line("OpsPilot: Applied ACT-0001. The sandbox ledger now has one action.", OK),
        Line(
            '  rollback_remediation(action_id="ACT-0001") is available.', DIM
        ),
        Line(""),
        Line("You: /audit", CYAN),
        Line("  EVT-0001  propose  RMD-0001  actor=agent", TEXT),
        Line("  EVT-0002  apply    ACT-0001  actor=human", OK),
        Line(""),
        Line("  ledger: sandbox/ledger.json  (1 action)", DIM),
    ]

    screen_three = [
        Line("You: Investigate INC-1005", CYAN),
        Line(
            "OpsPilot: I checked 2 metric series and found no anomaly.", WHITE
        ),
        Line(
            "  No recent change explains the complaint, and the logs are healthy.", DIM
        ),
        Line("Hypothesis: Unknown  (low confidence, score 0.10)", YELLOW),
        Line("needs_human_input = true", RED),
        Line(""),
        Line("I don't have enough evidence to propose a change.", TEXT),
        Line(
            "Should I widen the time window, inspect a dependency, or look at traces?",
            TEXT,
        ),
    ]

    for screen in (screen_one, screen_two, screen_three):
        if len(screen) > MAX_LINES:
            raise SystemExit(f"Transcript screen too tall: {len(screen)} lines")
    return [screen_one, screen_two, screen_three]


def _base_window() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (PAD, PAD, WIDTH - PAD, HEIGHT - PAD), radius=14, fill=WINDOW
    )
    draw.rounded_rectangle(
        (PAD, PAD, WIDTH - PAD, PAD + TITLE_H),
        radius=14,
        fill=TITLE_BG,
    )
    draw.rectangle(
        (PAD, PAD + TITLE_H - 8, WIDTH - PAD, PAD + TITLE_H), fill=TITLE_BG
    )
    for index, color in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        x = PAD + 20 + index * 22
        draw.ellipse((x, PAD + 15, x + 11, PAD + 26), fill=color)
    font = _font()
    draw.text(
        (PAD + 92, PAD + 11),
        "ops-pilot — approval-gated incident commander",
        font=font,
        fill=DIM,
    )
    draw.text(
        (WIDTH - PAD - 150, PAD + 11),
        "sandbox only",
        font=font,
        fill=DIM,
    )
    return image


def _render(screen: list[Line], revealed: int, cursor: bool) -> Image.Image:
    image = _base_window()
    draw = ImageDraw.Draw(image)
    font = _font()
    y = TEXT_Y
    for index, line in enumerate(screen[:revealed]):
        draw.text(
            (TEXT_X, y),
            line.text,
            font=font,
            fill=line.color,
            stroke_width=1 if line.bold else 0,
            stroke_fill=line.color,
        )
        y += LINE_H
    if cursor and revealed:
        last = screen[revealed - 1].text
        x = TEXT_X + draw.textlength(last, font=font) + 4
        draw.rectangle((x, y - LINE_H + 3, x + 10, y - 5), fill=TEXT)
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    screens = transcript()

    frames: list[Image.Image] = []
    durations: list[int] = []
    for screen in screens:
        for revealed in range(1, len(screen) + 1):
            frames.append(_render(screen, revealed, cursor=True))
            durations.append(140)
        # Hold the completed screen, with a blinking cursor.
        frames.append(_render(screen, len(screen), cursor=False))
        durations.append(500)
        frames.append(_render(screen, len(screen), cursor=True))
        durations.append(1900)

    gif = OUT / "cli-demo.gif"
    frames[0].save(
        gif,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    _render(screens[1], len(screens[1]), cursor=False).save(
        OUT / "cli-approval.png", optimize=True
    )
    print(f"wrote {gif} ({gif.stat().st_size / 1024:.0f} KB)")
    print(f"wrote {OUT / 'cli-approval.png'}")


if __name__ == "__main__":
    main()
