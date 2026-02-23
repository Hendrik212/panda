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

  # Check if AGNOS update is required.
  # Keep this version-gated to avoid install loops when the full manifest
  # differs across slots (e.g. system hash mismatch during kernel-only updates).
  if [ $(< /VERSION) != "$AGNOS_VERSION" ]; then
    AGNOS_PY="$DIR/system/hardware/tici/agnos.py"
    MANIFEST="$DIR/system/hardware/tici/agnos.json"
    if $AGNOS_PY --verify $MANIFEST; then
      sudo reboot
    fi
    $DIR/system/hardware/tici/updater $AGNOS_PY $MANIFEST
  fi

  # Apply boot-partition-only updates when our custom boot update version changes.
  # This keeps kernel-only releases automatic without forcing full AGNOS updates.
  BOOT_VER_FILE="/data/agnos_boot_update_version"
  BOOT_VER_CURRENT="$(cat "$BOOT_VER_FILE" 2>/dev/null || true)"
  if [ "$BOOT_VER_CURRENT" != "$AGNOS_BOOT_UPDATE_VERSION" ]; then
    AGNOS_PY="$DIR/system/hardware/tici/agnos.py"
    BOOT_MANIFEST="/tmp/agnos_boot_only.json"

    python3 - "$DIR/system/hardware/tici/agnos.json" "$BOOT_MANIFEST" << 'PY'
import json
import pathlib
import sys

src = pathlib.Path(sys.argv[1])
dst = pathlib.Path(sys.argv[2])
manifest = json.loads(src.read_text())
boot = [p for p in manifest if p.get("name") == "boot"]
if not boot:
  raise RuntimeError("boot partition entry missing from agnos manifest")
dst.write_text(json.dumps(boot, indent=2) + "\n")
PY

    if PYTHONPATH="$DIR" $AGNOS_PY --swap "$BOOT_MANIFEST"; then
      echo "$AGNOS_BOOT_UPDATE_VERSION" | sudo tee "$BOOT_VER_FILE" >/dev/null
      sudo reboot
    else
      echo "Boot-only AGNOS update failed; will retry on next boot"
    fi
  fi

  # Setup BLE for ABRP bridge (runs once after BT kernel is installed)
  setup_abrp_ble
}

function setup_abrp_ble {
  # Bring up WCN3990 BT chip via UART (ttyHS0 = SE6 UART at 0x898000)
  # 'any' protocol skips ROME firmware loading - chip works without rampatch
  if [ -c /dev/ttyHS0 ] && ! hciconfig hci0 2>/dev/null | grep -q "UP RUNNING"; then
    echo "Starting WCN3990 Bluetooth via hciattach..."
    # Always reset prior attach state so soft-rebooted chip state is deterministic.
    sudo pkill hciattach 2>/dev/null || true
    sleep 1

    # Pause bluetoothd while we attach UART to avoid startup races.
    sudo systemctl unmask --runtime bluetooth 2>/dev/null || true
    sudo systemctl stop bluetooth 2>/dev/null || true
    attach_try() {
      local init_speed="$1"
      local target_speed="$2"
      sudo pkill btattach 2>/dev/null || true
      sudo pkill hciattach 2>/dev/null || true
      sleep 0.2
      sudo hciattach -s "$init_speed" /dev/ttyHS0 any "$target_speed" flow 2>/dev/null &

      # Repeated short tries are more robust than a single fixed timing window.
      for _ in {1..10}; do
        sudo timeout 0.5 hciconfig hci0 up 2>/dev/null || true
        if hciconfig hci0 2>/dev/null | grep -q "UP RUNNING"; then
          return 0
        fi
        sleep 0.1
      done
      return 1
    }

    # On this kernel/userspace combo, fallback to fixed 115200 leaves hci0 in a
    # stuck state (DOWN/EBUSY). Use only high-speed attach attempts.
    if ! attach_try 115200 3000000 && ! attach_try 3000000 3000000 && ! attach_try 115200 3000000; then
      sudo pkill btattach 2>/dev/null || true
      sudo pkill hciattach 2>/dev/null || true
      sudo hciconfig hci0 down 2>/dev/null || true
    fi

    if ! hciconfig hci0 2>/dev/null | grep -q "UP RUNNING"; then
      echo "WCN3990 Bluetooth init did not reach UP RUNNING"
    else
      # BlueZ GATT server (bless) needs bluetoothd + LE mode.
      sudo btmgmt -i hci0 power off >/dev/null 2>&1 || true
      sudo btmgmt -i hci0 le on >/dev/null 2>&1 || true
      sudo btmgmt -i hci0 bredr off >/dev/null 2>&1 || true
      sudo btmgmt -i hci0 connectable on >/dev/null 2>&1 || true
      sudo btmgmt -i hci0 power on >/dev/null 2>&1 || true
      sudo systemctl start bluetooth 2>/dev/null || true
    fi
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
