#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Collect a read-only C792/PiKVM/BliKVM audio inventory (Python 3.9+).

This is not an audio test. It never records or plays audio, requests a browser
microphone, opens media devices, changes configfs, or starts/restarts services.
Run without sudo first. Missing tools and insufficient permissions are reported
as inventory gaps, not failures of the hardware. stdout is one JSON document.

Configuration extraction deliberately supports only a small, safe subset of
Janus JCFG and block-style YAML. It does not resolve the effective configuration.
Raw configuration, command stderr, logs, network settings, and authentication
settings are not collected. Local device/card descriptions from ALSA are
included; their user-assigned names can themselves contain private information.
"""

import argparse
import datetime
import errno
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


MAX_FILE_BYTES = 256 * 1024
MAX_OUTPUT_CHARS = 16 * 1024
COMMAND_TIMEOUT = 3.0
BOOT_PATHS = ("/boot/config.txt", "/boot/firmware/config.txt", "/boot/efi/config.txt")
JANUS_PATHS = (
    "/etc/kvmd/janus/janus.plugin.ustreamer.jcfg",
    "/etc/janus/janus.plugin.ustreamer.jcfg",
    "/usr/local/etc/janus/janus.plugin.ustreamer.jcfg",
    "/opt/janus/etc/janus/janus.plugin.ustreamer.jcfg",
    "/opt/janus/lib/janus/configs/janus.plugin.ustreamer.jcfg",
    "/usr/blikvm/lib/pi/janus_configs/janus.plugin.ustreamer.jcfg",
    "/mnt/exec/release/lib/pi/janus_configs/janus.plugin.ustreamer.jcfg",
)
PACKAGE_NAMES = (
    "kvmd", "ustreamer", "janus-gateway-pikvm", "janus-gateway", "janus",
    "alsa-utils", "blikvm", "linux-rpi", "linux-rpi4", "linux-rpi-legacy",
)
JANUS_KEYS = {
    "acap": {"device", "tc358743", "sampling_rate"},
    "aplay": {"device"},
    "video": {"sink"},
    "vplay": {"device", "sink"},
}
AUDIO_FLAGS = {
    "otg.devices.audio.enabled", "otg.devices.audio.start",
    "otg.devices.audio.speakers.enabled", "otg.devices.audio.mic.enabled",
}
UAC2_FIELDS = ("p_chmask", "p_srate", "p_ssize", "c_chmask", "c_srate", "c_ssize")
Result = Dict[str, Any]
Runner = Callable[[Sequence[str]], Result]


def error_status(error: OSError) -> str:
    if isinstance(error, FileNotFoundError) or error.errno == errno.ENOENT:
        return "missing"
    if isinstance(error, PermissionError) or error.errno in (errno.EACCES, errno.EPERM):
        return "permission_denied"
    return "io_error"


def local_path(root: Path, path: str) -> Path:
    """A test root allows exercising inventory without privileged operations."""
    return root / path.lstrip("/")


def read_text(path: Path) -> Result:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return {"status": "unsupported_file_type"}
        with path.open("rb") as source:
            data = source.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            return {"status": "too_large"}
        return {"status": "ok", "text": data.decode("utf-8", errors="replace").replace("\x00", "")}
    except OSError as error:
        return {"status": error_status(error)}


def run_command(argv: Sequence[str]) -> Result:
    """Run only fixed inventory commands. Never retain stderr or partial output."""
    environment = {
        "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
        "LANG": "C", "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace", check=False,
            timeout=COMMAND_TIMEOUT, env=environment, cwd="/",
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "timeout_seconds": COMMAND_TIMEOUT}
    except OSError as error:
        return {"status": error_status(error)}
    return {
        "status": "ok" if result.returncode == 0 else "nonzero_exit",
        "exit_code": result.returncode,
        "stdout": result.stdout[:MAX_OUTPUT_CHARS].strip(),
        "output_truncated": len(result.stdout) > MAX_OUTPUT_CHARS,
    }


def package_inventory(runner: Runner) -> Result:
    output: Result = {}
    commands = {
        "pacman": ("pacman", "-Q", *PACKAGE_NAMES),
        "dpkg-query": ("dpkg-query", "-W", "-f=${Package}\t${Version}\n", *PACKAGE_NAMES),
    }
    for manager, argv in commands.items():
        result = runner(argv)
        entry = {key: value for key, value in result.items() if key != "stdout"}
        packages = {}
        for line in result.get("stdout", "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in PACKAGE_NAMES and re.fullmatch(r"[A-Za-z0-9.+:~_-]{1,128}", parts[1]):
                packages[parts[0]] = parts[1]
        entry["packages"] = packages
        output[manager] = entry
    return output


def overlay_inventory(root: Path) -> List[Result]:
    output = []
    pattern = re.compile(r"^\s*(?P<comment>#\s*)?dtoverlay\s*=\s*tc358743-audio(?:\s|,|$)")
    for name in BOOT_PATHS:
        result = read_text(local_path(root, name))
        matches = []
        conditional = False
        for number, line in enumerate(result.get("text", "").splitlines(), 1):
            if re.match(r"^\s*\[", line):
                conditional = not bool(re.match(r"^\s*\[all\]\s*(?:#.*)?$", line))
            match = pattern.match(line)
            if match:
                # Never include parameters/comments: even a matching line may
                # carry unrelated private information after its directive.
                matches.append({
                    "line": number, "directive": "dtoverlay=tc358743-audio",
                    "commented_out": bool(match.group("comment")),
                    "under_conditional_section": conditional,
                })
        output.append({"path": name, "status": result["status"], "matches": matches})
    return output


def strip_comments(source: str) -> str:
    """Remove JCFG/YAML comments without treating quoted text as a comment."""
    output = []
    index = 0
    quote = ""
    while index < len(source):
        char = source[index]
        if quote:
            output.append(char)
            if char == "\\" and index + 1 < len(source):
                index += 1
                output.append(source[index])
            elif char == quote:
                quote = ""
        elif char in ("\"", "'"):
            quote = char
            output.append(char)
        elif char == "#" or source.startswith("//", index):
            while index < len(source) and source[index] != "\n":
                index += 1
            if index < len(source):
                output.append("\n")
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end < 0 else end + 2
            output.append(" " + "\n" * source[index:end].count("\n"))
            index = end - 1
        else:
            output.append(char)
        index += 1
    return "".join(output)


def safe_media_name(key: str, value: str) -> bool:
    if len(value) > 160:
        return False
    if key == "sink":
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(?:::[A-Za-z][A-Za-z0-9_-]*)*", value))
    if re.fullmatch(r"/dev/[A-Za-z0-9_./:-]+", value) and ".." not in value.split("/"):
        return True
    if key == "tc358743":
        return False
    return bool(re.fullmatch(
        r"(?:(?:plug)?hw:(?:CARD=)?[A-Za-z0-9_-]+(?:,(?:(?:DEV|SUBDEV)=)?[0-9]+){0,2}"
        r"|(?:default|sysdefault|front|surround[0-9]{2}|iec958|spdif|null)(?::CARD=[A-Za-z0-9_-]+)?)",
        value,
    ))


def parse_janus_names(source: str) -> Result:
    """Extract top-level, allowlisted media settings; never return raw text."""
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_][A-Za-z0-9_-]*|[0-9]+|[^\s]', strip_comments(source))
    names: Result = {}
    redacted = []
    duplicates = []
    section: Optional[str] = None
    depth = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if depth == 0 and index + 2 < len(tokens) and tokens[index + 1:index + 3] == [":", "{"]:
            section = token if token in JANUS_KEYS else None
            depth = 1
            index += 3
            continue
        if token == "{":
            depth += 1
        elif token == "}":
            depth = max(0, depth - 1)
            if not depth:
                section = None
        elif section and depth == 1 and token in JANUS_KEYS[section] and index + 2 < len(tokens) and tokens[index + 1] == "=":
            raw = tokens[index + 2]
            value = ""
            try:
                if raw.startswith('"'):
                    value = json.loads(raw)
                elif raw.startswith("'") and "\\" not in raw:
                    value = raw[1:-1]
            except (ValueError, TypeError):
                pass
            path = section + "." + token
            fields = names.setdefault(section, {})
            if token in fields:
                duplicates.append(path)
            if token == "sampling_rate" and re.fullmatch(r"[0-9]{1,6}", raw) and 0 <= int(raw) <= 768000:
                fields[token] = int(raw)
            elif token != "sampling_rate" and isinstance(value, str) and safe_media_name(token, value):
                fields[token] = value
            else:
                fields[token] = "[redacted: unsupported media name]"
                redacted.append(path)
            index += 3
            continue
        index += 1
    return {"names": names, "redacted_fields": redacted, "duplicate_keys": duplicates, "effective_config_resolved": False}


def parse_audio_flags(source: str) -> Result:
    """Read explicit booleans in ordinary block YAML, without evaluating YAML."""
    stack: List[Tuple[int, Optional[str]]] = []
    flags = []
    unsupported = []
    booleans = {"true": True, "yes": True, "on": True, "false": False, "no": False, "off": False}
    for number, line in enumerate(strip_comments(source).splitlines(), 1):
        if not line.strip() or line.strip() in ("---", "..."):
            continue
        indent = len(line) - len(line.lstrip(" "))
        while stack and indent <= stack[-1][0]:
            stack.pop()
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?", line.lstrip(" "))
        if not match:
            stack.append((indent, None))
            continue
        key, value = match.groups()
        path = ".".join(item[1] or "<unsupported>" for item in stack)
        path = path + "." + key if path else key
        if path in AUDIO_FLAGS and value is not None:
            normalized = value.strip().lower()
            if normalized in booleans:
                flags.append({"path": path, "value": booleans[normalized], "line": number})
            else:
                unsupported.append({"path": path, "line": number})
        stack.append((indent, key if value is None or not value.strip() else None))
    return {"explicit_flags": flags, "unsupported_values": unsupported, "effective_config_resolved": False}


def janus_inventory(root: Path, extra_paths: Sequence[str]) -> List[Result]:
    output = []
    for name in dict.fromkeys((*JANUS_PATHS, *extra_paths)):
        result = read_text(local_path(root, name))
        entry = {"path": name, "status": result["status"]}
        if result["status"] == "ok":
            entry.update(parse_janus_names(result["text"]))
        output.append(entry)
    return output


def list_directory(path: Path) -> Tuple[str, List[Path]]:
    try:
        return "ok", sorted(path.iterdir())
    except OSError as error:
        return error_status(error), []


def kvmd_inventory(root: Path) -> List[Result]:
    paths = ["/usr/lib/kvmd/main.yaml", "/etc/kvmd/main.yaml", "/etc/kvmd/override.yaml"]
    directory = "/etc/kvmd/override.d"
    directory_status, entries = list_directory(local_path(root, directory))
    paths.extend(directory + "/" + path.name for path in entries if re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", path.name))
    output: List[Result] = [{"path": directory, "status": directory_status}]
    for name in paths:
        result = read_text(local_path(root, name))
        entry = {"path": name, "status": result["status"]}
        if result["status"] == "ok":
            entry.update(parse_audio_flags(result["text"]))
        output.append(entry)
    return output


def device_info(root: Path, name: str) -> Result:
    path = local_path(root, name)
    entry: Result = {"path": name}
    try:
        link = path.lstat()
        info = path.stat()
        kind = "character_device" if stat.S_ISCHR(info.st_mode) else "other"
        if stat.S_ISREG(info.st_mode):
            kind = "regular_file"
        entry.update({
            "status": "ok", "type": kind, "symlink": stat.S_ISLNK(link.st_mode),
            "mode": format(stat.S_IMODE(info.st_mode), "04o"),
            "uid": info.st_uid, "gid": info.st_gid,
            "readable_by_invoking_user": os.access(path, os.R_OK),
            "writable_by_invoking_user": os.access(path, os.W_OK),
        })
    except OSError as error:
        entry["status"] = error_status(error)
    return entry


def device_inventory(root: Path) -> Result:
    paths = {"/dev/kvmd-video", "/dev/kvmd-camera"}
    video_status, video_entries = list_directory(local_path(root, "/dev"))
    paths.update("/dev/" + path.name for path in video_entries if re.fullmatch(r"video[0-9]+", path.name))
    sound_status, sound_entries = list_directory(local_path(root, "/dev/snd"))
    paths.update("/dev/snd/" + path.name for path in sound_entries if re.fullmatch(r"(?:controlC[0-9]+|pcmC[0-9]+D[0-9]+[cp]|hwC[0-9]+D[0-9]+|timer|seq)", path.name))
    return {
        "video_directory_status": video_status, "sound_directory_status": sound_status,
        "permission_scope": "Invoking user only; service-user permissions are not verified. Device nodes are never opened.",
        "devices": [device_info(root, name) for name in sorted(paths)],
    }


def read_numeric_field(path: Path) -> Result:
    result = read_text(path)
    if result["status"] != "ok":
        return {"status": result["status"]}
    value = result["text"].strip()
    if not re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)(?:,(?:0x[0-9a-fA-F]+|[0-9]+))*", value):
        return {"status": "unrecognized_value"}
    values = [int(part, 16 if part.startswith("0x") else 10) for part in value.split(",")]
    return {"status": "ok", "value": values[0] if len(values) == 1 else values}


def uac2_inventory(root: Path) -> Result:
    base = local_path(root, "/sys/kernel/config/usb_gadget")
    status, gadgets = list_directory(base)
    output = []
    gaps = []
    for gadget in gadgets:
        function_status, functions = list_directory(gadget / "functions")
        if function_status != "ok":
            gaps.append({"gadget": gadget.name, "area": "functions", "status": function_status})
            continue
        for function in functions:
            if not re.fullmatch(r"uac2\.[A-Za-z0-9_-]+", function.name):
                continue
            binding = read_text(gadget / "UDC")
            bound = bool(binding.get("text", "").strip()) if binding["status"] == "ok" else None
            configuration_status, configurations = list_directory(gadget / "configs")
            linked = False
            linkage_known = configuration_status == "ok"
            for configuration in configurations:
                child_status, children = list_directory(configuration)
                if child_status != "ok":
                    linkage_known = False
                for child in children:
                    try:
                        if child.is_symlink() and child.resolve(strict=True) == function.resolve(strict=True):
                            linked = True
                    except OSError:
                        linkage_known = False
            link_value = linked if linkage_known or linked else None
            active = link_value and bound if link_value is not None and bound is not None else None
            output.append({
                "gadget": gadget.name, "function": function.name,
                "fields": {name: read_numeric_field(function / name) for name in UAC2_FIELDS},
                "linked_to_usb_configuration": link_value,
                "gadget_bound_to_udc": bound, "binding_read_status": binding["status"],
                "configuration_directory_status": configuration_status,
                "active_in_gadget": active,
            })
    return {
        "status": status, "functions": output, "inspection_gaps": gaps,
        "active_meaning": "Configfs link and nonempty UDC binding only; host enumeration and audio streaming are not verified.",
    }


def recommendations(report: Result) -> List[Result]:
    notes = [{
        "code": "inventory_only",
        "message": "Compare a working BliKVM inventory with a separate PiKVM test image. This report cannot establish audible playback or microphone delivery.",
    }]
    if not any(item["matches"] for item in report["boot_audio_overlay"]):
        notes.append({"code": "overlay_not_seen", "message": "No tc358743-audio directive was found in the checked boot files. Includes and actual overlay loading were not resolved."})
    if not any(item.get("names", {}).get("acap") for item in report["janus_configs"]):
        notes.append({"code": "capture_config_not_seen", "message": "No allowlisted Janus acap settings were found. If BliKVM stores its plugin config elsewhere, rerun with --janus-config PATH."})
    if not report["uac2"]["functions"]:
        notes.append({"code": "uac2_not_seen", "message": "No UAC2 function was observable at the checked configfs path. This does not establish whether USB audio is supported or currently intended."})
    for name in ("capture_devices", "playback_devices"):
        if report["alsa"][name]["status"] != "ok":
            notes.append({"code": name + "_unavailable", "message": "The ALSA " + name.replace("_", " ") + " listing was incomplete; inspect its reported status. No capture or playback was attempted."})
    return notes


def collect_inventory(root: Path = Path("/"), runner: Runner = run_command, extra_janus_paths: Sequence[str] = ()) -> Result:
    board = read_text(local_path(root, "/proc/device-tree/model"))
    if board["status"] == "missing":
        board = read_text(local_path(root, "/sys/firmware/devicetree/base/model"))
    alsa: Result = {
        "cards": read_text(local_path(root, "/proc/asound/cards")),
        "pcm": read_text(local_path(root, "/proc/asound/pcm")),
        "capture_devices": runner(("arecord", "-l")),
        "playback_devices": runner(("aplay", "-l")),
    }
    report = {
        "schema_version": 1, "status": "inventory_only",
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "hardware_audio_verified": False, "microphone_e2e_verified": False,
        "system": {
            "board": board, "kernel_release": platform.release(), "machine": platform.machine(),
            "invoking_uid": os.getuid(),
        },
        "packages": package_inventory(runner), "boot_audio_overlay": overlay_inventory(root),
        "alsa": alsa, "device_permissions": device_inventory(root),
        "janus_configs": janus_inventory(root, extra_janus_paths),
        "kvmd_audio_flags": kvmd_inventory(root), "uac2": uac2_inventory(root),
        "limitations": [
            "No audio recording, playback, browser microphone request, service change, or configfs write is performed.",
            "Config extraction is an allowlisted subset; includes, aliases, merge precedence, running config, and service-user access are not resolved.",
            "The report includes local media paths, numeric ownership, package versions, and ALSA device descriptions; review before sharing publicly.",
        ],
    }
    report["recommendations"] = recommendations(report)
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--janus-config", action="append", default=[], metavar="PATH", help="Additional Janus plugin config to inspect using the same strict allowlist; repeatable.")
    arguments = parser.parse_args(argv)
    extra_paths = [str(Path(path).absolute()) for path in arguments.janus_config]
    print(json.dumps(collect_inventory(extra_janus_paths=extra_paths), indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
