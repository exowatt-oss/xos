# Debian 13 with what the room computer appliance installs (os/appliance/install.sh), plus test
# tools: openssl (the stand-in Meet's certificate) and iproute2 (to check nothing listens).
FROM debian:trixie-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      cage chromium python3 ca-certificates fonts-dejavu-core dbus-user-session \
      pipewire pipewire-pulse wireplumber pulseaudio-utils openssl iproute2 procps \
      at-spi2-core python3-pyatspi python3-gi wtype \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home xos
COPY os/appliance/policies/room.json /etc/chromium/policies/managed/xos-appliance.json
