import unittest

from app.mentions import (
    MentionTarget,
    bind_token_notice_content,
    game_chat_content,
    multiline_text_content,
    replace_matrix_mentions,
    user_notice_content,
)


class MentionsTest(unittest.TestCase):
    def test_bound_player_ids_become_matrix_mentions(self) -> None:
        targets = [
            MentionTarget("Steve", "@alice:example.org"),
            MentionTarget("survival:Alex", "@bob:example.org"),
        ]
        content = game_chat_content(
            "原版服", "Sender", "你好 @steve、@survival:Alex！<script>", targets
        )
        self.assertEqual(
            ["@alice:example.org", "@bob:example.org"],
            content["m.mentions"]["user_ids"],
        )
        self.assertTrue(content["body"].startswith("[原版服]<Sender> "))
        self.assertIn("&lt;script&gt;", content["formatted_body"])
        self.assertNotIn("<script>", content["formatted_body"])

    def test_partial_player_id_does_not_mention(self) -> None:
        content = game_chat_content(
            "原版服",
            "Sender",
            "@SteveOther",
            [MentionTarget("Steve", "@alice:example.org")],
        )
        self.assertEqual({}, content["m.mentions"])

    def test_player_avatar_is_embedded_with_plain_text_fallback(self) -> None:
        content = game_chat_content(
            "原版服",
            "Steve",
            "大家好",
            [],
            "mxc://chat.example.org/avatar123",
        )
        self.assertEqual("[原版服]<Steve> 大家好", content["body"])
        self.assertIn(
            '<img src="mxc://chat.example.org/avatar123" width="24" height="24"',
            content["formatted_body"],
        )
        self.assertIn("[Steve头像]", content["formatted_body"])

    def test_untrusted_avatar_uri_is_not_embedded(self) -> None:
        content = game_chat_content(
            "原版服", "Steve", "hello", [], "https://evil.example/avatar.png"
        )
        self.assertNotIn("formatted_body", content)

    def test_matrix_ids_are_replaced_with_game_ids(self) -> None:
        body = "你好 @alice:example.org 和 @bob:example.org"
        result = replace_matrix_mentions(
            body,
            ["@alice:example.org", "@bob:example.org"],
            {
                "@alice:example.org": "Steve",
                "@bob:example.org": "survival:Alex",
            },
        )
        self.assertEqual("你好 @Steve 和 @survival:Alex", result)

    def test_unbound_matrix_mention_is_left_unchanged(self) -> None:
        body = "你好 @unbound:example.org"
        result = replace_matrix_mentions(
            body,
            ["@unbound:example.org"],
            {"@unbound:example.org": "@unbound:example.org"},
        )
        self.assertEqual(body, result)

    def test_notice_mentions_only_target_user(self) -> None:
        content = user_notice_content("@alice:example.org", "第一行\n第二行<script>")
        self.assertEqual(
            ["@alice:example.org"], content["m.mentions"]["user_ids"]
        )
        self.assertIn("<strong>第一行</strong><br>\n第二行", content["formatted_body"])
        self.assertIn("&lt;script&gt;", content["formatted_body"])
        self.assertNotIn("<script>", content["formatted_body"])

    def test_multiline_text_content_preserves_each_line_in_html(self) -> None:
        content = multiline_text_content("标题\nSteve\nAlex")
        self.assertEqual("标题\nSteve\nAlex", content["body"])
        self.assertEqual(
            "<strong>标题</strong><br>\nSteve<br>\nAlex",
            content["formatted_body"],
        )

    def test_bind_token_is_in_copy_friendly_code_blocks(self) -> None:
        content = bind_token_notice_content(
            "@alice:example.org", "ABC234", 600, "Steve"
        )
        self.assertIn("\nABC234\n", content["body"])
        self.assertIn("<pre><code>ABC234</code></pre>", content["formatted_body"])


if __name__ == "__main__":
    unittest.main()
