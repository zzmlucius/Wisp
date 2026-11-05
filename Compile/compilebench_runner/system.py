"""Read-only Linux observations shared by all workloads."""
import hashlib
from functools import lru_cache
import json
import os
from pathlib import Path
import platform
import subprocess


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def digest(path):
    checksum = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def command_output(command, cwd=None):
    try:
        result = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=10)
        return {"command": command, "exit_code": result.returncode,
                "output": result.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "error": str(exc)}


def _read_desktop_profile(command):
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=2)
        if result.returncode == 0 and result.stdout.startswith('s '):
            profile = json.loads(result.stdout[2:])
            if isinstance(profile, str):
                return profile
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


@lru_cache(maxsize=1)
def _desktop_profile_command():
    # Select a read-only provider once; never infer a desktop mode from EPP.
    for service, path in (("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles"),
                          ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles")):
        command = ("busctl", "--system", "--timeout=1s", "get-property", service, path, service, "ActiveProfile")
        if _read_desktop_profile(command) is not None:
            return command
    return None


def power_state():
    state = {"platform_profile": read("/sys/firmware/acpi/platform_profile")}
    command = _desktop_profile_command()
    state["desktop_profile"] = _read_desktop_profile(command) if command else None
    state["desktop_profile_source"] = command[4] if command else None
    for profile in sorted(Path("/sys/class/platform-profile").glob("*/profile")):
        state[str(profile)] = read(profile)
    for supply in sorted(Path("/sys/class/power_supply").glob("*")):
        # Device batteries (mice, keyboards, etc.) do not power the host.
        kind = read(supply / "type") or ""
        if (read(supply / "scope") != "Device"
                and (kind in {"Mains", "Wireless", "UPS", "Apple_Brick_ID"} or kind.startswith("USB"))
                and (supply / "online").exists()):
            state[str(supply / "online")] = read(supply / "online")
    for cpu in sorted(Path("/sys/devices/system/cpu/cpufreq").glob("policy*")):
        for name in ("scaling_driver", "scaling_governor", "energy_performance_preference"):
            state[str(cpu / name)] = read(cpu / name)
    return state


def power_differences(expected, current, include_diagnostics=False):
    differences = {}
    for key in sorted(set(expected) | set(current)):
        before, after = expected.get(key), current.get(key)
        if before == after:
            continue
        diagnostic = key.endswith("/energy_performance_preference")
        if diagnostic and not include_diagnostics:
            continue
        differences[key] = {"before": before, "after": after,
                            "kind": "diagnostic" if diagnostic else "condition"}
    return differences


def power_error(expected, current, phase):
    changes = power_differences(expected, current)
    if not changes:
        return None
    detail = "; ".join(f"{key}: {change['before']!r} -> {change['after']!r}"
                       for key, change in changes.items())
    return f"供电/电源条件变化（{phase}）: {detail}"


def sensor_path(configured=None):
    if configured:
        path = Path(configured).resolve()
        float(path.read_text())
        return path
    candidates = []
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        if read(hwmon / "name") in {"coretemp", "k10temp", "zenpower", "cpu_thermal"}:
            for path in sorted(hwmon.glob("temp*_input")):
                label = read(path.with_name(path.name.replace("_input", "_label"))) or ""
                candidates.append((0 if label.startswith(("Package", "Tctl", "Tdie")) else 1,
                                   str(path.resolve())))
    if candidates:
        return Path(sorted(candidates)[0][1])
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        if read(zone / "type") in {"x86_pkg_temp", "cpu-thermal"}:
            return (zone / "temp").resolve()
    return None


def temperature(sensor):
    value = read(sensor) if sensor else None
    try:
        return float(value) / 1000 if value is not None else None
    except ValueError:
        return None


def machine_info():
    return {"uname": list(platform.uname()), "os_release": read("/etc/os-release"),
            "cpuinfo": read("/proc/cpuinfo"), "meminfo": read("/proc/meminfo"),
            "available_cpus": sorted(os.sched_getaffinity(0)), "uid": os.getuid(),
            "python": platform.python_version(), "power": power_state()}


def write_json(path, data):
    path = Path(path)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temp.replace(path)
