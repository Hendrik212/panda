#!/usr/bin/env python3
"""
ABRP OBD BLE Bridge for Hyundai Ioniq 6

Emulates an ELM327 OBD adapter over Bluetooth Low Energy (Nordic UART Service).
ABRP connects to this as if it were a real OBD dongle and requests battery data.

Data sources:
- SoC, voltage, current: CAN bus 0x2fa frames
- Speed: carState.vEgo from cereal

Requires:
- BlueZ + kernel BT support (custom Agnos kernel with CONFIG_BT=y)
- bless library: pip install bless

Usage:
  python abrp_ble.py

The service advertises as "ABRP_OBD" and responds to ELM327 AT commands
and Mode 22 PIDs for Hyundai BMS (7E4).
"""

import asyncio
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# Bundled BT tools (hciattach/btmgmt/bluetoothd from BlueZ 5.66 Debian 12, glibc 2.31 compatible)
# These replace system tools removed in AGNOS 17+
_BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
_BTATTACH = os.path.join(_BIN_DIR, "btattach")
_BTMGMT = os.path.join(_BIN_DIR, "btmgmt")
_BLUETOOTHD = os.path.join(_BIN_DIR, "bluetoothd")

# Ensure bundled binaries are executable (git doesn't preserve exec bits for LFS blobs)
for _b in (_BTATTACH, _BTMGMT, _BLUETOOTHD):
    if os.path.isfile(_b) and not os.access(_b, os.X_OK):
        os.chmod(_b, 0o755)

# D-Bus policy needed for bluetoothd to own org.bluez (removed in AGNOS 17+)
_DBUS_POLICY_PATH = "/etc/dbus-1/system.d/bluetooth.conf"
_DBUS_POLICY = """<!-- BlueZ D-Bus policy installed by abrp_ble for AGNOS 17+ compatibility -->
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="root">
    <allow own="org.bluez"/>
    <allow send_destination="org.bluez"/>
    <allow send_interface="org.bluez.Agent1"/>
    <allow send_interface="org.bluez.Profile1"/>
    <allow send_interface="org.bluez.GattCharacteristic1"/>
    <allow send_interface="org.bluez.GattDescriptor1"/>
    <allow send_interface="org.bluez.LEAdvertisement1"/>
    <allow send_interface="org.freedesktop.DBus.ObjectManager"/>
    <allow send_interface="org.freedesktop.DBus.Properties"/>
  </policy>
  <policy context="default">
    <allow send_destination="org.bluez"/>
  </policy>
</busconfig>
"""



def _ensure_dbus_policy() -> None:
    """Write bluetoothd D-Bus policy if missing (AGNOS 17+ has read-only rootfs)."""
    if os.path.exists(_DBUS_POLICY_PATH):
        return
    try:
        import subprocess
        # Remount root rw briefly to write the policy file
        subprocess.run(["sudo", "mount", "-o", "remount,rw", "/"], check=True, timeout=5)
        os.makedirs(os.path.dirname(_DBUS_POLICY_PATH), exist_ok=True)
        with open(_DBUS_POLICY_PATH, "w") as f:
            f.write(_DBUS_POLICY)
        subprocess.run(["sudo", "mount", "-o", "remount,ro", "/"], timeout=5)
        # Reload D-Bus config
        subprocess.run(["sudo", "systemctl", "reload", "dbus"], timeout=5)
        print("[ABRP-BLE] Installed bluetoothd D-Bus policy")
    except Exception as e:
        print(f"[ABRP-BLE] Failed to install D-Bus policy: {e}")

# bless is installed to /data/bless_packages (system venv is read-only)
if "/data/bless_packages" not in sys.path:
    sys.path.insert(0, "/data/bless_packages")

# BLE imports - will fail gracefully if not available
try:
    from bless import BlessServer, BlessGATTCharacteristic, GATTCharacteristicProperties, GATTAttributePermissions
    BLE_AVAILABLE = True
