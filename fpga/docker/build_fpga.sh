#!/bin/bash
# Runs `make B200mini` inside the ISE container against the uhd/ checkout
# next to this repo. Bitstream lands in uhd/fpga/usrp3/top/b2xxmini/build/.
#
# License file: point XILINX_LIC on the host at your WebPACK .lic file
# (default: docker/xilinx.lic).
set -euo pipefail
cd "$(dirname "$0")"

UHD_DIR="$(cd .. && pwd)/uhd"
LIC_FILE="${XILINX_LIC:-$(pwd)/xilinx.lic}"

if [ ! -d "$UHD_DIR/fpga/usrp3/top/b2xxmini" ]; then
    echo "Can't find uhd/fpga/usrp3/top/b2xxmini next to docker/. Wrong layout?" >&2
    exit 1
fi
if [ ! -f "$LIC_FILE" ]; then
    echo "No license file at $LIC_FILE." >&2
    echo "Get a free WebPACK license (covers Spartan-6 XC6SLX75) from AMD/Xilinx" >&2
    echo "and save it there, or set XILINX_LIC=/path/to/your.lic" >&2
    exit 1
fi

docker run --rm -it \
    --network host \
    -v "$UHD_DIR:/work/uhd" \
    -v "$LIC_FILE:/opt/xilinx.lic:ro" \
    -w /work/uhd/fpga/usrp3/top/b2xxmini \
    ise147-b2xxmini \
    make B200mini "$@"
