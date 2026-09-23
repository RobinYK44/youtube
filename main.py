import logging
import sys

from shortsbot.config import config


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    missing = config.missing()
    if missing:
        sys.exit("Deze instellingen ontbreken in .env: " + ", ".join(missing))
    if not config.youtube_token_file.exists():
        logging.warning("Nog geen YouTube-login. Draai eerst: python -m shortsbot.youtube auth")

    from shortsbot.bot import ShortsBot

    ShortsBot().run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