except ImportError:
    BLE_AVAILABLE = False
    BlessServer = None
    BlessGATTCharacteristic = object
    GATTCharacteristicProperties = None
    GATTAttributePermissions = None
    print("[ABRP-BLE] bless library not installed, BLE disabled")

# Cereal imports
try:
    import cereal.messaging as messaging
    CEREAL_AVAILABLE = True
except ImportError:
    CEREAL_AVAILABLE = False
    print("[ABRP-BLE] cereal not available, using mock data")


# Nordic UART Service UUIDs
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Write (phone → device)
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Notify (device → phone)

# OBDLink-style BLE UART profile (used by many ABRP-supported dongles)
OBD_SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
OBD_NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"  # Read/Notify
OBD_WRITE_CHAR_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"   # Write/WriteWithoutResponse

# Device Information service to improve compatibility probing
DIS_SERVICE_UUID = "0000180a-0000-1000-8000-00805f9b34fb"
DIS_MANUFACTURER_UUID = "00002a29-0000-1000-8000-00805f9b34fb"
DIS_MODEL_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
DIS_FWREV_UUID = "00002a26-0000-1000-8000-00805f9b34fb"


@dataclass
class EVData:
    """Thread-safe container for EV telemetry data."""
    soc: float = 0.0           # State of charge (%)
    voltage: float = 0.0       # Pack voltage (V)
    current: float = 0.0       # Battery current (A, positive = charging)
    speed_kmh: float = 0.0     # Vehicle speed (km/h)
    timestamp: float = 0.0     # Last update time
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, soc=None, voltage=None, current=None, speed_kmh=None):
        with self._lock:
            if soc is not None:
                self.soc = soc
            if voltage is not None:
                self.voltage = voltage
            if current is not None:
                self.current = current
            if speed_kmh is not None:
                self.speed_kmh = speed_kmh
            self.timestamp = time.time()

    def get_snapshot(self) -> dict:
        with self._lock:
            return {
                'soc': self.soc,
                'voltage': self.voltage,
                'current': self.current,
                'speed_kmh': self.speed_kmh,
                'timestamp': self.timestamp
            }


# Global EV data instance
ev_data = EVData()


