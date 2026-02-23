#!/usr/bin/env bash

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1

# models get lower priority than ui
# - ui is ~5ms
# - modeld is 20ms
# - DM is 10ms
# in order to run ui at 60fps (16.67ms), we need to allow
# it to preempt the model workloads. we have enough
# headroom for this until ui is moved to the CPU.
export QCOM_PRIORITY=12

if [ -z "$AGNOS_VERSION" ]; then
  export AGNOS_VERSION="16"
fi

# Bump this when boot partition content should be auto-applied on reboot.
# This is intentionally separate from AGNOS_VERSION to avoid full-system update loops.
if [ -z "$AGNOS_BOOT_UPDATE_VERSION" ]; then
  export AGNOS_BOOT_UPDATE_VERSION="16.0.3"
fi

export STAGING_ROOT="/data/safe_staging"
