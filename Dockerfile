FROM --platform=linux/amd64 ubuntu:22.04 AS base

SHELL ["/bin/bash", "-c"]

ENV project=attendee
ENV cwd=/$project

# Force PulseAudio in container environments for meeting audio capture
ENV FORCE_PULSE=1

# Set up comprehensive PulseAudio environment for Chrome audio routing
ENV XDG_RUNTIME_DIR=/run/user/1000
ENV PULSE_RUNTIME_DIR=/run/user/1000/pulse
ENV PULSE_RUNTIME_PATH=/run/user/1000/pulse
ENV PULSE_SERVER=unix:/run/user/1000/pulse/native
ENV PULSE_SINK=ChromeSink

# Create PulseAudio runtime directory with proper ownership
RUN mkdir -p /run/user/1000/pulse && chown -R 1000:1000 /run/user/1000

WORKDIR $cwd

ARG DEBIAN_FRONTEND=noninteractive

#  Install Dependencies
RUN apt-get update  \
    && apt-get install -y \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    gdb \
    git \
    gfortran \
    libopencv-dev \
    libdbus-1-3 \
    libgbm1 \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libglib2.0-dev \
    libssl-dev \
    libx11-dev \
    libx11-xcb1 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-shape0 \
    libxcb-shm0 \
    libxcb-xfixes0 \
    libxcb-xtest0 \
    libgl1-mesa-dri \
    libxfixes3 \
    linux-libc-dev \
    pkgconf \
    python3-pip \
    tar \
    unzip \
    zip \
    vim \
    libpq-dev

# Install Chrome dependencies
RUN apt-get install -y xvfb x11-xkb-utils xfonts-100dpi xfonts-75dpi xfonts-scalable xfonts-cyrillic x11-apps libvulkan1 fonts-liberation xdg-utils wget
# Install a specific version of Chrome.
RUN wget -q http://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_134.0.6998.88-1_amd64.deb
RUN apt-get install -y ./google-chrome-stable_134.0.6998.88-1_amd64.deb

# Install ALSA with Pulse bridge for reliable Chrome audio routing
RUN apt-get update && apt-get install -y \
    libasound2 \
    libasound2-plugins \
    alsa-utils \
    alsa-oss

# Install Pulseaudio with comprehensive audio support
RUN apt-get install -y \
    pulseaudio \
    pulseaudio-utils \
    libpulse0 \
    ffmpeg

# Configure ALSA to route to PulseAudio by default (critical for Chrome audio)
RUN printf '%s\n' \
    'pcm.!default { type pulse }' \
    'ctl.!default { type pulse }' > /etc/asound.conf

# Also create user-specific ALSA config for UID 1000
RUN mkdir -p /home/nonroot/.asoundrc.d && \
    printf '%s\n' \
        'pcm.!default { type pulse }' \
        'ctl.!default { type pulse }' > /home/nonroot/.asoundrc && \
    chown -R 1000:1000 /home/nonroot

# Install Linux Kernel Dev
RUN apt-get update && apt-get install -y linux-libc-dev

# Install Ctags
RUN apt-get update && apt-get install -y universal-ctags

# Install xterm
RUN apt-get update && apt-get install -y xterm

# Install python dependencies
# Pin deepgram-sdk to version 3.x which has DeepgramClientOptions
RUN pip install pyjwt cython gdown "deepgram-sdk>=3.0.0,<4.0.0" python-dotenv

# Install libavdevice-dev. Needed so that webpage streaming using pyav will work.
RUN apt-get update && apt-get install -y libavdevice-dev && pip uninstall -y av && pip install --no-binary av "av==12.0.0"

# Install gstreamer
RUN apt-get install -y gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgirepository1.0-dev --fix-missing

# Alias python3 to python
RUN ln -s /usr/bin/python3 /usr/bin/python

FROM base AS deps

# Copy only requirements.txt first to leverage Docker cache
COPY requirements.txt .
RUN pip install -r requirements.txt

ENV TINI_VERSION=v0.19.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini /tini
RUN chmod +x /tini

WORKDIR /opt

FROM deps AS build

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash app

# Workdir owned by app in one shot during copy
ENV project=attendee
ENV cwd=/$project
WORKDIR $cwd

# Copy only what you need; set ownership/perm at copy time
COPY --chown=app:app --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=app:app . .

# Make STATIC_ROOT writeable for the non-root user so collectstatic can run at startup
RUN mkdir -p "$cwd/staticfiles" && chown -R app:app "$cwd/staticfiles"

# Switch to non-root AFTER copies to avoid permission flakiness
USER app

# Use tini + entrypoint; CMD can be overridden by compose
ENTRYPOINT ["/tini","--","/usr/local/bin/entrypoint.sh"]
CMD ["bash"]