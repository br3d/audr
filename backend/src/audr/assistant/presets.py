"""Ready-made assistant prompts (AUD-961). Plain data, versioned with the code.

Each preset picks the context mode, supplies the user turn, and appends a short
instruction block to the system prompt (layout, word limit, per-preset intent).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PresetMode = Literal["portfolio", "news", "asset"]

_SHARED_RULES = (
    "\n\n=== TASK RULES ===\n"
    "Layout: a 1-2 sentence summary, then bullet points, each with its reasoning.\n"
    "Every claim must name the headline or number it comes from. A null/unknown value is "
    'called "unknown", never 0.\n'
    'No buy/sell instructions ("worth a look, because..." is fine).\n'
    "Answer in the language of the user's last message.\n"
)


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    description: str
    mode: PresetMode
    user_prompt: str
    instructions: str
    needs_asset: bool = False
    # Reasoning models spend the output budget thinking over a wide context; presets with a
    # 30-headline/20-alert context ask for more room than the default.
    min_output_tokens: int | None = None
    # news_impact: drop the generic headline/alert blocks and attach a trimmed headline list.
    trim_news_context: bool = False
    # biggest_movers: append the per-asset 24h/7d block and widen the headline list.
    movers_context: bool = False

    @property
    def system_suffix(self) -> str:
        return _SHARED_RULES + self.instructions + "\n"


PRESETS: tuple[Preset, ...] = (
    Preset(
        id="daily_brief",
        label="Daily brief",
        description="24h change, the 3 headlines that matter most to your holdings, unread alerts.",
        mode="portfolio",
        user_prompt="Give me my daily brief.",
        instructions=(
            "Task: daily brief. Maximum 200 words. Cover: the 24h portfolio change; the 3 "
            "headlines most relevant to the holdings (quote each); the unread alerts."
        ),
    ),
    Preset(
        id="concentration_risk",
        label="Concentration risk",
        description="Share of the largest asset, top 3 and stablecoins; holdings without a price.",
        mode="portfolio",
        user_prompt="How concentrated is my portfolio?",
        instructions=(
            "Task: concentration risk. Maximum 350 words. Give the share held in the largest "
            "asset, the top 3 combined, and stablecoins. List holdings with an unknown price "
            "(their share cannot be counted). End with plain flags (e.g. 'one asset above 50%')."
        ),
    ),
    Preset(
        id="news_impact",
        label="News impact",
        description="Which headlines matter for your holdings, and in which direction.",
        mode="news",
        user_prompt="What in the latest news matters for my holdings?",
        instructions=(
            "Task: news impact. Maximum 350 words. For each full headline (importance >= 60) "
            "give: affected holding, direction (up / down / neutral / unclear), confidence "
            "(low / medium / high), a one-line reason. Then list the titles under "
            '"Ignored headlines" without discussing them.'
        ),
        trim_news_context=True,
    ),
    Preset(
        id="alert_triage",
        label="Alert triage",
        description="Sort unread alerts into act now / watch / noise.",
        mode="news",
        user_prompt="Triage my unread alerts.",
        instructions=(
            "Task: alert triage. Maximum 350 words. Sort every unread alert into "
            "'Act now', 'Watch' or 'Noise', each with a one-line reason. If there are no "
            "unread alerts, say so."
        ),
        min_output_tokens=8000,
    ),
    Preset(
        id="biggest_movers",
        label="Biggest movers",
        description="Top gainers and losers by USD impact over 24h/7d, each with its news.",
        mode="portfolio",
        user_prompt="What moved the most in my portfolio?",
        instructions=(
            "Task: biggest movers. Maximum 300 words. Use the 'Per-asset movers' block, "
            "already sorted by 24h USD impact. List the top 3 gainers and top 3 losers by USD "
            "impact (not by %), each with its 24h and 7d % and USD change. Tie each to a matching "
            "headline from the context (quote it) or write 'no news found'. Assets whose change "
            "is unknown are listed separately as unknown."
        ),
        movers_context=True,
    ),
    Preset(
        id="asset_deep_dive",
        label="Asset deep dive",
        description="One asset's trend, news and alerts, and what is unknown about it.",
        mode="asset",
        user_prompt="Give me a deep dive on this asset.",
        instructions=(
            "Task: asset deep dive on the single asset in the context. Maximum 350 words. "
            "Cover its 30-day trend, its news, its alerts, and a final bullet list of "
            "what is unknown (missing price, empty series, no news)."
        ),
        needs_asset=True,
    ),
)

PRESETS_BY_ID: dict[str, Preset] = {p.id: p for p in PRESETS}
