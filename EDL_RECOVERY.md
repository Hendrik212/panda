# Comma 3 EDL Recovery Guide

Recovering a bricked Comma 3 (SDM845) via Qualcomm EDL (Emergency Download) mode — without a factory reset.

## Prerequisites

- Linux box with USB access (tested on Ubuntu at `192.168.1.131`)
- `edl` tool: https://github.com/bkerler/edl — clone to `~/edl_repo`
- Firehose loader: `Loaders/qualcomm/factory/sdm845_sdm850_sda845/0008e0e100000000_afca69d4235117e5_fhprg.bin`
  - This is included in the edl repo's loader collection
- Python 3 with `pyusb` installed
- `sudo` access for USB

### Required edl patches (apply once after cloning)

**1. Fix AttributeError in Sahara version check** (`edl`, line ~295):
```python
# Before:
version = conninfo["data"].version
# After:
version = conninfo["data"].version if hasattr(conninfo["data"], "version") else 2
```

**2. Fix `setactiveslot` hanging on USB bulk reads** (`edlclient/Library/firehose.py`):

In `cmd_setactiveslot`, replace the backup GPT reads and skip non-boot LUNs:

```python
# In the for lun_a in self.luns: loop, add at the top of the loop body:
if lun_a != 4:  # Only LUN 4 has boot_a/boot_b on Comma 3
    continue

# Replace backup GPT read (causes USB bulk transfer hang):
# BEFORE:
backup_gpt_data_a, backup_guid_gpt_a = self.get_gpt(lun_a, 0, 0, 0, guid_gpt_a.header.backup_lba)
# AFTER:
backup_gpt_data_a, backup_guid_gpt_a = gpt_data_a, guid_gpt_a  # skip backup GPT read
```

## Entering EDL Mode

With the device powered off, hold the device button while plugging in USB — OR use `adb reboot edl` if ADB is available.

Verify detection on Linux:
```bash
lsusb | grep -i qualcomm
# Should show: Bus 00X Device 00X: ID 05c6:9008 Qualcomm, Inc. Gobi Wireless Modem (QDL mode)
```

### Shorthand for all edl commands below

```bash
EDL="sudo python3 ~/edl_repo/edl --loader ~/edl_repo/Loaders/qualcomm/factory/sdm845_sdm850_sda845/0008e0e100000000_afca69d4235117e5_fhprg.bin"
```

---

## Recovery Scenarios

### Scenario A: Wrong active slot (device fails to boot, shows "press any key")

The bootloader is pointing to a broken slot. Fix: set the other slot as active via EDL.

```bash
# Check current slot flags
$EDL printgpt --lun=4 | grep boot_

# Set active slot to b (slot_b = working AGNOS 17.2)
$EDL setactiveslot b

# Verify flags changed:
#   boot_a: Flags 0x003a... Active False
#   boot_b: Flags 0x006f... Active True

# Reset device
$EDL reset
```

To set slot_a as active instead: `$EDL setactiveslot a`

---

### Scenario B: Bad boot image on a slot

If a slot has a corrupt/incompatible boot partition (e.g., wrong kernel), restore it from the other slot.

```bash
# Read the known-good boot partition (e.g. boot_b) to a file
$EDL r boot_b /tmp/boot_b.img --memory=ufs

# Write it to the broken slot
$EDL w boot_a /tmp/boot_b.img --memory=ufs

# Then set the good slot as active (see Scenario A)
$EDL setactiveslot b
$EDL reset
```

> **Note on AVB:** Writing boot_b's image to boot_a may fail Android Verified Boot because `vbmeta_a` contains hashes for the original boot_a. If boot fails after this, you MUST also set the active slot to b (the slot whose vbmeta matches its boot image).

---

### Scenario C: Full slot recovery (restore slot_a to AGNOS 17.2)

If slot_a has an old AGNOS version and needs a full update, copy all partitions from slot_b:

```bash
# Copy boot partition
$EDL r boot_b /tmp/boot_b.img --memory=ufs
$EDL w boot_a /tmp/boot_b.img --memory=ufs

# Copy system partition (large! ~3-4 GB, takes several minutes)
$EDL r system_b /tmp/system_b.img --memory=ufs
$EDL w system_a /tmp/system_b.img --memory=ufs

# Copy vbmeta partition
$EDL r vbmeta_b /tmp/vbmeta_b.img --memory=ufs
$EDL w vbmeta_a /tmp/vbmeta_b.img --memory=ufs

# Now slot_a is a complete clone of slot_b — safe to set active
$EDL setactiveslot a
$EDL reset
```

---

## Flashing a Custom Kernel (Safe Method)

To avoid bricking, flash the custom `boot.img` to the **currently active slot** directly — no slot switch needed.

```bash
# From the Linux box (device must be in EDL mode):
# 1. Read current active boot for backup
$EDL r boot_b /tmp/boot_b_backup.img --memory=ufs

# 2. Flash custom kernel to active slot
$EDL w boot_b /path/to/custom_boot.img --memory=ufs

# 3. Reset and test
$EDL reset
```

If the custom kernel doesn't boot, restore from the backup:
```bash
$EDL w boot_b /tmp/boot_b_backup.img --memory=ufs
$EDL reset
```

> **Important:** After flashing a custom boot image via EDL, set `AGNOS_BOOT_UPDATE_VERSION` in `/data/openpilot/launch_env.sh` to a value different from the current `/data/agnos_boot_update_version` file on the device — otherwise `launch_chffrplus.sh` will re-flash the stock kernel over your custom one on next boot.

---

## Partition Layout Reference (Comma 3, SDM845, UFS)

| Partition | LUN | Notes |
|-----------|-----|-------|
| xbl_a / xbl_b | 0 | XBL bootloader — do not flash unless you know what you're doing |
| boot_a / boot_b | 4 | Kernel + ramdisk (64 MB each) |
| system_a / system_b | 4 | AGNOS root filesystem |
| vbmeta_a / vbmeta_b | 4 | Android Verified Boot metadata |
| misc | 4 | Boot control metadata |
| userdata | 4 | `/data` — openpilot install, logs, settings |

## Slot Flag Values (GPT attributes byte 6)

| Value | Meaning |
|-------|---------|
| `0x6f` | Active boot partition (priority=3, tries=5, success=1) |
| `0x3a` | Inactive boot partition (priority=2, tries=7, success=0) |

Active slot is the one with `0x6f` at byte 6 of the GPT flags field (bits 48-55).
