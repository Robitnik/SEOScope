"""robots.txt parsing + semantic diff (task-35 §1.2/§5)."""

from __future__ import annotations

from app.monitor.robots import (
    blocking_relevant_groups,
    crawl_delay_for,
    diff_structures,
    from_structure,
    parse_robots,
    to_structure,
)

# Live regression: exxxoticaexpo.com (task-35 §1.2) — `Disallow: /wp- # comment`
# and a `Disallow: /` under an AI-bot group that must NOT read as "site blocked".
LIVE_EXXXOTICAEXPO_ROBOTS = """
User-agent: *
Disallow: /wp- # all files WP: /wp-json/, /wp-includes, /wp-content/plugins
Disallow: /cart/

User-agent: meta-externalagent
Disallow: /

User-agent: GPTBot
Disallow: /
Crawl-delay: 5

Sitemap: https://exxxoticaexpo.com/sitemap_index.xml
""".strip()


def test_inline_comments_are_stripped_from_directive_values():
    doc = parse_robots(LIVE_EXXXOTICAEXPO_ROBOTS)
    star_group = next(g for g in doc.groups if g.user_agents == ("*",))
    assert ("disallow", "/wp-") in [(r.directive, r.path) for r in star_group.rules]
    # The trailing comment must never leak into the path.
    assert all("#" not in rule.path for rule in star_group.rules)


def test_disallow_root_for_an_ai_bot_group_is_not_a_site_block():
    doc = parse_robots(LIVE_EXXXOTICAEXPO_ROBOTS)
    assert blocking_relevant_groups(doc) == []


def test_disallow_root_for_star_is_a_site_block():
    doc = parse_robots("User-agent: *\nDisallow: /\n")
    assert blocking_relevant_groups(doc) == ["*"]


def test_disallow_root_for_googlebot_is_a_site_block():
    doc = parse_robots("User-agent: Googlebot\nDisallow: /\n")
    assert blocking_relevant_groups(doc) == ["Googlebot"]


def test_wildcard_group_with_partial_disallow_does_not_block():
    doc = parse_robots("User-agent: *\nDisallow: /admin/\n")
    assert blocking_relevant_groups(doc) == []


def test_consecutive_user_agent_lines_share_one_group():
    doc = parse_robots("User-agent: a\nUser-agent: b\nDisallow: /x\n")
    assert len(doc.groups) == 1
    assert doc.groups[0].user_agents == ("a", "b")


def test_separate_user_agent_blocks_are_separate_groups():
    doc = parse_robots("User-agent: a\nDisallow: /x\nUser-agent: b\nDisallow: /y\n")
    assert len(doc.groups) == 2


def test_crawl_delay_is_read_per_group():
    doc = parse_robots(LIVE_EXXXOTICAEXPO_ROBOTS)
    assert crawl_delay_for(doc, "GPTBot") == 5.0
    assert crawl_delay_for(doc, "*") is None


def test_sitemap_directive_is_collected_regardless_of_group_position():
    doc = parse_robots(LIVE_EXXXOTICAEXPO_ROBOTS)
    assert doc.sitemaps == ("https://exxxoticaexpo.com/sitemap_index.xml",)


def test_structure_roundtrips_through_to_and_from_structure():
    doc = parse_robots(LIVE_EXXXOTICAEXPO_ROBOTS)
    restored = from_structure(to_structure(doc))
    assert to_structure(restored) == to_structure(doc)


def test_diff_structures_reports_baseline_as_everything_when_content_exists():
    doc = parse_robots("User-agent: *\nDisallow: /x\n")
    assert diff_structures(None, to_structure(doc)) == ["groups", "sitemaps"]


def test_diff_structures_empty_baseline_is_empty():
    empty = to_structure(parse_robots(""))
    assert diff_structures(None, empty) == []


def test_diff_structures_matches_groups_by_user_agent_set_not_position():
    old = to_structure(parse_robots("User-agent: a\nDisallow: /x\nUser-agent: b\nDisallow: /y\n"))
    # Same two groups, reordered and with `b`'s rule unchanged, `a`'s rule changed.
    new = to_structure(parse_robots("User-agent: b\nDisallow: /y\nUser-agent: a\nDisallow: /z\n"))
    assert diff_structures(old, new) == ["group:a"]


def test_diff_structures_detects_new_and_removed_groups():
    old = to_structure(parse_robots("User-agent: *\nDisallow: /x\n"))
    new = to_structure(
        parse_robots("User-agent: *\nDisallow: /x\nUser-agent: Bingbot\nDisallow: /\n")
    )
    assert diff_structures(old, new) == ["group:bingbot"]


def test_diff_structures_detects_sitemap_change():
    old = to_structure(parse_robots("Sitemap: https://x.com/a.xml\n"))
    new = to_structure(parse_robots("Sitemap: https://x.com/b.xml\n"))
    assert diff_structures(old, new) == ["sitemaps"]


def test_no_change_is_no_diff():
    doc = to_structure(parse_robots(LIVE_EXXXOTICAEXPO_ROBOTS))
    assert diff_structures(doc, doc) == []


def test_missing_robots_parses_empty():
    doc = parse_robots("")
    assert doc.groups == ()
    assert doc.sitemaps == ()
    assert blocking_relevant_groups(doc) == []