class ELM327Handler:
    """
    Emulates ELM327 OBD adapter responses.

    Handles AT commands and Mode 22 PIDs for Hyundai BMS (ECU 7E4).
    """

    def __init__(self):
        self.echo = True
        self.headers = False
        self.current_header = "7E4"  # Default to BMS ECU
        self.protocol = "0"
        self.linefeed = True

    def process_command(self, cmd: str) -> str:
        """Process an ELM327 command and return response."""
        cmd = cmd.strip().upper()

        # Handle empty commands
        if not cmd:
            return ">"

        # Log for debugging
        print(f"[ABRP-BLE] RX: {cmd}")

        # AT commands
        if cmd.startswith("AT"):
            return self._handle_at_command(cmd)

        # OBD Mode 01 (standard PIDs)
        if cmd.startswith("01"):
            return self._handle_mode_01(cmd)

        # OBD Mode 22 (manufacturer-specific) - handles both "22XXXX" and "XXXX" formats
        if cmd.startswith("22") or cmd.startswith("2101") or cmd.startswith("2105"):
            return self._handle_mode_22(cmd)

        # Unknown command
        return "?"

    def _handle_at_command(self, cmd: str) -> str:
        """Handle AT commands."""
        at_cmd = cmd[2:]  # Strip "AT"

        # Reset
        if at_cmd in ("Z", "WS", "D"):
            self.echo = True
            self.headers = False
            self.current_header = "7E4"
            return "ELM327 v1.5"

        # Echo control
        if at_cmd == "E0":
            self.echo = False
            return "OK"
        if at_cmd == "E1":
            self.echo = True
            return "OK"

        # Headers
        if at_cmd == "H0":
            self.headers = False
            return "OK"
        if at_cmd == "H1":
            self.headers = True
            return "OK"

        # Linefeed
        if at_cmd == "L0":
            self.linefeed = False
            return "OK"
        if at_cmd == "L1":
            self.linefeed = True
            return "OK"

        # Protocol
        if at_cmd.startswith("SP"):
            self.protocol = at_cmd[2:]
            return "OK"

        # Set header (ECU address)
        if at_cmd.startswith("SH"):
            self.current_header = at_cmd[2:]
            return "OK"

        # CAN filter/mask - just acknowledge
        if at_cmd.startswith("CF") or at_cmd.startswith("CM") or at_cmd.startswith("CRA"):
            return "OK"

        # Timing/formatting - acknowledge
        if at_cmd in ("M0", "M1", "S0", "S1", "AT0", "AT1", "AT2", "AL", "AR"):
            return "OK"
        if at_cmd.startswith("ST"):
            return "OK"

        # CAN priority
        if at_cmd.startswith("CP"):
            return "OK"

        # Describe protocol
        if at_cmd == "DP" or at_cmd == "DPN":
            return "ISO 15765-4 (CAN 11/500)"

        # Read voltage
        if at_cmd == "RV":
            return "12.4V"

        # Device description
        if at_cmd == "I":
            return "OBDLink CX"

        if at_cmd == "@1":
            return "OBDLink CX"

        # Unknown AT command - just OK
        return "OK"

    def _handle_mode_01(self, cmd: str) -> str:
        """Handle Mode 01 (standard OBD) requests."""
        pid = cmd[2:4] if len(cmd) >= 4 else ""

        # PID 00: Supported PIDs 01-20
        if pid == "00":
            # Report support for PID 0D (speed)
            if self.headers:
                return "7E8 06 41 00 00 00 10 00"
            return "41 00 00 00 10 00"

        # PID 0D: Vehicle speed
        if pid == "0D":
            data = ev_data.get_snapshot()
            speed = int(min(255, max(0, data['speed_kmh'])))
            if self.headers:
                return f"7E8 03 41 0D {speed:02X}"
            return f"41 0D {speed:02X}"

        return "NO DATA"

    def _handle_mode_22(self, cmd: str) -> str:
        """
        Handle Mode 22 (Hyundai BMS) requests.

        PIDs:
        - 2101: Main BMS data (SoC, voltage, current)
        - 2105: Extended data (display SoC)
        """
        # Normalize command - extract 4-digit PID
        if cmd.startswith("22"):
            pid = cmd[2:6]
        else:
            pid = cmd[:4]

        data = ev_data.get_snapshot()

        if pid == "0101":
            return self._build_2101_response(data)
        elif pid == "0105":
            return self._build_2105_response(data)

        return "NO DATA"

    def _build_2101_response(self, data: dict) -> str:
        """
        Build Mode 22 PID 2101 response (main BMS data).

        Hyundai Ioniq EV BMS frame layout (61 bytes after service ID):
        - Byte E (index 4): SoC BMS (value * 2 = %)
        - Bytes K,L (index 10,11): Current (signed, / 10 = A)
        - Bytes M,N (index 12,13): Voltage (/ 10 = V)
        """
        # Create 61-byte response frame
        frame = bytearray(61)

        # SoC at byte E (index 4)
        soc_raw = int(data['soc'] * 2)  # SoC * 2
        frame[4] = min(200, max(0, soc_raw))

        # Current at bytes K,L (indices 10,11)
        # Formula: ((Signed(K)*256)+L)/10 = Amps
        current_raw = int(data['current'] * 10)
        if current_raw < 0:
            current_raw = current_raw + 65536  # Convert to unsigned 16-bit
        frame[10] = (current_raw >> 8) & 0xFF  # Byte K (high)
        frame[11] = current_raw & 0xFF          # Byte L (low)

        # Voltage at bytes M,N (indices 12,13)
        # Formula: ((M*256)+N)/10 = Volts
        voltage_raw = int(data['voltage'] * 10)
        frame[12] = (voltage_raw >> 8) & 0xFF  # Byte M (high)
        frame[13] = voltage_raw & 0xFF          # Byte N (low)

        # Build response string
        response = "62 01 01 " + " ".join(f"{b:02X}" for b in frame)

        if self.headers:
            # Multi-frame ISO-TP response header
            return f"7EC 10 3E {response}"

        return response

    def _build_2105_response(self, data: dict) -> str:
        """
        Build Mode 22 PID 2105 response (extended BMS data).

        - Byte AF (index 30): Display SoC (value * 2 = %)
        """
        # Create 41-byte response frame (2105 is shorter)
        frame = bytearray(41)

        # Display SoC at byte AF (index 30)
        soc_raw = int(data['soc'] * 2)
        frame[30] = min(200, max(0, soc_raw))

        response = "62 01 05 " + " ".join(f"{b:02X}" for b in frame)

        if self.headers:
            return f"7EC 10 2A {response}"

        return response


