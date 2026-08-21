# Xilinx ISE 14.7 build environment for the USRP B200mini (Spartan-6 XC6SLX75).
#
# ISE 14.7 is the last ISE release and the only Xilinx toolchain that supports
# Spartan-6 (Vivado dropped it). It's proprietary and not redistributable, so
# this Dockerfile does NOT bundle the installer. Supply it at build time via
# the `docker/installer/` directory (see docker/README.md).
#
# Base: Debian 9 (stretch). Ubuntu 16.04/18.04 -- the usual ISE 14.7
# recommendation -- have both been fully pruned from old-releases.ubuntu.com
# (verified: neither xenial/ nor bionic/ exist there anymore as of 2026).
# Debian's archive.debian.org still serves stretch in full, and stretch is a
# commonly reported working base for ISE 14.7's old libstdc++/ncurses ABI.
#
# Single-stage, and the installer payload (~13GB) is brought in via a
# build-time bind mount (RUN --mount=type=bind), not COPY. A COPY would bake
# that payload into a permanent layer regardless of deleting it in a later
# RUN -- union filesystem layers don't reclaim space across instructions,
# only within the same one. A bind mount exists only for the duration of the
# RUN step and is never diffed into any layer at all. (Earlier attempts hit
# "no space left on device": multi-stage COPY --from needed both stages'
# layers on disk simultaneously during export, and plain COPY permanently
# baked in the installer payload on top of that -- both avoided here.)
FROM debian:stretch

# stretch is EOL; point apt at the permanent archive mirror, and disable the
# Valid-Until check since archived Release files are, by definition, expired.
RUN sed -i \
      -e 's|deb.debian.org/debian|archive.debian.org/debian|g' \
      -e 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' \
      -e '/stretch-updates/d' \
      /etc/apt/sources.list && \
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid-until

RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential \
      make \
      tar \
      unzip \
      xz-utils \
      dos2unix \
      python3 \
      libncurses5 \
      libncurses5:i386 \
      zlib1g \
      zlib1g:i386 \
      libstdc++6 \
      libstdc++6:i386 \
      libx11-6 \
      libx11-6:i386 \
      libxext6 \
      libxext6:i386 \
      libxft2 \
      libxft2:i386 \
      libxi6 \
      libxi6:i386 \
      libxrender1 \
      libxrender1:i386 \
      libxtst6 \
      libxtst6:i386 \
      libsm6 \
      libsm6:i386 \
      libice6 \
      libice6:i386 \
      libfontconfig1 \
      libfontconfig1:i386 \
      libfreetype6 \
      libfreetype6:i386 \
      libglib2.0-0 \
      libglib2.0-0:i386 \
      libuuid1 \
      libuuid1:i386 \
      libxcursor1 \
      libxcursor1:i386 \
      libxrandr2 \
      libxrandr2:i386 \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ISE's Motif-based tools (impact, cs) want libXm. Not present in this base
# either; if a step below fails looking for libXm.so.4, that's why. See
# docker/README.md "Known gotchas".

WORKDIR /opt/installer
RUN --mount=type=bind,source=installer,target=/opt/installer-src \
    --mount=type=bind,source=install.sh,target=/opt/install.sh \
    bash /opt/install.sh

ENV XILINXD_LICENSE_FILE=/opt/xilinx.lic
# settings64.sh checks $# to decide whether it was given an explicit install
# path; sourced from entrypoint.sh it would otherwise inherit entrypoint's
# own "$@" (e.g. "bash -c ...") and silently misinterpret "bash" as the
# install path, sourcing nothing and leaving PATH untouched. Pass the path
# explicitly to sidestep that.
RUN printf '#!/bin/bash\nsource /opt/Xilinx/14.7/ISE_DS/settings64.sh /opt/Xilinx/14.7/ISE_DS\nexec "$@"\n' > /entrypoint.sh && \
    chmod +x /entrypoint.sh

WORKDIR /work
ENTRYPOINT ["/entrypoint.sh"]
CMD ["/bin/bash"]
