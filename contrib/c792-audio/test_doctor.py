# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardware-independent regression tests for the inventory's privacy and gaps."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("c792_doctor", Path(__file__).with_name("doctor.py"))
assert SPEC and SPEC.loader
doctor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doctor)


class ConfigurationPrivacyTest(unittest.TestCase):
    def test_janus_only_returns_allowlisted_local_media_names(self):
        config = r'''
            general: { token = "top-level-secret"; host = "192.168.50.218"; }
            /* acap: { device = "comment-secret"; } */
            acap: {
                device = "hw:tc358743,0"; # trailing-secret
                tc358743 = "/dev/kvmd-video";
                password = "capture-secret";
                nested: { device = "nested-secret"; }
            }
            aplay: { device = "plughw:UAC2Gadget,0"; token = "playback-secret"; }
            video: { sink = "kvmd::ustreamer::h264"; key = "video-secret"; }
            vplay: { device = "/dev/kvmd-camera"; sink = "kvmd::ucamera::h264"; }
            auth: { acap: { device = "hw:nested-auth-secret,0"; } }
        '''
        result = doctor.parse_janus_names(config)
        serialized = json.dumps(result)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("192.168.50.218", serialized)
        self.assertEqual(result["names"]["acap"], {"device": "hw:tc358743,0", "tc358743": "/dev/kvmd-video"})
        self.assertEqual(result["names"]["aplay"]["device"], "plughw:UAC2Gadget,0")
        self.assertFalse(result["effective_config_resolved"])

    def test_network_and_nonstandard_values_are_redacted_even_in_allowed_keys(self):
        result = doctor.parse_janus_names('''
            acap: { device = "https://host/audio?token=private-secret"; tc358743 = "/etc/private-secret"; }
            video: { sink = "192.168.1.3/private-secret"; }
            aplay: { device = "hw:CARD=tc358743"; }
        ''')
        self.assertNotIn("private-secret", json.dumps(result))
        self.assertNotIn("192.168.1.3", json.dumps(result))
        self.assertEqual(set(result["redacted_fields"]), {"acap.device", "acap.tc358743", "video.sink"})
        self.assertEqual(result["names"]["aplay"]["device"], "hw:CARD=tc358743")

    def test_sampling_rate_distinguishes_auto_from_fixed_and_rejects_unbounded_values(self):
        for raw, expected in [("0", 0), ("48000", 48000), ("192000", 192000)]:
            with self.subTest(raw=raw):
                result = doctor.parse_janus_names("acap: { sampling_rate = " + raw + "; }")
                self.assertEqual(result["names"]["acap"]["sampling_rate"], expected)
        for raw in ("99999999999", '"private-secret"', "-1"):
            with self.subTest(raw=raw):
                result = doctor.parse_janus_names("acap: { sampling_rate = " + raw + "; }")
                self.assertEqual(result["redacted_fields"], ["acap.sampling_rate"])
                self.assertNotIn("private-secret", json.dumps(result))

    def test_yaml_flags_do_not_include_secrets_or_pretend_to_resolve_merges(self):
        result = doctor.parse_audio_flags('''
auth:
  token: private-secret
  otg:
    devices:
      audio:
        enabled: false
otg:
  devices:
    audio:
      enabled: true # hidden-secret
      start: false
      mic:
        enabled: yes
      speakers:
        enabled: false
      password: private-secret
''')
        flags = {item["path"]: item["value"] for item in result["explicit_flags"]}
        self.assertEqual(len(flags), 4)
        self.assertTrue(flags["otg.devices.audio.enabled"])
        self.assertTrue(flags["otg.devices.audio.mic.enabled"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertFalse(result["effective_config_resolved"])

    def test_yaml_aliases_and_strings_are_not_coerced_to_enabled(self):
        result = doctor.parse_audio_flags('''
otg:
  devices:
    audio:
      enabled: "false"
      mic:
        enabled: *private-secret
''')
        self.assertEqual(result["explicit_flags"], [])
        self.assertEqual(len(result["unsupported_values"]), 2)
        self.assertNotIn("private-secret", json.dumps(result))


class ReadOnlyInventoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="c792-doctor-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name, value):
        path = self.root / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def test_device_inventory_stats_nodes_without_reading_their_contents(self):
        video = self.write("/dev/video0", "private-video-frame")
        video.chmod(0o640)
        self.write("/dev/snd/pcmC0D0c", "private-audio-sample")
        (video.parent / "kvmd-video").symlink_to("video0")
        with mock.patch.object(Path, "open", side_effect=AssertionError("Device must not be opened")):
            result = doctor.device_inventory(self.root)
        entries = {entry["path"]: entry for entry in result["devices"]}
        self.assertEqual(entries["/dev/video0"]["mode"], "0640")
        self.assertEqual(entries["/dev/video0"]["type"], "regular_file")
        self.assertTrue(entries["/dev/kvmd-video"]["symlink"])
        self.assertEqual(entries["/dev/kvmd-camera"]["status"], "missing")
        self.assertNotIn("private-", json.dumps(result))

    def test_overlay_ignores_private_comments_and_preserves_disabled_state(self):
        self.write("/boot/config.txt", "[pi4]\ndtoverlay=tc358743-audio # private-secret\n#dtoverlay=tc358743-audio,private-secret\n[all]\n")
        result = doctor.overlay_inventory(self.root)
        matches = result[0]["matches"]
        self.assertEqual(len(matches), 2)
        self.assertFalse(matches[0]["commented_out"])
        self.assertTrue(matches[0]["under_conditional_section"])
        self.assertTrue(matches[1]["commented_out"])
        self.assertNotIn("private-secret", json.dumps(result))

    def test_uac2_link_and_binding_are_inventory_not_end_to_end_evidence(self):
        prefix = "/sys/kernel/config/usb_gadget/kvmd"
        self.write(prefix + "/UDC", "controller-id-not-for-output\n")
        function = self.root / (prefix + "/functions/uac2.usb0").lstrip("/")
        for name, value in {"p_chmask": "3", "p_srate": "48000,44100", "p_ssize": "2", "c_chmask": "0"}.items():
            self.write(prefix + "/functions/uac2.usb0/" + name, value)
        config = self.root / (prefix + "/configs/c.1").lstrip("/")
        config.mkdir(parents=True)
        (config / "uac2.usb0").symlink_to(function)
        result = doctor.uac2_inventory(self.root)
        entry = result["functions"][0]
        self.assertTrue(entry["active_in_gadget"])
        self.assertTrue(entry["gadget_bound_to_udc"])
        self.assertEqual(entry["fields"]["p_srate"]["value"], [48000, 44100])
        self.assertEqual(entry["fields"]["c_srate"]["status"], "missing")
        self.assertNotIn("controller-id-not-for-output", json.dumps(result))
        (config / "uac2.usb0").unlink()
        self.assertFalse(doctor.uac2_inventory(self.root)["functions"][0]["active_in_gadget"])

    def test_missing_system_is_reported_without_false_pass_or_device_operations(self):
        calls = []

        def absent(argv):
            calls.append(tuple(argv))
            return {"status": "missing"}

        result = doctor.collect_inventory(root=self.root, runner=absent)
        self.assertEqual(result["status"], "inventory_only")
        self.assertFalse(result["hardware_audio_verified"])
        self.assertFalse(result["microphone_e2e_verified"])
        self.assertEqual(result["alsa"]["cards"]["status"], "missing")
        self.assertEqual(result["uac2"]["status"], "missing")
        self.assertEqual(calls[:2], [("arecord", "-l"), ("aplay", "-l")])
        self.assertEqual({call[0] for call in calls}, {"arecord", "aplay", "pacman", "dpkg-query"})
        self.assertNotIn("PASS", json.dumps(result))

    def test_permission_error_and_oversized_file_are_explicit(self):
        denied = self.write("/denied", "private-config")
        with mock.patch.object(Path, "open", side_effect=PermissionError("private-path")):
            result = doctor.read_text(denied)
        self.assertEqual(result, {"status": "permission_denied"})
        large = self.write("/large", "x" * (doctor.MAX_FILE_BYTES + 1))
        self.assertEqual(doctor.read_text(large), {"status": "too_large"})

    def test_a_config_path_cannot_open_a_pipe_or_media_device(self):
        pipe = self.root / "plugin-config-pipe"
        os.mkfifo(pipe)
        with mock.patch.object(Path, "open", side_effect=AssertionError("Special file must not be opened")):
            self.assertEqual(doctor.read_text(pipe), {"status": "unsupported_file_type"})
            self.assertEqual(doctor.read_text(Path("/dev/null")), {"status": "unsupported_file_type"})


class CommandIsolationTest(unittest.TestCase):
    def test_timeout_discards_partial_output_and_sensitive_exception_details(self):
        timeout = subprocess.TimeoutExpired(["arecord", "-l"], doctor.COMMAND_TIMEOUT, output="private-partial-output", stderr="private-error")
        with mock.patch.object(subprocess, "run", side_effect=timeout) as run:
            result = doctor.run_command(("arecord", "-l"))
        self.assertEqual(result["status"], "timeout")
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(run.call_args.kwargs["timeout"], doctor.COMMAND_TIMEOUT)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_absent_and_unexecutable_commands_are_reported(self):
        for exception, status in [(FileNotFoundError("secret-path"), "missing"), (PermissionError("secret-path"), "permission_denied")]:
            with self.subTest(status=status), mock.patch.object(subprocess, "run", side_effect=exception):
                result = doctor.run_command(("arecord", "-l"))
                self.assertEqual(result, {"status": status})

    def test_package_inventory_keeps_only_requested_package_versions(self):
        def command(argv):
            return {"status": "nonzero_exit", "exit_code": 1, "stdout": "kvmd 4.142-1\nprivate-secret 1.0\njanus-gateway-pikvm 1.3.0-1\n"}

        result = doctor.package_inventory(command)
        self.assertEqual(result["pacman"]["packages"], {"kvmd": "4.142-1", "janus-gateway-pikvm": "1.3.0-1"})
        self.assertNotIn("private-secret", json.dumps(result))

    def test_main_returns_successful_json_for_inventory_gaps(self):
        report = {"status": "inventory_only", "hardware_audio_verified": False, "microphone_e2e_verified": False}
        with mock.patch.object(doctor, "collect_inventory", return_value=report), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            status = doctor.main([])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), report)


if __name__ == "__main__":
    unittest.main()
