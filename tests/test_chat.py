"""Room chat and reactions (0.9): cleaning text, the limits, what the room messages may hold."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syncwatch.net import protocol  # noqa: E402
from syncwatch.sync import chat  # noqa: E402


class ChatTextTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(chat.clean("  hello \n  room\t!  "), "hello room !")
        self.assertEqual(chat.clean("a\x00b\x1bc"), "abc")
        self.assertEqual(len(chat.clean("x" * 1000)), protocol.CHAT_TEXT)
        self.assertEqual(chat.clean(""), "")
        self.assertEqual(chat.clean(None), "")

    def test_limiter(self):
        limiter = chat.Limiter(3, 5.0)
        self.assertEqual([limiter.allow("a", t) for t in (0, 1, 2, 3)], [True, True, True, False])
        self.assertTrue(limiter.allow("b", 3))           # per person
        self.assertTrue(limiter.allow("a", 5.5))         # the window moved on
        limiter.clear()
        self.assertTrue(limiter.allow("a", 5.6))


class ChatProtocolTests(unittest.TestCase):
    def check(self, **body):
        return protocol.validate(protocol.ROOM, {"s": "0" * 16, "n": 1, **body})

    def test_chat_and_react_messages(self):
        self.assertEqual(self.check(t="chat", text="hi <b>there</b>")["text"], "hi <b>there</b>")  # shown as text
        hidden = self.check(t="chat", text="a‮b​c\x07d")["text"]        # bidi, zero-width, bell
        self.assertEqual(hidden, "abcd")
        for emoji in protocol.REACTIONS:
            self.assertEqual(self.check(t="react", e=emoji)["e"], emoji)
        for bad in ({"t": "chat", "text": "x" * (protocol.CHAT_TEXT + 1)}, {"t": "chat"}, {"t": "chat", "text": 5},
                    {"t": "react", "e": "💩"}, {"t": "react", "e": "<img src=x>"}, {"t": "react"}):
            with self.subTest(bad=str(bad)[:40]), self.assertRaises(protocol.BadMessage):
                self.check(**bad)


if __name__ == "__main__":
    unittest.main()
