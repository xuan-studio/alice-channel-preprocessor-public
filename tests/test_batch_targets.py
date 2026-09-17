from app.extractors import parse_batch_channel_targets


def test_batch_target_parser_handles_links_mentions_and_duplicates() -> None:
    parsed = parse_batch_channel_targets(
        """
        https://t.me/northstar_demo
        https://t.me/HarborLabDemo
        @signalWorkshopDemo
        https://t.me/example_research_demo
        @NORTHSTAR_DEMO
        """
    )
    assert [item["username"] for item in parsed["targets"]] == [
        "northstar_demo", "harborlabdemo", "signalworkshopdemo", "example_research_demo",
    ]
    assert parsed["duplicates"] == [{"target": "@NORTHSTAR_DEMO", "username": "northstar_demo"}]
    assert parsed["invalid"] == []


def test_batch_target_parser_reports_invalid_lines() -> None:
    parsed = parse_batch_channel_targets(
        """
        @valid_channel
        not a telegram target
        https://t.me/+privateInvite
        """
    )
    assert [item["username"] for item in parsed["targets"]] == ["valid_channel"]
    assert [item["target"] for item in parsed["invalid"]] == ["not a telegram target", "https://t.me/+privateInvite"]
