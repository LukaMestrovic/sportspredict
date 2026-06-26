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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SPORTSPREDICT_HYBRID_ROOT=/sportspredict-hybrid \
    SPORTSPREDICT_HYBRID_PYTHON=/usr/local/bin/python

WORKDIR /app

# The LLM bot itself only needs requests; the optional hybrid simulator bridge
# installs its heavier stack from the deployment snapshot below.
COPY requirements.txt /app/
RUN pip install -r requirements.txt

# Bake a deployment-time snapshot of the sibling hybrid simulator. The deploy
# script creates .deploy/hybrid_snapshot from ../sportspredict-hybrid immediately
# before docker build, so later edits to either checkout cannot affect a running
# cron tick until a new image is built.
COPY .deploy/hybrid_snapshot/ /sportspredict-hybrid/
RUN pip install --find-links /sportspredict-hybrid/wheels /sportspredict-hybrid

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
