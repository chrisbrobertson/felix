"""E2E smoke tests for Circles commands."""
import json
import yaml
import pytest
from pathlib import Path

pytestmark = pytest.mark.asyncio


def _make_circle_yaml(deploy_dir: Path, slug: str = "family") -> Path:
    """Write a minimal circle YAML ruleset to deploy_dir/circles/."""
    circles_dir = deploy_dir / "circles"
    circles_dir.mkdir(exist_ok=True)
    p = circles_dir / f"{slug}.yaml"
    p.write_text(yaml.dump({
        "circle": slug,
        "display_name": slug.title(),
        "members": [],
        "bot_token": "",
        "icloud_folder": f"second-brain-circles/{slug}/memories",
        "rules": {"include": [{"type": "calendar_event"}], "exclude": []},
    }))
    return p


async def test_circles_smoke(handler, mk_update, brain_dir, deploy_dir):
    _make_circle_yaml(deploy_dir)
    update, ctx = mk_update("/circles")
    await handler.cmd_circles(update, ctx)
    update.message.reply_text.assert_called()


async def test_circle_smoke(handler, mk_update, brain_dir, deploy_dir):
    _make_circle_yaml(deploy_dir)
    # Populate list
    update, ctx = mk_update("/circles")
    await handler.cmd_circles(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/circle", args=["1"])
    await handler.cmd_circle(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_circle_status_smoke(handler, mk_update, brain_dir, deploy_dir):
    _make_circle_yaml(deploy_dir)
    update, ctx = mk_update("/circle_status")
    await handler.cmd_circle_status(update, ctx)
    update.message.reply_text.assert_called()


async def test_circle_rule_smoke(handler, mk_update, brain_dir, deploy_dir):
    _make_circle_yaml(deploy_dir)
    # Populate list first
    update, ctx = mk_update("/circles")
    await handler.cmd_circles(update, ctx)
    # Run circle_rule add
    update2, ctx2 = mk_update("/circle_rule", args=["add", "1", "include", "type:goal"])
    await handler.cmd_circle_rule(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_circle_invite_smoke(handler, mk_update, brain_dir, deploy_dir):
    _make_circle_yaml(deploy_dir)
    # Populate list first
    update, ctx = mk_update("/circles")
    await handler.cmd_circles(update, ctx)
    # Generate an invite
    update2, ctx2 = mk_update("/circle_invite", args=["1"])
    await handler.cmd_circle_invite(update2, ctx2)
    update2.message.reply_text.assert_called()
