# A demo room computer: what os/appliance/install.sh puts on a box (Cage, Chromium, PipeWire) plus
# screenshot (grim) and remote-view (wayvnc) tools. Built on the appliance test image.
#   docker build -f os/appliance/test/Dockerfile -t xos-appliance-test .
#   docker build -f os/appliance/demo/Dockerfile -t xos-demo-room .
FROM xos-appliance-test
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      grim wlr-randr wayvnc fonts-noto-color-emoji fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*
