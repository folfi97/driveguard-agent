#!/usr/bin/env python3
"""
DriveGuard Hardware Agent v2
Communicates with the agentApi Base44 backend function.
API contract: POST /functions/agentApi  with JSON body { "action": "...", ... }
Authentication: api_key header = Organization.license_token
"""

import os, sys, time, json, logging, socket, argparse, configparser, subprocess, threading
from datetime import datetime, timezone
from pathlib import Path

import requests
import psutil

# ─── Bootstrap ────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="DriveGuard Hardware Agent")
    p.add_argument("--config", default="/etc/driveguard/agent.conf")
    p.add_argument("--once",   action="store_true", help="Run one poll cycle then exit")
    return p.parse_args()

def load_config(path):
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        raise FileNotFoundError(f"Config not found: {path}")
    return cfg

def setup_logging(level_str):
    level = getattr(logging, level_str.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

# ─── API Client ───────────────────────────────────────────────────────────────
class DriveGuardAPI:
    """
    Thin wrapper around the agentApi Base44 backend function.
    All calls POST to /functions/agentApi with api_key header.
    """
    def __init__(self, api_url: str, token: str):
        # api_url should be the Base44 app root, e.g. https://drive-guard.base44.app
        self.endpoint = api_url.rstrip("/") + "/functions/agentApi"
        self.session  = requests.Session()
        self.session.headers.update({
            "api_key":      token,
            "Content-Type": "application/json",
            "User-Agent":   "DriveGuard-Agent/2.0",
        })
        self.timeout = 30

    def _call(self, payload: dict) -> dict:
        r = self.session.post(self.endpoint, json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"API error: {data.get('error', 'unknown')}")
        return data

    def ping(self) -> dict:
        return self._call({"action": "ping"})

    def register_system(self, system: dict) -> dict:
        return self._call({"action": "register_system", "system": system})

    def heartbeat(self, system_id: str, system: dict) -> dict:
        return self._call({"action": "heartbeat", "system_id": system_id, "system": system})

    def get_jobs(self, system_id: str) -> list:
        data = self._call({"action": "get_jobs", "system_id": system_id})
        return data.get("jobs", [])

    def update_job(self, system_id: str, job_id: str, updates: dict) -> dict:
        return self._call({"action": "update_job", "system_id": system_id, "job_id": job_id, "updates": updates})

    def report_inventory(self, system_id: str, drives: list) -> dict:
        return self._call({"action": "report_inventory", "system_id": system_id, "drives": drives})

    def report_drive_change(self, system_id: str, change_type: str, drive: dict) -> dict:
        return self._call({"action": "report_drive_change", "system_id": system_id, "change_type": change_type, "drive": drive})

    def check_for_updates(self) -> dict:
        """Check for available agent updates."""
        update_endpoint = self.endpoint.replace("/functions/agentApi", "/functions/agentUpdate")
        r = self.session.post(update_endpoint, json={"action": "get_latest"}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def download_file(self, filename: str) -> str:
        """Download a specific agent file."""
        update_endpoint = self.endpoint.replace("/functions/agentApi", "/functions/agentUpdate")
        r = self.session.post(update_endpoint, json={"action": "download_file", "filename": filename}, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("content", "")

# ─── Drive Discovery ──────────────────────────────────────────────────────────
def get_os_drive() -> str:
    """Detect the OS/system drive by checking root mount point."""
    try:
        result = subprocess.run(["df", "/"], capture_output=True, text=True)
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                parts = lines[1].split()
                if parts:
                    dev = parts[0]
                    if dev.startswith('/dev/'):
                        return dev.rstrip('0123456789')
    except Exception:
        pass
    return ""

def list_drives() -> list:
    """Return all block devices that are whole disks (not partitions), excluding OS drive."""
    drives = []
    os_drive = get_os_drive()
    logging.debug("Detected OS drive: %s", os_drive or "none")
    
    result = subprocess.run(
        ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,ROTA,TRAN,MODEL,SERIAL,VENDOR,PHY-SEC,LOG-SEC"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        logging.warning("lsblk failed: %s", result.stderr)
        return drives

    data = json.loads(result.stdout)
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        
        device_path = f"/dev/{dev['name']}"
        if os_drive and device_path.startswith(os_drive):
            logging.info("Skipping OS drive: %s", device_path)
            continue
        
        transport = (dev.get("tran") or "unknown").upper()
        interface = "SATA"
        if "NVME" in transport or "NVME" in (dev.get("name","")).upper():
            interface = "NVMe"
        elif transport in ("SAS",):
            interface = "SAS"
        elif transport in ("USB",):
            interface = "USB"

        size_bytes = parse_size(dev.get("size", "0"))
        drives.append({
            "device":       f"/dev/{dev['name']}",
            "name":         dev.get("name", ""),
            "model":        (dev.get("model") or "").strip(),
            "serial_number": (dev.get("serial") or "").strip(),
            "manufacturer": (dev.get("vendor") or "").strip(),
            "capacity_gb":  round(size_bytes / 1e9, 1),
            "interface":    interface,
            "rotational":   dev.get("rota") == "1",
            "sector_size":  int(dev.get("phy-sec") or 512),
        })
    return drives

def parse_size(size_str: str) -> int:
    import re
    size_str = size_str.strip().upper()
    match = re.match(r"^([\d.]+)\s*([KMGTPE]?)B?$", size_str)
    if not match:
        return 0
    num  = float(match.group(1))
    unit = match.group(2)
    multipliers = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5, "E": 1024**6}
    return int(num * multipliers.get(unit, 1))

# ─── Health Check ─────────────────────────────────────────────────────────────
def run_health_check(device: str) -> dict:
    result = {"device": device, "overall": "UNKNOWN", "attributes": {}}

    health = subprocess.run(["smartctl", "-H", device], capture_output=True, text=True)
    if "PASSED" in health.stdout:
        result["overall"] = "PASS"
    elif "FAILED" in health.stdout:
        result["overall"] = "FAIL"

    smart_json = subprocess.run(
        ["smartctl", "-a", "--json=c", device], capture_output=True, text=True
    )
    if smart_json.returncode in (0, 4):
        try:
            smart_data = json.loads(smart_json.stdout)
            result["temperature_c"]      = smart_data.get("temperature", {}).get("current")
            result["power_on_hours"]     = smart_data.get("power_on_time", {}).get("hours")
            result["smart_data"]         = smart_data
            ata = smart_data.get("ata_smart_attributes", {})
            for attr in ata.get("table", []):
                result["attributes"][attr["name"]] = {
                    "id":    attr["id"],
                    "value": attr["value"],
                    "worst": attr["worst"],
                    "raw":   attr.get("raw", {}).get("value", 0),
                }
            result["reallocated_sectors"] = result["attributes"].get("Reallocated_Sector_Ct", {}).get("raw", 0)
            result["pending_sectors"]    = result["attributes"].get("Current_Pending_Sector", {}).get("raw", 0)
        except json.JSONDecodeError:
            pass
    return result

# ─── Wipe Engine ─────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wipe_engine import wipe_nist_800_88, wipe_enhanced_erase, wipe_dod_5220, verify_wipe

# ─── Job Runner ───────────────────────────────────────────────────────────────
class JobRunner:
    def __init__(self, api: DriveGuardAPI, system_id: str):
        self.api       = api
        self.system_id = system_id
        self.active    = {}
        self._lock     = threading.Lock()

    def dispatch(self, job: dict):
        job_id = job["id"]
        with self._lock:
            if job_id in self.active:
                return
            t = threading.Thread(target=self._run_job, args=(job,), daemon=True)
            self.active[job_id] = t
            t.start()

    def _run_job(self, job: dict):
        job_id   = job["id"]
        device   = job.get("device")
        job_type = job.get("job_type", "full")
        standard = job.get("wipe_standard", "NIST_800_88")

        if not device:
            self._fail(job_id, "No device assigned to this job")
            return

        logging.info("Starting job %s  type=%s  device=%s  standard=%s", job_id, job_type, device, standard)
        self._update(job_id, {"status": "running", "started_at": _now()})

        # Health check
        if job_type in ("test", "full", "data_check"):
            try:
                health = run_health_check(device)
                self._update(job_id, {
                    "result_smart": "pass" if health["overall"] == "PASS" else "fail",
                    "smart_data":   health.get("smart_data"),
                    "progress_percent": 20,
                })
            except Exception as e:
                logging.warning("Health check failed: %s", e)

        # Wipe
        if job_type in ("wipe", "full"):
            def _progress(pct):
                self._update(job_id, {"progress_percent": 20 + int(pct * 0.7)})
            try:
                if standard == "NIST_800_88":
                    wipe_result = wipe_nist_800_88(device, progress_cb=_progress)
                elif standard in ("Secure_Erase", "enhanced_erase"):
                    wipe_result = wipe_enhanced_erase(device, progress_cb=_progress)
                elif standard == "DoD_5220":
                    wipe_result = wipe_dod_5220(device, progress_cb=_progress)
                else:
                    wipe_result = wipe_nist_800_88(device, progress_cb=_progress)

                self._update(job_id, {
                    "result_wipe":    "pass" if wipe_result["success"] else "fail",
                    "progress_percent": 90,
                })
                if not wipe_result["success"]:
                    self._fail(job_id, wipe_result.get("error", "Wipe failed"))
                    return
            except Exception as e:
                self._fail(job_id, str(e))
                return

        # Verify
        if job_type in ("wipe", "full"):
            try:
                verified = verify_wipe(device)
                self._update(job_id, {
                    "result_data_check": "pass" if verified["clean"] else "fail",
                    "progress_percent":  100,
                })
            except Exception as e:
                logging.warning("Verify step failed: %s", e)

        self._update(job_id, {
            "status":           "completed",
            "progress_percent": 100,
            "completed_at":     _now(),
        })
        logging.info("Job %s completed.", job_id)
        with self._lock:
            self.active.pop(job_id, None)

    def _update(self, job_id, updates):
        try:
            self.api.update_job(self.system_id, job_id, updates)
        except Exception as e:
            logging.warning("Failed to update job %s: %s", job_id, e)

    def _fail(self, job_id, msg):
        logging.error("Job %s FAILED: %s", job_id, msg)
        self._update(job_id, {"status": "failed", "error_message": msg, "completed_at": _now()})
        with self._lock:
            self.active.pop(job_id, None)

# ─── Helpers ──────────────────────────────────────────────────────────────────
def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "unknown"

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_machine_id() -> str:
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            return Path(path).read_text().strip()
        except Exception:
            pass
    return socket.gethostname()

# ─── Update Manager ──────────────────────────────────────────────────────────
class UpdateManager:
    def __init__(self, agent_dir: str = "/opt/driveguard"):
        self.agent_dir = agent_dir
        self.backup_dir = f"{agent_dir}/.backup"
        self.current_version = None
        self.latest_version = None

    def get_current_version(self) -> str:
        """Read current version from agent config or file."""
        try:
            result = subprocess.run(
                ["grep", "AGENT_VERSION", f"{self.agent_dir}/driveguard_agent.py"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                import re
                match = re.search(r'AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', result.stdout)
                if match:
                    return match.group(1)
        except Exception:
            pass
        return "2.0.0"

    def backup_current(self, filename: str):
        """Backup current file, keeping only one backup."""
        current_file = f"{self.agent_dir}/{filename}"
        backup_file = f"{self.backup_dir}/{filename}.bak"
        
        if not Path(self.backup_dir).exists():
            Path(self.backup_dir).mkdir(parents=True, exist_ok=True)
        
        if Path(current_file).exists():
            import shutil
            shutil.copy2(current_file, backup_file)
            logging.info("Backed up %s", filename)

    def apply_update(self, filename: str, content: str) -> bool:
        """Write updated file and make executable if needed."""
        try:
            target = f"{self.agent_dir}/{filename}"
            
            # Backup current version first
            self.backup_current(filename)
            
            # Write new content
            with open(target, 'w') as f:
                f.write(content)
            
            # Make executable if Python/shell script
            if filename in ["driveguard_agent.py", "install.sh"]:
                import os
                os.chmod(target, 0o755)
            
            logging.info("Updated %s", filename)
            return True
        except Exception as e:
            logging.error("Failed to update %s: %s", filename, e)
            return False

    def check_and_apply_updates(self, api: 'DriveGuardAPI') -> bool:
        """Check for updates and apply if available."""
        try:
            latest = api.check_for_updates()
            latest_version = latest.get("version")
            current_version = self.get_current_version()
            
            if latest_version and latest_version > current_version:
                logging.info("Update available: %s -> %s", current_version, latest_version)
                
                files_to_update = ["driveguard_agent.py", "wipe_engine.py", "install.sh"]
                for filename in files_to_update:
                    try:
                        content = api.download_file(filename)
                        if content:
                            self.apply_update(filename, content)
                    except Exception as e:
                        logging.warning("Failed to download %s: %s", filename, e)
                
                logging.info("Update completed successfully")
                return True
        except Exception as e:
            logging.debug("Update check failed (will retry later): %s", e)
        
        return False

# ─── Drive Change Detector ───────────────────────────────────────────────────
class DriveChangeDetector:
    def __init__(self):
        self.previous_drives = {}
    
    def detect_changes(self, current_drives: list, api: DriveGuardAPI, system_id: str):
        """Detect plugged/unplugged drives and report immediately."""
        current_map = {d["device"]: d for d in current_drives}
        
        # Detect plugged drives (new devices)
        for device, drive in current_map.items():
            if device not in self.previous_drives:
                logging.info("Drive plugged: %s", device)
                try:
                    api.report_drive_change(system_id, "plugged", drive)
                except Exception as e:
                    logging.warning("Failed to report plugged drive: %s", e)
        
        # Detect unplugged drives (removed devices)
        for device in self.previous_drives:
            if device not in current_map:
                logging.info("Drive unplugged: %s", device)
                try:
                    api.report_drive_change(system_id, "unplugged", {"device": device})
                except Exception as e:
                    logging.warning("Failed to report unplugged drive: %s", e)
        
        self.previous_drives = current_map

# ─── Main loop ────────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    cfg  = load_config(args.config)
    setup_logging(cfg["agent"].get("log_level", "INFO"))

    token   = cfg["agent"]["license_token"]
    api_url = cfg["agent"]["api_url"]
    poll    = int(cfg["agent"].get("poll_interval", 10))
    hostname = cfg["agent"].get("hostname", socket.gethostname())

    api = DriveGuardAPI(api_url, token)
    change_detector = DriveChangeDetector()
    update_manager = UpdateManager()

    logging.info("DriveGuard Agent starting — endpoint: %s", api.endpoint)

    # Verify connectivity
    try:
        api.ping()
        logging.info("API reachable.")
    except Exception as e:
        logging.warning("Initial ping failed (will retry): %s", e)

    # Register system and get system_id
    system_id = cfg["agent"].get("system_id", "")
    try:
        reg = api.register_system({
            "name":          hostname,
            "hostname":      hostname,
            "serial_number": get_machine_id(),
            "agent_version": "2.0.1",
            "ip_address":    get_local_ip(),
            "drive_bays":    len(list_drives()),
            "status":        "connected",
        })
        system_id = reg.get("system_id", system_id)
        logging.info("Registered. system_id=%s", system_id)
    except Exception as e:
        logging.warning("Registration failed (will retry on next poll): %s", e)

    if not system_id:
        logging.error("No system_id — cannot poll for jobs. Check your license token.")
        if args.once:
            return
        time.sleep(30)

    runner = JobRunner(api, system_id)

    update_check_counter = 0
    while True:
        try:
            # Check for updates every 60 polls (default 10s interval = ~10 minutes)
            update_check_counter += 1
            if update_check_counter >= 60:
                update_manager.check_and_apply_updates(api)
                update_check_counter = 0

            drives = list_drives()
            
            # Detect and report drive changes in real-time
            change_detector.detect_changes(drives, api, system_id)
            
            hb_response = api.heartbeat(system_id, {
                "hostname":      hostname,
                "agent_version": "2.0.1",
                "ip_address":    get_local_ip(),
                "drive_bays":    len(drives),
                "status":        "busy" if runner.active else "idle",
            })

            # Heartbeat returns pending_jobs directly
            jobs = hb_response.get("pending_jobs", [])
            for job in jobs:
                runner.dispatch(job)

            # Also report detected drives as inventory
            if drives:
                try:
                    api.report_inventory(system_id, drives)
                except Exception as e:
                    logging.debug("Inventory report failed: %s", e)

        except requests.exceptions.ConnectionError:
            logging.warning("API unreachable, retrying in %ds...", poll)
        except Exception as e:
            logging.error("Poll error: %s", e)

        if args.once:
            break
        time.sleep(poll)

if __name__ == "__main__":
    main()
