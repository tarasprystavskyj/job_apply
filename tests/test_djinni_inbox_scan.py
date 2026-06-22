from __future__ import annotations

import sys
import unittest
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import djinni_inbox_scan  # noqa: E402


class DjinniInboxScanTests(unittest.TestCase):
    def test_normalize_offer_preserves_unread_signal(self) -> None:
        offer = djinni_inbox_scan.normalize_offer(
            {
                "href": "https://djinni.co/my/inbox/25880123/",
                "anchorText": "Senior Python Engineer",
                "text": "Senior Python Engineer\nAcme\nPython AI role",
                "unread": True,
                "unreadHint": "thread unread",
            }
        )

        payload = asdict(offer)

        self.assertTrue(payload["unread"])
        self.assertEqual(payload["unread_hint"], "thread unread")
        self.assertEqual(payload["source_url"], "https://djinni.co/my/inbox/25880123/")

    def test_inspect_script_collects_unread_markers(self) -> None:
        source = Path(djinni_inbox_scan.__file__).read_text(encoding="utf-8")

        self.assertIn("unreadCount", source)
        self.assertIn("unreadOffers", source)
        self.assertIn("unreadByClass", source)


if __name__ == "__main__":
    unittest.main()
