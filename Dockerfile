FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates tzdata util-linux \
    && rm -rf /var/lib/apt/lists/*
ARG CODEX_PACKAGE_VERSION=0.162.1
RUN npm install --global "@openai/codex@${CODEX_PACKAGE_VERSION}" && codex --version

RUN useradd --create-home --shell /bin/bash cloud \
    && mkdir -p /root/.ssh /home/cloud/.ssh \
    && chmod 0700 /root/.ssh /home/cloud/.ssh \
    && chown cloud:cloud /home/cloud/.ssh \
    && usermod --unlock root
WORKDIR /app
COPY engine.py config.json /app/
COPY cloud /app/cloud
RUN chmod 0755 /app/cloud/entrypoint.sh && chmod -R go-w /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PAPER_CLOUD_STORAGE=/var/data
EXPOSE 10000
ENTRYPOINT ["/app/cloud/entrypoint.sh"]