class ABRPBLEServer:
    """BLE GATT server emulating ELM327 over Nordic UART Service."""

    def __init__(self):
        self.server: Optional[BlessServer] = None
        self.elm = ELM327Handler()
        self.running = False
        self.rx_buffer = ""
        self.recovering_bt = False
        self.attach_proc: Optional[subprocess.Popen] = None

    @staticmethod
    def _run_cmd(cmd: list[str], timeout: float = 3.0) -> tuple[int, str]:
        try:
            p = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            return p.returncode, p.stdout
        except Exception as e:
            return 1, str(e)

    def _hci_up_running(self) -> bool:
        """Check if hci0 is registered and accessible via the BT management interface."""
        rc, out = self._run_cmd(["sudo", _BTMGMT, "info"], timeout=3.0)
        return rc == 0 and "hci0" in out and "Primary controller" in out

    async def _configure_bt(self) -> None:
        """Configure hci0 for BLE advertising and start bluetoothd."""
        _ensure_dbus_policy()

        # Stop any system bluetooth service; we manage our own bluetoothd
        self._run_cmd(["sudo", "systemctl", "mask", "--runtime", "bluetooth"], timeout=3.0)
        self._run_cmd(["sudo", "systemctl", "stop", "bluetooth"], timeout=3.0)
        self._run_cmd(["sudo", "pkill", "bluetoothd"], timeout=2.0)
        await asyncio.sleep(0.3)

        # Start bundled bluetoothd — it discovers hci0 via the mgmt interface
        subprocess.Popen(["sudo", _BLUETOOTHD, "-n", "--noplugin=*"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(1.5)  # let it register with D-Bus

        self._run_cmd(["sudo", _BTMGMT, "-i", "hci0", "power", "off"], timeout=2.0)
        self._run_cmd(["sudo", _BTMGMT, "-i", "hci0", "le", "on"], timeout=2.0)
        self._run_cmd(["sudo", _BTMGMT, "-i", "hci0", "bredr", "off"], timeout=2.0)
        self._run_cmd(["sudo", _BTMGMT, "-i", "hci0", "connectable", "on"], timeout=2.0)
        rc, out = self._run_cmd(["sudo", _BTMGMT, "-i", "hci0", "power", "on"], timeout=2.0)
        print(f"[ABRP-BLE] btmgmt power on (rc={rc}): {out.strip()}")

    def _setup_bt_firmware(self) -> None:
        """Ensure QCA ROME firmware is at a path the kernel firmware loader can find.

        On AGNOS 17+, the firmware_class.path is set to /firmware/image at boot (vfat,
        read-only, no QCA BT files). The fallback paths include /lib/firmware. We mount
        a tmpfs over /lib/firmware/qca/ and copy the correct files there so the kernel's
        hci_qca driver can find them during btattach.
        """
        fw_src = "/data/firmware/qca"
        fw_dst = "/lib/firmware/qca"
        needed = ["rampatch_02140201.bin", "nvm_02140201.bin"]

        # Check if already set up
        if all(os.path.isfile(os.path.join(fw_dst, f)) for f in needed):
            return

        try:
            self._run_cmd(["sudo", "mkdir", "-p", fw_dst], timeout=3.0)
            # Mount tmpfs to make the directory writable (root fs is read-only)
            rc, _ = self._run_cmd(["sudo", "mount", "-t", "tmpfs", "tmpfs", fw_dst], timeout=5.0)
            if rc != 0:
                print(f"[ABRP-BLE] Warning: could not mount tmpfs on {fw_dst}")
                return
            for fname in needed:
                src = os.path.join(fw_src, fname)
                dst = os.path.join(fw_dst, fname)
                if os.path.isfile(src):
                    self._run_cmd(["sudo", "cp", src, dst], timeout=3.0)
            print(f"[ABRP-BLE] BT firmware staged at {fw_dst}")
        except Exception as e:
            print(f"[ABRP-BLE] Warning: firmware setup failed: {e}")

    async def ensure_bt_ready(self, attempts: int = 3) -> bool:
        """Bring up hci0 with a userspace sequence known to work on this platform."""
        if self._hci_up_running():
            await self._configure_bt()
            return True

        print("[ABRP-BLE] Recovering Bluetooth adapter...")
        self.recovering_bt = True
        try:
            # Ensure QCA firmware is in a location the kernel firmware loader can access.
            # Must be done before btattach triggers the kernel's hci_qca firmware download.
            self._setup_bt_firmware()

            for i in range(1, attempts + 1):
                print(f"[ABRP-BLE] BT recovery attempt {i}/{attempts}")
                self._run_cmd(["sudo", "pkill", "btattach"], timeout=1.0)
                if self.attach_proc is not None:
                    try:
                        self.attach_proc.terminate()
                        self.attach_proc.wait(timeout=1.0)
                    except Exception:
                        pass
                    self.attach_proc = None
                await asyncio.sleep(2.0)

                self._run_cmd(["sudo", "systemctl", "mask", "--runtime", "bluetooth"], timeout=3.0)
                self._run_cmd(["sudo", "systemctl", "stop", "bluetooth"], timeout=3.0)

                # btattach sets the QCA UART line discipline; the kernel's hci_qca driver
                # runs setup including firmware download (rampatch + nvm via 0xfc00 commands).
                # btattach stays running to hold the UART line discipline open.
                self.attach_proc = subprocess.Popen(
                    ["sudo", _BTATTACH, "-B", "/dev/ttyHS0", "-P", "qca"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                await asyncio.sleep(20.0)  # wait for kernel QCA setup + firmware download

                if self._hci_up_running():
                    await self._configure_bt()
                    print("[ABRP-BLE] Bluetooth adapter is UP RUNNING")
                    return True
                else:
                    self._run_cmd(["sudo", "pkill", "btattach"], timeout=1.0)
                    if self.attach_proc is not None:
                        try:
                            self.attach_proc.terminate()
                            self.attach_proc.wait(timeout=1.0)
                        except Exception:
                            pass
                        self.attach_proc = None
                    await asyncio.sleep(3.0)  # pause before retry

            print("[ABRP-BLE] BT recovery failed after retries")
            return False
        finally:
            self.recovering_bt = False

    async def start(self):
        """Start the BLE server."""
        if not BLE_AVAILABLE:
            print("[ABRP-BLE] BLE not available, cannot start server")
            return False

        ready = await self.ensure_bt_ready()
        if not ready:
            return False

        print("[ABRP-BLE] Starting BLE server...")
        try:
            self.server = BlessServer(name="OBDLink CX")
            self.server.read_request_func = self._on_read
            self.server.write_request_func = self._on_write

            # Add OBDLink-style UART profile first.
            # bless advertises only the first service UUID, so keep FFF0 first.
            await self.server.add_new_service(OBD_SERVICE_UUID)
            await self.server.add_new_characteristic(
                OBD_SERVICE_UUID,
                OBD_NOTIFY_CHAR_UUID,
                GATTCharacteristicProperties.notify | GATTCharacteristicProperties.read,
                None,
                GATTAttributePermissions.readable
            )
            await self.server.add_new_characteristic(
                OBD_SERVICE_UUID,
                OBD_WRITE_CHAR_UUID,
                GATTCharacteristicProperties.write | GATTCharacteristicProperties.write_without_response,
                None,
                GATTAttributePermissions.writeable
            )

            # Add Nordic UART Service
            await self.server.add_new_service(NUS_SERVICE_UUID)

            # RX characteristic (write from phone)
            await self.server.add_new_characteristic(
                NUS_SERVICE_UUID,
                NUS_RX_CHAR_UUID,
                GATTCharacteristicProperties.write | GATTCharacteristicProperties.write_without_response,
                None,
                GATTAttributePermissions.writeable
            )

            # TX characteristic (notify to phone)
            await self.server.add_new_characteristic(
                NUS_SERVICE_UUID,
                NUS_TX_CHAR_UUID,
                GATTCharacteristicProperties.notify | GATTCharacteristicProperties.read,
                None,
                GATTAttributePermissions.readable
            )

            # Add Device Information Service (common compatibility probe target)
            await self.server.add_new_service(DIS_SERVICE_UUID)
            await self.server.add_new_characteristic(
                DIS_SERVICE_UUID,
                DIS_MANUFACTURER_UUID,
                GATTCharacteristicProperties.read,
                bytearray(b"OBD Solutions, LLC"),
                GATTAttributePermissions.readable
            )
            await self.server.add_new_characteristic(
                DIS_SERVICE_UUID,
                DIS_MODEL_UUID,
                GATTCharacteristicProperties.read,
                bytearray(b"OBDLink CX"),
                GATTAttributePermissions.readable
            )
            await self.server.add_new_characteristic(
                DIS_SERVICE_UUID,
                DIS_FWREV_UUID,
                GATTCharacteristicProperties.read,
                bytearray(b"5.6.19"),
                GATTAttributePermissions.readable
            )

            # Start advertising
            await self.server.start()
            self.running = True
            print("[ABRP-BLE] BLE server started, advertising as 'OBDLink CX'")
            return True
        except Exception as e:
            # BlueZ can race adapter registration on boot; retry via outer self-heal loop.
            print(f"[ABRP-BLE] BLE server start failed: {e}")
            self.running = False
            self.server = None
            return False

    async def stop(self):
        """Stop the BLE server."""
        if self.server:
            await self.server.stop()
            self.running = False
            print("[ABRP-BLE] BLE server stopped")

    def _on_read(self, characteristic: BlessGATTCharacteristic, **kwargs) -> bytearray:
        """Handle read requests."""
        return bytearray(b">")

    def _on_write(self, characteristic: BlessGATTCharacteristic, value: bytes, **kwargs):
        """Handle write requests (commands from ABRP)."""
        if characteristic.uuid not in (NUS_RX_CHAR_UUID, OBD_WRITE_CHAR_UUID):
            return

        # Decode received data
        try:
            text = value.decode('utf-8')
        except UnicodeDecodeError:
            text = value.decode('latin-1')

        # Buffer commands (may come in chunks)
        self.rx_buffer += text

        # Process complete commands (terminated by \r or \n)
        while '\r' in self.rx_buffer or '\n' in self.rx_buffer:
            # Find first terminator
            idx = len(self.rx_buffer)
            for term in ['\r', '\n']:
                pos = self.rx_buffer.find(term)
                if pos >= 0:
                    idx = min(idx, pos)

            cmd = self.rx_buffer[:idx].strip()
            self.rx_buffer = self.rx_buffer[idx+1:]

            if cmd:
                response = self.elm.process_command(cmd)
                asyncio.create_task(self._send_response(response))

    async def _send_response(self, response: str):
        """Send response back to ABRP via TX characteristic."""
        if not self.server or not self.running:
            return

        # Format response with CR/LF and prompt
        full_response = f"{response}\r\n>"
        print(f"[ABRP-BLE] TX: {response}")
        payload = bytearray(full_response.encode('utf-8'))

        # bless on BlueZ 5.72 uses update_value(service_uuid, char_uuid)
        # rather than notify_subscribers.
        for service_uuid, char_uuid in (
            (NUS_SERVICE_UUID, NUS_TX_CHAR_UUID),
            (OBD_SERVICE_UUID, OBD_NOTIFY_CHAR_UUID),
        ):
            try:
                ch = self.server.get_characteristic(char_uuid)
                if ch is None:
                    continue
                ch.value = payload
                self.server.update_value(service_uuid, char_uuid)
            except Exception as e:
                print(f"[ABRP-BLE] Failed to update {char_uuid}: {e}")


def cereal_listener_thread():
    """Background thread to receive carState and CAN data from cereal."""
    if not CEREAL_AVAILABLE:
        print("[ABRP-BLE] Cereal not available, using mock data loop")
        while True:
            # Mock data for testing
            ev_data.update(soc=75.0, voltage=400.0, current=0.0, speed_kmh=0.0)
            time.sleep(1.0)
        return

    print("[ABRP-BLE] Starting cereal listener...")

    sm = messaging.SubMaster(['carState', 'can'])

    while True:
        sm.update(100)  # 100ms timeout

        # Get speed from carState
        if sm.updated['carState']:
            speed_ms = sm['carState'].vEgo
            speed_kmh = speed_ms * 3.6
            ev_data.update(speed_kmh=speed_kmh)

        # Parse Hyundai battery metrics directly from bus 1 CAN frame 0x2FA.
        if sm.updated['can']:
            for can_msg in sm['can']:
                if can_msg.src != 1 or can_msg.address != 0x2FA:
                    continue
                data = can_msg.dat
                if len(data) < 26:
                    continue

                # Byte 15: SoC in 0.5% units.
                soc = data[15] / 2.0
                # Bytes 4-5: Pack voltage in 0.1V.
                voltage = ((data[4]) | (data[5] << 8)) * 0.1
                # Bytes 10-11: Signed current in 0.1A.
                current_raw = (data[10] << 8) | data[11]
                if current_raw >= 0x8000:
                    current_raw -= 0x10000
                current = current_raw * 0.1

                ev_data.update(soc=soc, voltage=voltage, current=current)
                break


async def async_main():
    """Main entry point."""
    print("[ABRP-BLE] ABRP OBD BLE Bridge starting...")
    print("[ABRP-BLE] Waiting for BLE kernel support...")

    # Start cereal listener in background thread
    listener = threading.Thread(target=cereal_listener_thread, daemon=True)
    listener.start()

    # Start BLE server
    server = ABRPBLEServer()
    started = await server.start()

    if not started:
        print("[ABRP-BLE] BLE server failed to start. Will keep retrying recovery...")

    # Run forever
    try:
        while True:
            await asyncio.sleep(5.0)

            # Self-heal BT/advertising if adapter dropped or start failed.
            if not server.running and not server.recovering_bt:
                await server.start()
            elif server.running and not server._hci_up_running() and not server.recovering_bt:
                print("[ABRP-BLE] hci0 dropped, restarting BLE server")
                await server.stop()
                await server.start()

            # Periodic status log
            data = ev_data.get_snapshot()
            if data['timestamp'] > 0:
                age = time.time() - data['timestamp']
                print(f"[ABRP-BLE] Data: SoC={data['soc']:.1f}% V={data['voltage']:.1f}V "
                      f"I={data['current']:.1f}A Speed={data['speed_kmh']:.1f}km/h (age={age:.1f}s)")
    except KeyboardInterrupt:
        print("[ABRP-BLE] Shutting down...")
    finally:
        await server.stop()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
