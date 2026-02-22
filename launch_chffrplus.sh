#!/usr/bin/env bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"

source "$DIR/launch_env.sh"

function agnos_init {
  # TODO: move this to agnos
  sudo rm -f /data/etc/NetworkManager/system-connections/*.nmmeta

  # set success flag for current boot slot
  sudo abctl --set_success

  # TODO: do this without udev in AGNOS
  # udev does this, but sometimes we startup faster
  sudo chgrp gpu /dev/adsprpc-smd /dev/ion /dev/kgsl-3d0
  sudo chmod 660 /dev/adsprpc-smd /dev/ion /dev/kgsl-3d0

  # Check if AGNOS update is required
  if [ $(< /VERSION) != "$AGNOS_VERSION" ]; then
    AGNOS_PY="$DIR/system/hardware/tici/agnos.py"
    MANIFEST="$DIR/system/hardware/tici/agnos.json"
    if $AGNOS_PY --verify $MANIFEST; then
      sudo reboot
    fi
    $DIR/system/hardware/tici/updater $AGNOS_PY $MANIFEST
  fi

  # Setup BLE for ABRP bridge (runs once after BT kernel is installed)
  setup_abrp_ble
}

function setup_abrp_ble {
  # Bring up WCN3990 BT chip via UART (ttyHS0 = SE6 UART at 0x898000)
  # 'any' protocol skips ROME firmware loading - chip works without rampatch
  if [ -c /dev/ttyHS0 ] && ! hciconfig hci0 2>/dev/null | grep -q "UP RUNNING"; then
    echo "Starting WCN3990 Bluetooth via hciattach..."
    # Stop bluetoothd so it doesn't conflict with hciattach (bluez 5.x D-Bus
    # activates and calls HCIDEVUP within ~3s, leaving HCI_INIT stuck on failure)
    sudo systemctl stop bluetooth 2>/dev/null || true
    sudo hciattach -s 115200 /dev/ttyHS0 any 3000000 flow 2>/dev/null &
    # 2s sleep: chip responds at 3Mbaud before bluetoothd D-Bus activates
    sleep 2
    sudo hciconfig hci0 up 2>/dev/null || true
  fi

  # Install bless Python library for BLE GATT server (once, to /data since /usr is read-only)
  if ! PYTHONPATH=/data/bless_packages python3 -c "import bless" 2>/dev/null; then
    /usr/local/venv/bin/pip install --quiet --target /data/bless_packages bless
  fi
}

function launch {
  # Remove orphaned git lock if it exists on boot
  [ -f "$DIR/.git/index.lock" ] && rm -f $DIR/.git/index.lock

  # Check to see if there's a valid overlay-based update available. Conditions
  # are as follows:
  #
  # 1. The DIR init file has to exist, with a newer modtime than anything in
  #    the DIR Git repo. This checks for local development work or the user
  #    switching branches/forks, which should not be overwritten.
  # 2. The FINALIZED consistent file has to exist, indicating there's an update
  #    that completed successfully and synced to disk.

  if [ -f "${DIR}/.overlay_init" ]; then
    find ${DIR}/.git -newer ${DIR}/.overlay_init | grep -q '.' 2> /dev/null
    if [ $? -eq 0 ]; then
      echo "${DIR} has been modified, skipping overlay update installation"
    else
      if [ -f "${STAGING_ROOT}/finalized/.overlay_consistent" ]; then
        if [ ! -d /data/safe_staging/old_openpilot ]; then
          echo "Valid overlay update found, installing"
          LAUNCHER_LOCATION="${BASH_SOURCE[0]}"

          mv $DIR /data/safe_staging/old_openpilot
          mv "${STAGING_ROOT}/finalized" $DIR
          cd $DIR

          echo "Restarting launch script ${LAUNCHER_LOCATION}"
          unset AGNOS_VERSION
          exec "${LAUNCHER_LOCATION}"
        else
          echo "openpilot backup found, not updating"
          # TODO: restore backup? This means the updater didn't start after swapping
        fi
      fi
    fi
  fi

  # handle pythonpath
  ln -sfn $(pwd) /data/pythonpath
  export PYTHONPATH="$PWD"

  # Use venv python so agnos.py, build.py, manager.py all get capnp/zmq/etc
  export PATH="/usr/local/venv/bin:$PATH"

  # hardware specific init
  if [ -f /AGNOS ]; then
    agnos_init
  fi

  # write tmux scrollback to a file
  tmux capture-pane -pq -S-1000 > /tmp/launch_log

  # start manager
  cd system/manager
  if [ ! -f $DIR/prebuilt ]; then
    ./build.py
  fi
  ./manager.py

  # if broken, keep on screen error
  while true; do sleep 1; done
}

launch
