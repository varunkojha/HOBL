# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

"""Side-effect-free configuration and artifact validation for enterprise_ai."""

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import socket


CONDITIONS = {
    "A": (True, False, False),
    "B": (True, True, False),
    "C": (False, False, True),
    "D": (True, False, True),
    "E": (True, True, True),
    "F": (False, True, True),
}

DEFAULTS = {
    "dedicated_dut": ("0", "Explicit consent to use an exclusive, remote Windows test DUT.", ["0", "1"]),
    "condition": ("A", "A=EC, B=EC+stress, C=AI, D=EC+AI, E=all, F=stress+AI.", list(CONDITIONS)),
    "protocol": ("indexing", "Indexing load is implemented. Query/model remain gated.", ["indexing", "query", "model"]),
    "measurement_seconds": ("1800", "Fixed observation window; an overrun invalidates the run.", []),
    "settle_seconds": ("10", "Settle time after the core trace starts, before measurement.", []),
    "stress_workers": ("1", "Fixed worker count, calibrated on the DUT before comparisons.", []),
    "stress_duty_cycle": ("0.25", "Per-worker duty fraction, not achieved whole-system CPU.", []),
    "corpus_dir": ("", "Approved host corpus directory. Required for indexing cells.", []),
    "semantic_ready_value": ("", "Operator-validated SemanticIndexingStatus value for this OS build; no assumed default.", []),
    "configure_utc": ("0", "1=snapshot/apply/restore UTC state; 0=verify preconfigured state only.", ["0", "1"]),
    "validation_mode": ("capture", "capture=inconclusive evidence collection; strict requires validated trace evidence.", ["capture", "strict"]),
    "required_pts": ("8805 8806 8807", "Required foreground PT IDs in strict mode; space-separated.", []),
    "background_timers": ("1", "Inherited foreground baseline timer activity.", ["0", "1"]),
    "background_teams": ("1", "Inherited foreground baseline Teams activity.", ["0", "1"]),
    "simple_office_launch": ("1", "Include inherited Office launches.", ["0", "1"]),
    "file_explorer": ("1", "Include inherited Explorer probes.", ["0", "1"]),
    "snipping_tool": ("1", "Include inherited Snipping Tool probe.", ["0", "1"]),
    "settings_app": ("1", "Include inherited Settings probe.", ["0", "1"]),
}


