# DriveGuard Hardware Agent

A production-grade Linux daemon that connects physical wiping hardware to the DriveGuard platform.

## Requirements

- **OS:** Ubuntu 20.04+ / Debian 11+
- **Privileges:** Must run as root (raw disk access)
- **Network:** Outbound HTTPS to DriveGuard API

## Quick Install

```bash
sudo bash install.sh --token YOUR_LICENSE_TOKEN
```

Optional:
```bash
sudo bash install.sh --token YOUR_LICENSE_TOKEN --api-url https://your-self-hosted-api.com
```

## What Gets Installed

| Package          | Purpose                                        |
|------------------|------------------------------------------------|
| `smartmontools`  | Drive health checks (open-source HD Sentinel)  |
| `hdparm`         | ATA Secure Erase + NIST 800-88 (SATA/SAS)     |
| `nvme-cli`       | NVMe Secure Erase + Crypto Erase              |
| `sg3-utils`      | SAS/SCSI drive support                         |
| `fio`            | Drive surface/stress testing                   |
| `python3`        | Agent runtime                                  |

## Wipe Standards Supported

### NIST SP 800-88 Rev 2
- **Clear:** Single-pass zero overwrite
- **Purge:** ATA Secure Erase (SATA) or NVMe Format (NVMe)
- Compliant with NIST SP 800-88 Rev 2 guidelines for both HDD and SSD

### Enhanced Erase (Crypto Erase)
- ATA Security Erase Enhanced for Self-Encrypting Drives (SEDs)
- NVMe `--ses=2` (Cryptographic Erase) — destroys the encryption key
- Completes in seconds regardless of drive size

### DoD 5220.22-M
- 3-pass overwrite: 0x00 → 0xFF → random
- Followed by sample verification

## Health Checks

Uses `smartctl` (smartmontools) — open-source equivalent of HD Sentinel:

- Overall PASS/FAIL assessment
- All SMART attributes (Reallocated Sectors, Pending Sectors, etc.)
- Temperature, Power-On Hours
- Health score estimation (0–100%)
- SMART short/long self-test support

## Service Management

```bash
# Status
systemctl status driveguard-agent

# Logs
tail -f /var/log/driveguard/agent.log
tail -f /var/log/driveguard/agent.error.log

# Restart
sudo systemctl restart driveguard-agent

# Stop
sudo systemctl stop driveguard-agent
```

## Config File

Located at `/etc/driveguard/agent.conf`:

```ini
[agent]
license_token = YOUR_TOKEN
api_url       = https://api.driveguard.io
hostname      = my-wipe-station
system_id     = (auto-detected from DMI UUID)
poll_interval = 10
log_level     = INFO

[wipe]
default_standard = NIST_800_88
passes           = 1
verify           = true
```

## ATA Frozen State

If a drive is in **frozen** state (common on drives hot-plugged from a running system),
the agent will report this and the drive must be power-cycled to unfreeze before Secure Erase can proceed.

## File Structure

```
/opt/driveguard/
  driveguard_agent.py   — main daemon
  wipe_engine.py        — wipe/erase implementations
  venv/                 — Python virtual environment

/etc/driveguard/
  agent.conf            — configuration (chmod 600)

/var/log/driveguard/
  agent.log             — standard output
  agent.error.log       — errors
``
