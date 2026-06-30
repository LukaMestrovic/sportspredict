# syntax=docker/dockerfile:1
#
# Immutable image for the sportspredict-llm autonomous submitter. The CURRENT
# source is baked in, so once deployed the running bot is a frozen snapshot:
# editing the working tree never affects a live cron tick until the next
# scripts/deploy.sh rebuild. Secrets are NEVER baked — the API keys are passed
# at `docker run` via -e (see scripts/run.sh). Build context = repo root.
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Keep the bot and numerical simulator dependency sets explicit and separate.
COPY requirements.txt /app/
COPY simulator/requirements.txt /app/simulator/requirements.txt
RUN pip install -r requirements.txt -r simulator/requirements.txt

# Bake the tracked simulator source, config, and fitted artifacts.
COPY simulator/ /app/simulator/

# Bake the source. No .env, no secrets (enforced by .dockerignore). cache/ and
# logs/ are intentionally absent — they are bind-mounted at run time so state
# (paid odds cache, parser cache, cron markers, ledger) persists across ticks.
COPY bot/ /app/bot/
COPY scripts/ /app/scripts/
COPY prompts/ /app/prompts/
COPY run.py validate.py /app/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# One cron tick of the dispatcher; args (--status, --dry-run) pass through.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
