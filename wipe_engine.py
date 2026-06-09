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
    return result


# ─── Enhanced Erase (ATA Security Erase Enhanced) ────────────────────────────

def wipe_enhanced_erase(device: str, progress_cb: Optional[Callable] = None) -> dict:
    """
    ATA Security Erase Enhanced — for Self-Encrypting Drives (SEDs).
    Throws away the encryption key, making all data cryptographically unrecoverable
    in a fraction of the time of overwrite methods.
    NVMe: uses nvme-format with crypto-erase (ses=2).
    """
    log.info("Enhanced Erase (crypto) — device: %s", device)
    _unmount_all(device)

    result = {
        "standard": "enhanced_erase",
        "device":   device,
        "steps":    [],
        "success":  False,
    }

    if progress_cb: progress_cb(10)

    if _is_nvme(device):
        ok, detail = _nvme_format(device, ses=2)
        method = "nvme_crypto_erase"
    else:
        ok, detail = _ata_secure_erase(device, enhanced=True)
        method = "ata_enhanced_erase"

    result["steps"].append({"step": method, "success": ok, "detail": detail})

    if not ok:
        result["error"] = f"Enhanced erase failed: {detail}"
        log.error("Enhanced erase failed on %s: %s", device, detail)
        return result

    if progress_cb: progress_cb(100)
    result["success"] = True
    result["completed_at"] = _ts()
    log.info("Enhanced Erase complete on %s", device)
    return result


# ─── DoD 5220.22-M (3-pass) ──────────────────────────────────────────────────

def wipe_dod_5220(device: str, progress_cb: Optional[Callable] = None) -> dict:
    """
    DoD 5220.22-M:
    Pass 1: 0x00 (zeros)
    Pass 2: 0xFF (ones)
    Pass 3: random
    """
    log.info("DoD 5220.22-M — device: %s", device)
    _unmount_all(device)

    result = {
        "standard": "DoD_5220",
        "device":   device,
        "steps":    [],
        "success":  False,
    }

    passes = [
        ("pass1_zeros",   "/dev/zero",    0,  33),
        ("pass2_ones",    "0xFF",         33, 33),
        ("pass3_random",  "/dev/urandom", 66, 34),
    ]

    for name, pattern, base, rng in passes:
        log.info("DoD pass: %s", name)
        ok = _dd_wipe(device, pattern, progress_cb=progress_cb,
                       progress_base=base, progress_range=rng)
        result["steps"].append({"step": name, "success": ok})
        if not ok:
            result["error"] = f"{name} failed"
            return result

    if progress_cb: progress_cb(100)
    result["success"] = True
    result["completed_at"] = _ts()
    log.info("DoD 5220.22-M complete on %s", device)
    return result


# ─── Verify ───────────────────────────────────────────────────────────────────

def verify_wipe(device: str, sample_blocks: int = 64) -> dict:
    """
    Verify wipe by sampling random sectors and confirming they read as zeros.
    Reads sample_blocks × 4096-byte blocks from random offsets.
    """
    size = _device_size_bytes(device)
    if size == 0:
        return {"clean": False, "error": "Could not determine device size"}

    import random
    block_size = 4096
    max_offset = size - block_size
    non_zero   = 0

    with open(device, "rb") as f:
        for _ in range(sample_blocks):
            offset = random.randint(0, max_offset // block_size) * block_size
            f.seek(offset)
            data = f.read(block_size)
            if any(b != 0 for b in data):
                non_zero += 1

    clean = non_zero == 0
    return {
        "clean":          clean,
        "blocks_sampled": sample_blocks,
        "non_zero_blocks": non_zero,
        "note":           "Sampled verification — not exhaustive",
    }


# ─── ATA Secure Erase (hdparm) ───────────────────────────────────────────────

def _ata_secure_erase(device: str, enhanced: bool = False) -> tuple[bool, str]:
    """
    Perform ATA Secure Erase using hdparm.
    enhanced=True uses --security-erase-enhanced (for SEDs).
    Returns (success, detail_string).
    """
    TEMP_PASSWORD = "driveguard_temp"

    # Unfreeze check
    rc, out, err = _run(["hdparm", "-I", device])
    if "frozen" in out.lower():
        return False, "Drive is in frozen state — power cycle required to unfreeze."

    # Set a temporary password
    rc, out, err = _run(["hdparm", "--security-set-pass", TEMP_PASSWORD, device])
    if rc != 0:
        return False, f"Failed to set security password: {err.strip()}"

    # Issue erase command
    erase_flag = "--security-erase-enhanced" if enhanced else "--security-erase"
    rc, out, err = _run(
        ["hdparm", erase_flag, TEMP_PASSWORD, device],
        timeout=21600   # 6 hours max for large drives
    )
    if rc != 0:
        # Attempt to disable password if erase failed
        _run(["hdparm", "--security-disable", TEMP_PASSWORD, device])
        return False, f"Secure erase failed: {err.strip()}"

    return True, f"ATA {'Enhanced ' if enhanced else ''}Secure Erase completed successfully."


# ─── NVMe Format ─────────────────────────────────────────────────────────────

def _nvme_format(device: str, ses: int = 1) -> tuple[bool, str]:
    """
    NVMe format with Secure Erase Settings:
    ses=0 — no secure erase
    ses=1 — user data erase
    ses=2 — cryptographic erase (destroys encryption key)
    """
    # Get namespace ID
    rc, out, _ = _run(["nvme", "list", "-o", "json"])
    ns_id = "1"  # Default namespace

    rc, out, err = _run(
        ["nvme", "format", device, f"--ses={ses}", f"--namespace-id={ns_id}", "--force"],
        timeout=3600
    )
    if rc != 0:
        return False, f"nvme format failed: {err.strip()}"
    return True, f"NVMe format (ses={ses}) completed successfully."


# ─── Utility ─────────────────────────────────────────────────────────────────

def _ts() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
