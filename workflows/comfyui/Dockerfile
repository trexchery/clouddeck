# Prebuilt ComfyUI environment for the ready-made workflows (Wan 2.2, Wan 2.2 Animate, SCAIL-2): what install.sh would otherwise install at the start of
# every job -- ComfyUI at a pinned commit, its requirements, the two Animate custom nodes and the DWPose weights -- plus what jobremote/boot.sh would apt-get.
# Model weights are NOT in it (tens of GB: the spec's [cache] items fetch and verify them in parallel while the box boots).
# Build and publish: docs/prebuilt-image.md ;  docker build -f workflows/comfyui/Dockerfile -t ghcr.io/OWNER/clouddeck-comfyui:0.42.1-1 workflows/comfyui
#
# The same base the stock fallback uses (a GPU box that works with it was verified on A100 and H100); keep it pinned by digest.
ARG BASE=pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime@sha256:417bd75df6365104c283ea4c1651fb3530d9eb5a4c2fafa51943cff2a94e6385
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
# keep this list >= BASE_APT in clouddeck/job/spec.py plus the image.apt of the workflow specs
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends openssh-server rsync curl ca-certificates aria2 tmux git libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd /root/.ssh /work \
 && rm -f /etc/ssh/ssh_host_*          # host keys are generated per container by boot.sh (ssh-keygen -A); never bake keys, tokens or secrets
COPY install.sh /tmp/install.sh
RUN bash /tmp/install.sh --with-nodes && rm -f /tmp/install.sh
WORKDIR /work
# no ENTRYPOINT/CMD: the SDL start command (jobremote/boot.sh) runs the same on this image and on the stock one
