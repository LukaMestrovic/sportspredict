# syntax=docker/dockerfile:1
#
# Immutable image for the sportspredict-llm deployed runner. The CURRENT
# source is baked in, so once deployed the running bot is a frozen snapshot:
# editing the working tree never affects a live manual/settle run until the next
# scripts/deploy.sh rebuild. Secrets are NEVER baked — the API keys are passed
# at `docker run` via -e by the generated deployed runner. Build context = repo
# root.
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
# logs/ are intentionally absent — they are bind-mounted at run time so retained
# odds, intent-resolution, run-audit, and ledger state persists across runs.
COPY bot/ /app/bot/
COPY scripts/ /app/scripts/
COPY prompts/ /app/prompts/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Deployed entrypoint accepts only `manual ...` and `settle ...`.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
