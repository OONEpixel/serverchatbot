import unittest

from app.protocol import ProtocolError, decode, encode, make_envelope


class ProtocolTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        original = make_envelope("player_chat", {"message": "你好"}, "survival")
        decoded = decode(encode(original), "fallback")
        self.assertEqual(original, decoded)

    def test_rejects_wrong_version(self) -> None:
        message = make_envelope("ping", {}, "survival")
        message["v"] = 99
        with self.assertRaises(ProtocolError):
            decode(encode(message), "fallback")


if __name__ == "__main__":
    unittest.main()
