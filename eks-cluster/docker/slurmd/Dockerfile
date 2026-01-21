# Slurmd container with CUDA, EFA, and PyTorch from AWS DLC
#
# Multi-stage build:
# 1. AWS Deep Learning Container (DLC) - source for CUDA, EFA, Python, PyTorch
# 2. Slinky slurmd base - Slurm compute node daemon
#
# Usage:
#   ./build.sh

# Stage 1: AWS DLC PyTorch
FROM 763104351884.dkr.ecr.us-west-2.amazonaws.com/pytorch-training:2.6.0-gpu-py312-cu126-ubuntu22.04-ec2 AS dlc

# Stage 2: Slinky slurmd + DLC components
FROM ghcr.io/slinkyproject/slurmd:25.05.0-ubuntu24.04

ARG PYTHON_SHORT_VERSION=3.12

RUN mkdir -p /var/spool/slurmd

# Environment
ENV CUDA_HOME="/usr/local/cuda" \
    EFA_PATH="/opt/amazon/efa" \
    OPEN_MPI_PATH="/opt/amazon/openmpi" \
    OFI_NCCL_PATH="/opt/amazon/ofi-nccl"

ENV LD_LIBRARY_PATH="lib:${EFA_PATH}/lib:${OPEN_MPI_PATH}/lib:${CUDA_HOME}/lib64:/usr/local/lib:/lib/x86_64-linux-gnu:${OFI_NCCL_PATH}/lib/x86_64-linux-gnu:/usr/local/nvidia/lib" \
    PATH="${EFA_PATH}/bin:${OPEN_MPI_PATH}/bin:${CUDA_HOME}/bin:${PATH}" \
    NCCL_DEBUG=INFO \
    NCCL_SOCKET_IFNAME=^docker0,lo \
    PYTHONUNBUFFERED=1 \
    NVTE_FRAMEWORK=pytorch

# System dependencies
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates cmake curl git \
    libcurl4-openssl-dev libssl-dev libnuma1 libnuma-dev \
    libibverbs-dev openssh-client openssh-server \
    && rm -rf /var/lib/apt/lists/*

# CUDA from DLC
COPY --from=dlc /usr/local/cuda /usr/local/cuda

# EFA stack from DLC
COPY --from=dlc /opt/amazon/efa /opt/amazon/efa
COPY --from=dlc /opt/amazon/openmpi /opt/amazon/openmpi
COPY --from=dlc /opt/amazon/ofi-nccl /opt/amazon/ofi-nccl

# NCCL
COPY --from=dlc /usr/local/lib/libnccl* /usr/local/lib/
COPY --from=dlc /etc/nccl.conf /etc/nccl.conf

# OpenMPI config for root
RUN mv ${OPEN_MPI_PATH}/bin/mpirun ${OPEN_MPI_PATH}/bin/mpirun.real \
    && echo '#!/bin/bash' > ${OPEN_MPI_PATH}/bin/mpirun \
    && echo "${OPEN_MPI_PATH}/bin/mpirun.real --allow-run-as-root \"\$@\"" >> ${OPEN_MPI_PATH}/bin/mpirun \
    && chmod a+x ${OPEN_MPI_PATH}/bin/mpirun

# Python from DLC
COPY --from=dlc /usr/local/bin/python${PYTHON_SHORT_VERSION}* /usr/local/bin/
COPY --from=dlc /usr/local/lib/python${PYTHON_SHORT_VERSION} /usr/local/lib/python${PYTHON_SHORT_VERSION}
COPY --from=dlc /usr/local/lib/libpython${PYTHON_SHORT_VERSION}* /usr/local/lib/

RUN rm -f /usr/local/bin/python3 /usr/local/bin/python && \
    ln -s /usr/local/bin/python${PYTHON_SHORT_VERSION} /usr/local/bin/python3 && \
    ln -s /usr/local/bin/python${PYTHON_SHORT_VERSION} /usr/local/bin/python

# SSH for inter-node communication
RUN mkdir -p /var/run/sshd /root/.ssh && \
    ssh-keygen -q -t rsa -N '' -f /root/.ssh/id_rsa && \
    cp /root/.ssh/id_rsa.pub /root/.ssh/authorized_keys && \
    printf "Host *\n StrictHostKeyChecking no\n" >> /root/.ssh/config

WORKDIR /home
