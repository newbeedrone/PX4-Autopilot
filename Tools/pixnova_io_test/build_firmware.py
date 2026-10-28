#!/usr/bin/env python3
"""Build isolated source/probe images without reusing NuttX object caches."""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import zlib

from generate_profile import load_profile


ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def build_lock(directory=None):
    # NuttX archives live in the source tree and are shared between targets.
    # Prevent two copies of this helper from cleaning/building them together.
    import fcntl
    directory = ROOT / "build" if directory is None else directory
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".pixnova-io-build.lock").open("a") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another PixNova IO firmware build is already running") from error
        yield


def verify_context(digest, revision):
    _, current = load_profile(Path(__file__).with_name("profile.json"))
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if current != digest or head != revision:
        raise RuntimeError("Profile or Git revision changed during the build; rebuild both roles")


def validate_package(package, binary, board_id):
    if package.get("magic") != "PX4FWv1" or package.get("board_id") != board_id or package.get("image_size") != len(binary):
        raise ValueError("PX4 package identity or image size does not match the binary")
    try:
        image = zlib.decompress(base64.b64decode(package["image"], validate=True))
    except (KeyError, ValueError, zlib.error) as error:
        raise ValueError("Invalid PX4 package image") from error
    if image != binary:
        raise ValueError("PX4 package contains a different firmware binary")


def validate_binary(data, role, digest):
    if role not in ("source", "probe") or len(data) < 8:
        raise ValueError("Invalid firmware role or image")
    flash = 0x08020000 if role == "source" else 0x08000000
    stack, reset = struct.unpack("<II", data[:8])
    if stack % 8 or not (0x20000000 < stack <= 0x20020000 or 0x24000000 < stack <= 0x24080000):
        raise ValueError("Firmware startup stack is outside SRAM or not 8-byte aligned")
    if not reset & 1 or not flash <= reset & ~1 < flash + len(data):
        raise ValueError("Firmware reset vector does not match its flash address")
    product = "PixNova IO Test" if role == "source" else "PixNova IO Probe"
    if digest.encode() not in data or not (product.encode() in data or product.encode("utf-16-le") in data):
        raise ValueError("Firmware profile or USB product does not match its role")
    return {"flash_address": flash, "initial_stack": stack, "reset_vector": reset}


def clean_generated_objects(directory):
    # NuttX's in-source archives can survive a configuration change. Remove only
    # ignored, untracked object/archive files; never source or tracked binaries.
    candidates = [path for path in directory.rglob("*")
                  if path.suffix in (".o", ".a") and path.is_file() and not path.is_symlink()]
    if not candidates:
        return
    data = b"".join(os.fsencode(path.relative_to(directory)) + b"\0" for path in candidates)
    checked = subprocess.run(["git", "-C", str(directory), "check-ignore", "-z", "--stdin"], input=data, capture_output=True)
    if checked.returncode not in (0, 1):
        raise RuntimeError(checked.stderr.decode(errors="replace"))
    ignored = {os.fsdecode(name) for name in checked.stdout.split(b"\0") if name}
    removed = 0
    for path in candidates:
        if str(path.relative_to(directory)) in ignored:
            assert path.resolve().is_relative_to(directory.resolve())
            path.unlink()
            removed += 1
    print(f"Cleared {removed} generated NuttX objects in {directory}", flush=True)


def build(role, artifact_dir, stamp, digest, revision):
    verify_context(digest, revision)
    target = "newbeedrone_pixnova_" + ("io-test" if role == "source" else "io-probe")
    for folder in (ROOT / "platforms/nuttx/NuttX/nuttx", ROOT / "platforms/nuttx/NuttX/apps"):
        clean_generated_objects(folder)
    cached = ROOT / "build" / target / "NuttX"
    if cached.exists():
        for archive in cached.rglob("*.a"):
            assert archive.resolve().is_relative_to(cached.resolve())
            archive.unlink()
    subprocess.run(["make", target, "-j4"], cwd=ROOT, env=os.environ | {"CCACHE_DISABLE": "1"}, check=True)
    verify_context(digest, revision)
    head = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
    outputs = []
    # Require the packager's success output even though the raw probe is flashed via SWD.
    package = ROOT / "build" / target / (target + ".px4")
    if not package.is_file():
        raise FileNotFoundError(package)
    binary = (ROOT / "build" / target / (target + ".bin")).read_bytes()
    vectors = validate_binary(binary, role, digest)
    prototype = json.loads((ROOT / "boards/newbeedrone/pixnova/firmware.prototype").read_text())
    validate_package(json.loads(package.read_text()), binary, prototype["board_id"])
    for extension in ([".px4", ".elf", ".bin"] if role == "source" else [".elf", ".bin"]):
        origin = ROOT / "build" / target / (target + extension)
        destination = artifact_dir / f"{target}_{head}{'_dirty' if dirty else ''}_{stamp}{extension}"
        with origin.open("rb") as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output)
        outputs.append({"path": str(destination), "bytes": destination.stat().st_size,
                        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
    return {"role": role, "target": target, "commit": head, "dirty": dirty, **vectors, "artifacts": outputs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("both", "source", "probe"), default="both")
    parser.add_argument("--artifacts", type=Path, default=Path("/tmp/px4-firmware"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    roles = ("source", "probe") if args.role == "both" else (args.role,)
    with build_lock():
        profile, digest = load_profile(Path(__file__).with_name("profile.json"))
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        results = [build(role, args.artifacts, stamp, digest, revision) for role in roles]
    manifest = args.artifacts / ("pixnova-io-build_" + stamp + ".json")
    manifest.write_text(json.dumps({"profile_sha256": digest, "channels": len(profile["channels"]), "builds": results}, indent=2) + "\n")
    print(f"Build manifest: {manifest}", flush=True)


if __name__ == "__main__":
    main()
