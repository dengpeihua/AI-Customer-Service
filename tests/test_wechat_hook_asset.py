from __future__ import annotations

import ctypes
import hashlib
import unittest
from pathlib import Path


_EXPECTED_SHA256 = "a2cd8623b1fd0d8addc71ca96621e9be384492161524f9d8c8832f762ed6a962"
_VERSION_EXPORTS = (
    "GetFileVersionInfoA",
    "GetFileVersionInfoByHandle",
    "GetFileVersionInfoExA",
    "GetFileVersionInfoExW",
    "GetFileVersionInfoSizeA",
    "GetFileVersionInfoSizeExA",
    "GetFileVersionInfoSizeExW",
    "GetFileVersionInfoSizeW",
    "GetFileVersionInfoW",
    "VerFindFileA",
    "VerFindFileW",
    "VerInstallFileA",
    "VerInstallFileW",
    "VerLanguageNameA",
    "VerLanguageNameW",
    "VerQueryValueA",
    "VerQueryValueW",
)


class WeChatHookAssetTests(unittest.TestCase):
    def test_patched_hook_has_expected_hash_and_proxy_exports(self) -> None:
        asset = Path(__file__).resolve().parents[1] / "assets" / "hook" / "version_hook.dll"

        self.assertEqual(_EXPECTED_SHA256, hashlib.sha256(asset.read_bytes()).hexdigest())
        dll = ctypes.WinDLL(str(asset))
        for export in _VERSION_EXPORTS:
            self.assertTrue(getattr(dll, export))


if __name__ == "__main__":
    unittest.main()