def finite_number(value, name, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number.") from error
    if isinstance(value, bool) or not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return number


def boolean(value, name):
    if value not in ("0", "1"):
        raise ValueError(f"{name} must be 0 or 1.")
    return value == "1"


@dataclass(frozen=True)
class Configuration:
    condition: str
    protocol: str
    measurement_seconds: float
    settle_seconds: float
    stress_workers: int
    stress_duty_cycle: float
    corpus_dir: str
    semantic_ready_value: int | None
    configure_utc: bool
    validation_mode: str
    required_pts: tuple[str, ...]

    @property
    def foreground(self):
        return CONDITIONS[self.condition][0]

    @property
    def stress(self):
        return CONDITIONS[self.condition][1]

    @property
    def ai(self):
        return CONDITIONS[self.condition][2]

    @classmethod
    def from_values(cls, values):
        def get(name):
            return values.get(name, DEFAULTS[name][0])

        condition = get("condition")
        if condition not in CONDITIONS:
            raise ValueError("condition must be one of A, B, C, D, E, F.")
        protocol = get("protocol")
        if protocol not in ("indexing", "query", "model"):
            raise ValueError("protocol must be indexing, query, or model.")
        if protocol != "indexing":
            raise ValueError(
                "Measured semantic queries and model/API execution are gated until an approved "
                "runner and its completion/correctness contract are validated on a dedicated DUT. "
                "Use protocol=indexing; ordinary filename search is not a substitute."
            )
        workers = finite_number(get("stress_workers"), "stress_workers", 1, 256)
        if not workers.is_integer():
            raise ValueError("stress_workers must be a whole number.")
        mode = get("validation_mode")
        if mode not in ("capture", "strict"):
            raise ValueError("validation_mode must be capture or strict.")
        pts = tuple(dict.fromkeys(get("required_pts").split()))
        if any(not pt.isascii() or not pt.isdecimal() for pt in pts):
            raise ValueError("required_pts must contain space-separated numeric PT IDs.")
        if mode == "strict" and CONDITIONS[condition][0] and not pts:
            raise ValueError("Strict foreground runs require an explicit non-empty required_pts list.")
        ready_value = None
        if CONDITIONS[condition][2]:
            if not get("corpus_dir"):
                raise ValueError("corpus_dir must identify an approved host corpus for indexing.")
            if not str(get("semantic_ready_value")).isascii() or not str(get("semantic_ready_value")).isdecimal():
                raise ValueError(
                    "semantic_ready_value is required: obtain the validated enabled value for "
                    "SemanticIndexingStatus on the target build. No readiness value is assumed."
                )
            ready_value = int(get("semantic_ready_value"))
            if ready_value > 0xFFFFFFFF:
                raise ValueError("semantic_ready_value must fit a Windows DWORD.")
        return cls(
            condition, protocol,
            finite_number(get("measurement_seconds"), "measurement_seconds", 30, 7200),
            finite_number(get("settle_seconds"), "settle_seconds", 0, 600),
            int(workers),
            finite_number(get("stress_duty_cycle"), "stress_duty_cycle", 0.01, 1),
            get("corpus_dir"), ready_value,
            boolean(get("configure_utc"), "configure_utc"), mode, pts,
        )


def validate_remote_dut(consent, platform, dut_ip, local_execution):
    if consent != "1":
        raise ValueError("Set enterprise_ai:dedicated_dut=1 only for an exclusive test DUT/account.")
    if platform.lower() != "windows" or local_execution != "0":
        raise ValueError("enterprise_ai requires a remote Windows DUT; local execution is disabled.")
    host = str(dut_ip).strip().lower().rstrip(".")
    own_names = {"", "localhost", socket.gethostname().lower(), socket.getfqdn().lower()}
    if host in own_names:
        raise ValueError("The developer/host machine cannot be used as the enterprise_ai DUT.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return  # A remote hostname will be resolved and checked before its first RPC.
    effective = getattr(address, "ipv4_mapped", None) or address
    if effective.is_loopback or effective.is_unspecified:
        raise ValueError("Loopback/unspecified DUT addresses are not allowed.")


def file_hash(filename):
    digest = hashlib.sha256()
    with open(filename, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def corpus_manifest(directory, max_files=1000, max_bytes=256 * 1024 * 1024):
    candidate = Path(directory).absolute()
    for item in (candidate, *candidate.parents):
        info = item.lstat()
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Corpus reparse points/symlinks are not allowed: {item}")
    root = Path(directory).resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError("Use a dedicated, bounded corpus directory, not a drive or home directory.")
    files = []
    total = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            item = Path(current, name)
            info = item.lstat()
            if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Corpus reparse points/symlinks are not allowed: {item}")
        for name in sorted(names):
            item = Path(current, name)
            if not item.is_file():
                raise ValueError(f"Corpus entry is not a regular file: {item}")
            total += item.stat().st_size
            if total > max_bytes or len(files) >= max_files:
                raise ValueError("Corpus exceeds the 1,000-file / 256-MiB safety limit.")
            files.append({
                "relative_path": str(item.relative_to(root)).replace("/", "\\"),
                "size_bytes": item.stat().st_size,
                "sha256": file_hash(item),
            })
    if not files:
        raise ValueError("The approved corpus must contain at least one file.")
    files.sort(key=lambda entry: entry["relative_path"])
    serialized = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 1,
        "files": files,
        "file_count": len(files),
        "total_bytes": total,
        "sha256": hashlib.sha256(serialized).hexdigest(),
    }
