# Debian 12 (same base as an Xos node) with the packages the node uses, minus the display stack.
FROM debian:bookworm-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      chromium python3 curl ca-certificates fonts-inter fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /xos
COPY app ./site/app
COPY static ./site/static
COPY os/agent ./agent
COPY os/test/smoke_test.py ./smoke_test.py
ENV XOS_APP_ROOT=/xos/site XOS_NODE_CONFIG=/xos/node.json
RUN echo '{"role":"tv","room":"lamar","zoom_room_id":"lamar-zr","port":7000}' > /xos/node.json
CMD ["python3", "/xos/smoke_test.py"]
