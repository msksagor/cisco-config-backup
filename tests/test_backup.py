import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.touch()

    def test_inventory_defaults_and_unsafe_name(self):
        path = Path(__file__).resolve().parents[1] / "inventory.example.yml"
        devices = backup.load_inventory(path)
        self.assertEqual({device.device_type for device in devices}, backup.PLATFORMS)
        self.assertEqual(devices[0].credential_prefix, "CISCO")
        invalid = self.root / "inventory.yml"
        invalid.write_text(path.read_text().replace("branch-router", "../escape"))
        with self.assertRaisesRegex(ValueError, "invalid name"):
            backup.load_inventory(invalid)

    def test_snapshot_lifecycle_for_ios_and_nxos(self):
        for name, first_header, second_header in [
            ("ios", "Building configuration...\nCurrent configuration : 180 bytes\n"
             "! Last configuration change at 12:00\n",
             "Building configuration...\nCurrent configuration : 188 bytes\n"
             "! Last configuration change at 13:00\n"),
            ("nxos", "!Command: show running-config\n!Time: today\n",
             "!Command: show running-config\n!Time: tomorrow\n"),
        ]:
            with self.subTest(name=name):
                first = backup.save_snapshot(self.root, name, first_header + "hostname edge")
                self.assertEqual(first.read_text(), first_header + "hostname edge\n")
                self.assertIsNone(
                    backup.save_snapshot(self.root, name, second_header + "hostname edge")
                )
                second = backup.save_snapshot(self.root, name, second_header + "hostname core")
                self.assertNotEqual(first, second)
                self.assertEqual(len(list(first.parent.glob("*.cfg"))), 2)
                if os.name == "posix":
                    self.assertEqual(first.stat().st_mode & 0o777, 0o600)

    @patch.dict(os.environ, {"CISCO_USERNAME": "user", "CISCO_PASSWORD": "test",
                             "CISCO_ENABLE_SECRET": "test-enable"}, clear=True)
    @patch("backup.ConnectHandler")
    def test_verified_ssh_and_ios_enable(self, connect):
        connection = connect.return_value.__enter__.return_value
        connection.send_command.return_value = "hostname edge"
        device = backup.Device("edge", "192.0.2.4", "cisco_xe")
        self.assertEqual(backup.fetch_config(device, self.known_hosts, 55), "hostname edge")
        self.assertTrue(connect.call_args.kwargs["ssh_strict"])
        self.assertEqual(connect.call_args.kwargs["alt_key_file"], str(self.known_hosts))
        connection.enable.assert_called_once_with()
        connection.send_command.assert_called_once_with("show running-config", read_timeout=55)
        connect.return_value.__exit__.assert_called_once()

    @patch.dict(os.environ, {}, clear=True)
    @patch("backup.ConnectHandler")
    def test_missing_credentials_never_connect(self, connect):
        device = backup.Device("edge", "192.0.2.4", "cisco_ios")
        with self.assertRaisesRegex(backup.BackupError, "CISCO_PASSWORD"):
            backup.fetch_config(device, self.known_hosts, 60)
        connect.assert_not_called()

    @patch.dict(os.environ, {"CISCO_USERNAME": "user", "CISCO_PASSWORD": "test"}, clear=True)
    @patch("backup.ConnectHandler")
    def test_rejected_command_and_partial_run(self, connect):
        path = self.root / "inventory.yml"
        path.write_text(
            "devices:\n"
            "  - {name: edge, host: 192.0.2.4, device_type: cisco_ios}\n"
            "  - {name: core, host: 192.0.2.5, device_type: cisco_nxos}\n"
        )
        connection = connect.return_value.__enter__.return_value
        connection.send_command.side_effect = [
            "% Authorization failed. sensitive-text", "hostname core"
        ]
        log_file = self.root / "backup.log"
        backups = self.root / "backups"
        result = backup.main([
            "--inventory", str(path), "--known-hosts", str(self.known_hosts),
            "--backup-dir", str(backups), "--log-file", str(log_file),
        ])
        self.assertEqual(result, 1)
        self.assertFalse((backups / "edge").exists())
        self.assertEqual(len(list((backups / "core").glob("*.cfg"))), 1)
        log = log_file.read_text()
        self.assertIn("saved=1 unchanged=0 failed=1", log)
        self.assertNotIn("sensitive-text", log)
        connection.enable.assert_not_called()
