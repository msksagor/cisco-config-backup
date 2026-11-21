"""Back up Cisco running configurations over verified SSH."""
import argparse
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from netmiko import ConnectHandler

LOG = logging.getLogger("cisco_backup")
PLATFORMS = {"cisco_ios", "cisco_xe", "cisco_nxos"}
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}", re.ASCII)
ENV_PREFIX = re.compile(r"[A-Z_][A-Z0-9_]*", re.ASCII)
SNAPSHOT = re.compile(r"\d{8}T\d{12}Z\.cfg", re.ASCII)
CLI_ERROR = re.compile(
    r"^\s*(?:%\s*(?:invalid|incomplete|ambiguous|authorization|permission|error)"
    r"|(?:error:\s*)?(?:authorization (?:failed|required)|permission denied"
    r"|access denied|invalid command|command authorization failed))\b",
    re.IGNORECASE,
)
HEADER = re.compile(
    r"(?:!\s*)?(?:Building configuration\.\.\.|Current configuration\s*:\s*\d+ bytes"
    r"|Command: show running-config|Time: .+|Last configuration change .+|NVRAM config last updated .+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Device:
    name: str
    host: str
    device_type: str
    port: int = 22
    credential_prefix: str = "CISCO"


def load_inventory(path: Path) -> list[Device]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"devices"}:
        raise ValueError("inventory must contain a devices list")
    entries = document["devices"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("devices must be a nonempty list")

    devices = []
    names = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"device {index} must be a mapping")
        try:
            device = Device(**entry)
        except TypeError:
            raise ValueError(f"device {index} has missing or unsupported keys") from None
        if not isinstance(device.name, str) or not NAME.fullmatch(device.name):
            raise ValueError(f"device {index} has an invalid name")
        if device.name in names:
            raise ValueError(f"device {index} has a duplicate name")
        if not isinstance(device.host, str) or not device.host or any(
            char.isspace() or ord(char) < 32 for char in device.host
        ):
            raise ValueError(f"device {index} has an invalid host")
        if not isinstance(device.device_type, str) or device.device_type not in PLATFORMS:
            raise ValueError(f"device {index} has an unsupported device_type")
        if type(device.port) is not int or not 1 <= device.port <= 65535:
            raise ValueError(f"device {index} has an invalid port")
        if not isinstance(device.credential_prefix, str) or not ENV_PREFIX.fullmatch(
            device.credential_prefix
        ):
            raise ValueError(f"device {index} has an invalid credential_prefix")
        names.add(device.name)
        devices.append(device)
    return devices


class BackupError(Exception):
    """Failure with a message that is safe to log."""


def fetch_config(device: Device, known_hosts: Path, read_timeout: int) -> str:
    prefix = device.credential_prefix
    username = os.environ.get(f"{prefix}_USERNAME")
    password = os.environ.get(f"{prefix}_PASSWORD")
    secret = os.environ.get(f"{prefix}_ENABLE_SECRET")
    if not username or not password:
        raise BackupError(f"set {prefix}_USERNAME and {prefix}_PASSWORD")

    with ConnectHandler(
        host=device.host,
        port=device.port,
        device_type=device.device_type,
        username=username,
        password=password,
        secret=secret or "",
        ssh_strict=True,
        system_host_keys=False,
        alt_host_keys=True,
        alt_key_file=str(known_hosts),
        conn_timeout=10,
        auth_timeout=15,
        banner_timeout=15,
    ) as connection:
        if secret and device.device_type in {"cisco_ios", "cisco_xe"}:
            connection.enable()
        output = connection.send_command("show running-config", read_timeout=read_timeout)
    if not comparable_lines(output):
        raise BackupError("device returned an empty configuration")
    if any(CLI_ERROR.match(line) for line in output.splitlines()[:10]):
        raise BackupError("device rejected show running-config")
    return output


def comparable_lines(config: str) -> list[str]:
    """Ignore known volatile preamble lines, preserving the configuration body."""
    lines = config.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped in {"", "!"} or HEADER.fullmatch(stripped):
            continue
        return lines[index:]
    return []


def save_snapshot(root: Path, name: str, config: str) -> Path | None:
    directory = root / name
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    previous = max(
        (path for path in directory.iterdir() if SNAPSHOT.fullmatch(path.name)),
        default=None,
    )
    if previous is not None:
        if comparable_lines(previous.read_text(encoding="utf-8")) == comparable_lines(config):
            return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"{stamp}.cfg"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=directory, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(config)
            if not config.endswith("\n"):
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def configure_logging(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(descriptor)
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )
    formatter.converter = time.gmtime
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    for handler in LOG.handlers[:]:
        LOG.removeHandler(handler)
        handler.close()
    for handler in (logging.StreamHandler(), logging.FileHandler(path, encoding="utf-8")):
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=Path("inventory.yml"))
    parser.add_argument("--backup-dir", type=Path, default=Path("backups"))
    parser.add_argument("--log-file", type=Path, default=Path("logs/backup.log"))
    parser.add_argument("--known-hosts", type=Path, default=Path("~/.ssh/known_hosts"))
    parser.add_argument("--read-timeout", type=int, default=60, metavar="SECONDS")
    parser.add_argument("--check-inventory", action="store_true")
    args = parser.parse_args(argv)
    if args.read_timeout < 1:
        parser.error("--read-timeout must be positive")

    try:
        devices = load_inventory(args.inventory)
    except ValueError as error:
        parser.exit(2, f"Inventory error: {error}\n")
    except (OSError, yaml.YAMLError):
        parser.exit(2, "Inventory error: cannot read file or parse YAML\n")
    if args.check_inventory:
        print(f"Inventory valid: {len(devices)} device(s)")
        return 0

    known_hosts = args.known_hosts.expanduser()
    try:
        with known_hosts.open("rb"):
            pass
        configure_logging(args.log_file)
    except OSError as error:
        parser.exit(2, f"Cannot open known-hosts or log file ({type(error).__name__})\n")

    saved = unchanged = failed = 0
    for device in devices:
        try:
            config = fetch_config(device, known_hosts, args.read_timeout)
            snapshot = save_snapshot(args.backup_dir, device.name, config)
        except BackupError as error:
            failed += 1
            LOG.error("%s failed: %s", device.name, error)
        except Exception as error:
            # Netmiko exceptions may include raw device output. Log only the class.
            failed += 1
            LOG.error("%s failed (%s)", device.name, type(error).__name__)
        else:
            if snapshot is None:
                unchanged += 1
                LOG.info("%s unchanged", device.name)
            else:
                saved += 1
                LOG.info("%s snapshot saved: %s", device.name, snapshot.name)
    LOG.info("Finished: saved=%d unchanged=%d failed=%d", saved, unchanged, failed)
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
