#!/usr/bin/env python3
"""
Bring up an HCI device by issuing the HCIDEVUP ioctl.

Replaces `hciconfig hciN up` which was removed from AGNOS 17+ rootfs.
Must run as root (via sudo).

Usage: sudo python3 hci_up.py [dev_id]
  dev_id: HCI device index (default 0 for hci0)
"""
import fcntl
import socket
import struct
import sys

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
# HCIDEVUP = _IOW('H', 201, int) on ARM64
# = (1<<30) | (ord('H')<<8) | 201 | (4<<16) = 0x400448C9
HCIDEVUP = 0x400448C9

dev_id = int(sys.argv[1]) if len(sys.argv) > 1 else 0

s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
try:
    fcntl.ioctl(s.fileno(), HCIDEVUP, struct.pack('I', dev_id))
    print(f"hci{dev_id} up")
finally:
    s.close()
