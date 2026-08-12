from __future__ import annotations

import unittest

from widget.app import UnavailableWeChatAdapter, choose_pages_adapter


class ChannelPageSelectionTests(unittest.TestCase):
    def test_personal_wechat_is_default_when_both_channels_are_available(self) -> None:
        wechat = object()
        wecom = object()

        use_wecom, selected = choose_pages_adapter(wechat, [wecom])

        self.assertFalse(use_wecom)
        self.assertIs(wechat, selected)

    def test_wecom_is_default_only_when_personal_wechat_is_unavailable(self) -> None:
        unavailable = UnavailableWeChatAdapter("offline")
        wecom = object()

        use_wecom, selected = choose_pages_adapter(unavailable, [wecom])

        self.assertTrue(use_wecom)
        self.assertIs(wecom, selected)


if __name__ == "__main__":
    unittest.main()
