FROM ubuntu:22.04

# Avoid warnings by switching to noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic utilities
RUN apt-get update && apt-get -y install --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    git \
    sudo \
    locales \
    openssl \
    libstdc++6 \
    libc6 \
    libgcc-s1 \
    tar \
    jq \
    unzip \
    file \
    gzip \
    build-essential \
    procps \
    lsb-release \
    bash \
    zsh \
    openssh-client \
    gnupg2 \
    passwd \
    software-properties-common \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Update CA certificates
RUN update-ca-certificates

# Generate locale
RUN locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US:en
ENV LC_ALL=en_US.UTF-8

# Create non-root user 'vscode' with sudo access
RUN groupadd --gid 1000 vscode \
    && useradd --uid 1000 --gid 1000 -m vscode -s /bin/bash \
    && echo vscode ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/vscode \
    && chmod 0440 /etc/sudoers.d/vscode

# Set up basic shell configuration
RUN echo 'export PS1="\[\e[32m\]\u@\h\[\e[0m\]:\[\e[34m\]\w\[\e[0m\]\$ "' >> /home/vscode/.bashrc \
    && echo 'export PATH="$HOME/.local/bin:$PATH"' >> /home/vscode/.bashrc \
    && mkdir -p /home/vscode/.local/bin \
    && chown -R vscode:vscode /home/vscode

# Switch back to dialog for any ad-hoc use of apt-get
ENV DEBIAN_FRONTEND=dialog

# Set the default shell to bash
SHELL ["/bin/bash", "-c"]

# Switch to non-root user
USER vscode
WORKDIR /home/vscode
