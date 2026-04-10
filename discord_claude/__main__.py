"""Entry point: python -m discord_claude"""

from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    if not settings.bot_token:
        raise SystemExit("DISCORD_BOT_TOKEN not set — copy .env.example to .env")

    # Deferred import so config loads first
    from .bot import run_bot

    run_bot(settings)


if __name__ == "__main__":
    main()
