"""
views.py — Shared reusable discord.ui.View components.
"""

import discord
from datetime import datetime, timezone


class LeaderboardView(discord.ui.View):
    """
    Paginated leaderboard view.
    pages: list of lists, each inner list has dicts with 'name' and 'total' keys.
    """

    def __init__(
        self,
        pages: list[list[dict]],
        title: str,
        color: discord.Color,
        footer: str,
        medals: list[str] | None = None,
    ):
        super().__init__(timeout=120)
        self.pages   = pages
        self.title   = title
        self.color   = color
        self.footer  = footer
        self.medals  = medals or (["🥇", "🥈", "🥉"] + ["🏅"] * 47)
        self.page    = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= len(self.pages) - 1

    def make_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.title,
            color=self.color,
            timestamp=datetime.now(timezone.utc),
        )
        page_data  = self.pages[self.page]
        start_rank = self.page * 10
        lines = []
        for i, entry in enumerate(page_data):
            global_rank = start_rank + i
            medal = self.medals[global_rank] if global_rank < len(self.medals) else f"`#{global_rank + 1}`"
            lines.append(f"{medal} **{entry['name']}** — {entry['total']}")
        embed.description = "\n".join(lines) if lines else "No entries yet."
        total_pages = max(1, len(self.pages))
        embed.set_footer(text=f"Page {self.page + 1}/{total_pages} • {self.footer}")
        return embed

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(len(self.pages) - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


def chunk_leaderboard(rows, name_key: str, total_key: str = "total", page_size: int = 10) -> list[list[dict]]:
    """Convert DB rows into pages of {name, total} dicts."""
    entries = [{"name": row[name_key], "total": row[total_key]} for row in rows]
    return [entries[i:i + page_size] for i in range(0, max(1, len(entries)), page_size)] if entries else [[]]
