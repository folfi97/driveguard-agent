#!/usr/bin/env python3
"""
DriveGuard Wipe Engine
Implements:
  - NIST 800-88 Rev 2 (Clear + Purge via ATA Secure Erase / NVMe Format)
  - Enhanced Erase (ATA Security Erase Enhanced — for self-encrypting drives)
  - DoD 5220.22-M (3-pass overwrite)
  - Verify (sample read-back confirming zero/random pattern)

All operations run as subprocess calls to hdparm / nvme-cli / dd.
Root privileges required.
"""

import os, re, time, json, logging, subprocess, threading, shutil
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _run(cmd: list, timeout: int = 7200) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    log.debug("RUN: %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _device_size_bytes(device: str) -> int:
    """Return device size in bytes via blockdev."""
    rc, out, _ = _run(["blockdev", "--getsize64", device])
    return int(out.strip()) if rc == 0 and out.strip().isdigit() else 0


def _is_nvme(device: str) -> bool:
    return "nvme" in device.lower()


def _is_sas(device: str) -> bool:
    rc, out, _ = _run(["lsblk", "-dno", "TRAN", device])
    return "sas" in out.lower()


def _unmount_all(device: str):
    """Unmount all partitions of a device before wiping."""
    rc, out, _ = _run(["lsblk", "-lno", "NAME,MOUNTPOINT", device])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1]:
            subprocess.run(["umount", "-lf", f"/dev/{parts[0]}"], capture_output=True)


def _dd_wipe(device: str, pattern: str, progress_cb: Optional[Callable] = None,
              progress_base: int = 0, progress_range: int = 100) -> bool:
    """
    Overwrite device with a pattern using dd.
    pattern: /dev/zero, /dev/urandom, or a hex byte (written via Python).
    Streams progress via dd status=progress output.
    """
    size = _device_size_bytes(device)
    bs   = 4 * 1024 * 1024  # 4 MiB blocks

    if pattern in ("/dev/zero", "/dev/urandom"):
        cmd = [
            "dd", f"if={pattern}", f"of={device}",
            f"bs={bs}", "conv=fsync,noerror", "status=progress"
        ]
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
        written = 0
        for line in proc.stderr:
            m = re.search(r"(\d+) bytes", line)
            if m and size > 0:
                written = int(m.group(1))
                pct = min(100, int(written / size * 100))
                if progress_cb:
                    progress_cb(progress_base + int(pct * progress_range / 100))
        proc.wait()
        return proc.returncode == 0
    else:
        # Fixed byte pattern via Python write
        byte_val = bytes([int(pattern, 16)])
        buf      = byte_val * bs
        written  = 0
        with open(device, "wb") as f:
            while written < size:
                chunk = min(bs, size - written)
                f.write(buf[:chunk])
                f.flush()
                written += chunk
                if size > 0 and progress_cb:
                    pct = min(100, int(written / size * 100))
                    progress_cb(progress_base + int(pct * progress_range / 100))
        return True


# ─── NIST 800-88 Rev 2 ────────────────────────────────────────────────────────

def wipe_nist_800_88(device: str, progress_cb: Optional[Callable] = None) -> dict:
    """
    NIST SP 800-88 Rev 2:
    - Clear:  single overwrite with zeros (satisfies 'Clear' for magnetic/SSD)
    - Purge:  ATA Secure Erase (SANITIZE or hdparm -security-erase-enhanced)
              NVMe: nvme format with cryptographic erase
    Returns result dict with success flag and details.
    """
    log.info("NIST 800-88 Rev2 — device: %s", device)
    _unmount_all(device)

    result = {
        "standard": "NIST_800_88",
        "device":   device,
        "steps":    [],
        "success":  False,
    }

    # ── Step 1: Clear (overwrite with zeros) ──────────────────────────────────
    log.info("Step 1/2: Clear (zero overwrite)...")
    if progress_cb: progress_cb(0)

    ok = _dd_wipe(device, "/dev/zero", progress_cb=progress_cb,
                   progress_base=0, progress_range=45)
    result["steps"].append({"step": "clear_zeros", "success": ok})
    if not ok:
        result["error"] = "Zero overwrite (Clear) failed"
        return result

    if progress_cb: progress_cb(45)

    # ── Step 2: Purge ─────────────────────────────────────────────────────────
    log.info("Step 2/2: Purge (Secure Erase)...")
    if _is_nvme(device):
        purge_ok, purge_detail = _nvme_format(device)
    else:
        purge_ok, purge_detail = _ata_secure_erase(device, enhanced=False)

    result["steps"].append({"step": "purge_secure_erase", "success": purge_ok, "detail": purge_detail})

    if not purge_ok:
        log.warning("Purge via Secure Erase failed (%s); falling back to second zero pass.", purge_detail)
        # Fallback: second overwrite pass
        ok2 = _dd_wipe(device, "/dev/zero", progress_cb=progress_cb,
                        progress_base=45, progress_range=45)
        result["steps"].append({"step": "purge_fallback_zeros", "success": ok2})
        if not ok2:
            result["error"] = "Both Purge methods failed"
            return result

    if progress_cb: progress_cb(100)
    result["success"] = True
    result["completed_at"] = _ts()
    log.info("NIST 800-88 complete on %s", device)
    ret
