#!/bin/bash
# Silently installs Xilinx ISE 14.7 WebPACK. Invoked by the Dockerfile with
# the installer payload bind-mounted read-only at /opt/installer-src (a
# build-time bind mount, not a COPY -- see the Dockerfile comment for why),
# and this script's cwd as /opt/installer (writable, empty).
#
# /opt/installer-src is either:
#   - the untarred installer directory (contains ./xsetup), or
#   - a single .tar (Xilinx_ISE_DS_Lin_14.7_*.tar) which we extract into the
#     writable /opt/installer here.
#
# The real unattended-install entry point is bin/lin64/batchxsetup, NOT the
# xsetup GUI launcher (xsetup only exposes --help/--uninstall/--copy_registry
# in this build; batchxsetup is what actually implements --batch). Found by
# inspecting both binaries directly (objdump -p / strings) since Xilinx's
# public docs describe a different -b ConfigGen/Install flow that this
# installer build doesn't implement.
set -euo pipefail

SRC=/opt/installer-src
TARBALL="$(find "$SRC" -maxdepth 1 -iname '*.tar' | head -n1 || true)"
if [ -n "$TARBALL" ]; then
    echo "Extracting $TARBALL ..."
    tar xf "$TARBALL" -C /opt/installer
    SEARCH_ROOT=/opt/installer
else
    # Already extracted -- read directly off the read-only mount, nothing to
    # copy. (batchxsetup writes its own logs relative to cwd, not beside its
    # binary, and cwd here is /opt/installer, which is writable.)
    SEARCH_ROOT="$SRC"
fi

BATCHXSETUP="$(find "$SEARCH_ROOT" -maxdepth 5 -type f -path '*/bin/lin64/batchxsetup' | head -n1 || true)"
if [ -z "$BATCHXSETUP" ]; then
    echo "ERROR: could not find bin/lin64/batchxsetup under $SEARCH_ROOT." >&2
    echo "Put the extracted ISE 14.7 installer (or its .tar) in docker/installer/ first." >&2
    exit 1
fi

BATCH_FILE=/opt/installer/batch_install.txt
cat > "$BATCH_FILE" << 'EOF'
################################################################################
# Unattended install: ISE WebPACK (free tier -- covers Spartan-6/XC6SLX75).
################################################################################

destination_dir=/opt/Xilinx
copy_preferences=N
use_multiple_cores=Y

application=Acquire or Manage a License Key::0

package=ISE WebPACK::1
application=setupEnv.sh::0
application=Install Linux System Generator Info XML::0
application=Ensure Linux System Generator Symlinks::0
application=Install Cable Drivers::0
EOF

echo "Running batchxsetup (this takes a couple minutes)..."
export TERM=xterm
# The installer pages the EULA text to the console and wants a bare
# Enter/"Y" per page, then a final Y/N accept -- `yes Y` satisfies all of it.
# `yes` gets SIGPIPE (exit 141) once batchxsetup stops reading stdin at EOF
# of the install, which is expected -- don't let pipefail turn that into a
# script failure; check batchxsetup's own exit status instead.
set +o pipefail
yes Y | "$BATCHXSETUP" --batch "$BATCH_FILE"
BATCHXSETUP_STATUS=${PIPESTATUS[1]}
set -o pipefail
if [ "$BATCHXSETUP_STATUS" -ne 0 ]; then
    echo "ERROR: batchxsetup exited $BATCHXSETUP_STATUS" >&2
    exit "$BATCHXSETUP_STATUS"
fi

test -f /opt/Xilinx/14.7/ISE_DS/settings64.sh || {
    echo "ERROR: install finished but settings64.sh is missing -- something didn't land where expected." >&2
    exit 1
}
echo "ISE 14.7 WebPACK installed to /opt/Xilinx/14.7/ISE_DS."

# Clean up anything written into the writable /opt/installer scratch dir
# (extracted tarball, batch file, install logs) so it doesn't add to the
# committed layer. The read-only bind-mounted source itself was never
# copied anywhere, so there's nothing else to reclaim.
rm -rf /opt/installer/*
