# Runtime for the miles reports on a NAS. The project folder is mounted at /app,
# so edits to data/, the prompts or .env apply without rebuilding.
# The full node image already includes python3 and CA certificates.
FROM node:22-bookworm
RUN npm install -g @anthropic-ai/claude-code
ENV PYTHONUNBUFFERED=1
WORKDIR /app
CMD ["python3", "scheduler.py"]
