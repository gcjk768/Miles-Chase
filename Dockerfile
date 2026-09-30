# Runtime for the miles reports on a NAS. The project folder is mounted at /app,
# so edits to data/, the prompts or .env apply without rebuilding.
FROM node:22-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code
ENV PYTHONUNBUFFERED=1
WORKDIR /app
CMD ["python3", "scheduler.py"]
