# reservations-bot

A personal dining secretary that lives in Telegram. You text it ("dinner tonight, 2 at 8, SoHo, lively") and it replies with a short, ranked shortlist — real Resy slots where available, OpenTable deep links where not.

Built on Claude (Anthropic SDK) with tool use. Agentic loop: parse the request → search Google Places → check Resy availability / generate OpenTable links → present options → log the search and any bookings.

## What it does

- **Search + rank.** Pulls candidate restaurants from Google Places for a given area/vibe, then checks Resy for real-time availability. Falls back to OpenTable deep links when Resy has nothing.
- **Personalizes.** Reads your ranked Beli list and prefs each turn; heavily weights spots you've rated well and respects dietary/time constraints.
- **Remembers.** Appends stable preferences to `prefs.md`, logs every search to `history.md`, and logs confirmed bookings to `bookings.md`.
- **Stays quick.** One Conversation per chat kept in memory, auto-reset on idle. System prompt + prefs are prompt-cached (5-min TTL) so multi-turn sessions are cheap. A Haiku-generated "Pulling SoHo options for 8pm…" ack fires only if the main response takes >3s.

## Setup

```bash
uv sync                          # or: pip install -e .
cp .env.example .env             # fill in all four keys + addresses
cp prefs.example.md prefs.md
cp beli.example.md beli.md
cp bookings.example.md bookings.md
cp history.example.md history.md
python bot.py
```

### Required env vars

| Var | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram |
| `TELEGRAM_ALLOWED_CHAT_IDS` | comma-separated; `/id` in chat prints yours. If unset, bot replies to anyone (dev only) |
| `GOOGLE_PLACES_API_KEY` | Google Cloud Console → Places API (New) |
| `HOME_ADDRESS`, `WORK_ADDRESS` | used for "near home" / "near work" queries |

Optional: `CLAUDE_MODEL` (default `claude-sonnet-4-6`), `ACK_MODEL` (default `claude-haiku-4-5`), `RESY_API_KEY` (defaults to Resy's public web key).

## Telegram commands

- any free-text message → reservation request
- `/start` → help
- `/reset` → clear conversation state for your chat
- `/id` → print your `chat_id` (for the allowlist)

## Layout

```
bot.py              Telegram entry point; one Conversation per chat_id
agent.py            Claude loop — message history, tool dispatch, idle reset
tools.py            All tools (read_prefs, search_restaurants, check_availability, log_booking, ...)
probe.py            Standalone Resy API probe
probe_ot.py         Standalone OpenTable probe

prefs.md            User preferences (gitignored — starts from prefs.example.md)
beli.md             Ranked restaurants (gitignored)
bookings.md         Confirmed bookings, append-only (gitignored)
history.md          Search log, append-only (gitignored)
```

## Notes

- State files (`prefs.md`, `beli.md`, `bookings.md`, `history.md`) are gitignored by design — they're per-user and the bot mutates them. Commit only the `*.example.md` templates.
- Home and work addresses live in `.env`, not `prefs.md`, so sharing your prefs doesn't leak your location.
- The Resy key in `tools.py` is Resy's public web API key (shipped to every browser on resy.com). Override via `RESY_API_KEY` if you have your own.
