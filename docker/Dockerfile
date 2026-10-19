# Toolchain image for `ddc job`: bakes in what jobremote/boot.sh would otherwise apt-get at every start (sshd, rsync, aria2, ...).
# jobremote/boot.sh runs `dpkg -s` per package and installs only what is missing, so this image is optional:
# `ddc job` falls back to the stock image automatically. Build/publish: docs/prebuilt-image.md
#
# BASE is a mutable tag here, like any tag: pin the BASE you build from by digest (BASE=ubuntu@sha256:<digest>) if you need a reproducible
# image, and run the result by digest too (the controller warns about mutable tags in its image setting for the same reason).
ARG BASE=ubuntu:24.04
FROM ${BASE}
ARG EXTRA_APT=""
ENV DEBIAN_FRONTEND=noninteractive
# keep this list >= BASE_APT in clouddeck/job/spec.py plus every package you list under image.apt in your specs
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
      openssh-server rsync curl ca-certificates aria2 tmux git jq build-essential ${EXTRA_APT} \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd /root/.ssh /work \
 && rm -f /etc/ssh/ssh_host_*          # host keys are generated per container by boot.sh (ssh-keygen -A); never bake keys, tokens or secrets
WORKDIR /work
# no ENTRYPOINT/CMD: the SDL start command (jobremote/boot.sh) runs the same on this image and on a stock one
