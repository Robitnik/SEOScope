"""Group-aware robots.txt parser + semantic diff (task-35 §1.2/§3.5).

A raw text diff of robots.txt is not safe to alert on: real files carry
inline comments (`Disallow: /wp- # all files WP: ...`) and per-bot groups
that are irrelevant to us (`Disallow: /` under `User-agent: meta-externalagent`
does not mean the site is closed to Google). This module parses into
"user-agent group -> rules" and only that structure is ever diffed or
severity-scored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The only bots whose blocking actually matters to us (task-35 §1.2/§3.4).
# Everything else (GPTBot, meta-externalagent, ...) is `info`, never `critical`.
RELEVANT_USER_AGENTS = frozenset({"*", "googlebot", "googlebot-image", "bingbot"})


@dataclass(frozen=True)
class RobotsRule:
    directive: str  # "disallow" | "allow"
    path: str


@dataclass(frozen=True)
class RobotsGroup:
    user_agents: tuple[str, ...]  # as written, case preserved for display
    rules: tuple[RobotsRule, ...]
    crawl_delay: float | None = None

    @property
    def user_agents_ci(self) -> frozenset[str]:
        return frozenset(ua.lower() for ua in self.user_agents)


@dataclass(frozen=True)
class RobotsDocument:
    groups: tuple[RobotsGroup, ...] = ()
    sitemaps: tuple[str, ...] = ()


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def parse_robots(text: str) -> RobotsDocument:
    groups: list[RobotsGroup] = []
    sitemaps: list[str] = []

    current_agents: list[str] = []
    current_rules: list[RobotsRule] = []
    current_crawl_delay: float | None = None
    started_rules = False

    def flush() -> None:
        nonlocal current_agents, current_rules, current_crawl_delay, started_rules
        if current_agents:
            groups.append(
                RobotsGroup(
                    user_agents=tuple(current_agents),
                    rules=tuple(current_rules),
                    crawl_delay=current_crawl_delay,
                )
            )
        current_agents = []
        current_rules = []
        current_crawl_delay = None
        started_rules = False

    for raw_line in text.splitlines():
        line = _strip_comment(raw_line)
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()
        if not value and field_name not in {"disallow", "allow"}:
            continue

        if field_name == "user-agent":
            if started_rules:
                flush()
            current_agents.append(value)
        elif field_name == "disallow":
            started_rules = True
            current_rules.append(RobotsRule("disallow", value))
        elif field_name == "allow":
            started_rules = True
            current_rules.append(RobotsRule("allow", value))
        elif field_name == "crawl-delay":
            started_rules = True
            try:
                current_crawl_delay = float(value)
            except ValueError:
                pass
        elif field_name == "sitemap" and value:
            sitemaps.append(value)

    flush()
    return RobotsDocument(groups=tuple(groups), sitemaps=tuple(sitemaps))


def blocking_relevant_groups(doc: RobotsDocument) -> list[str]:
    """User-agent tokens (as written) whose group has a literal `Disallow: /`
    AND belongs to `RELEVANT_USER_AGENTS` (task-35 §1.2/§3.4 `robots_blocks_site`).
    """
    blocked: list[str] = []
    for group in doc.groups:
        if not group.user_agents_ci & RELEVANT_USER_AGENTS:
            continue
        if any(rule.directive == "disallow" and rule.path == "/" for rule in group.rules):
            blocked.extend(ua for ua in group.user_agents if ua.lower() in RELEVANT_USER_AGENTS)
    return blocked


def crawl_delay_for(doc: RobotsDocument, user_agent: str = "*") -> float | None:
    for group in doc.groups:
        if user_agent.lower() in group.user_agents_ci and group.crawl_delay is not None:
            return group.crawl_delay
    return None


def to_structure(doc: RobotsDocument) -> dict[str, Any]:
    return {
        "groups": [
            {
                "user_agents": list(group.user_agents),
                "rules": [{"directive": r.directive, "path": r.path} for r in group.rules],
                "crawl_delay": group.crawl_delay,
            }
            for group in doc.groups
        ],
        "sitemaps": list(doc.sitemaps),
    }


def from_structure(data: dict[str, Any]) -> RobotsDocument:
    groups = tuple(
        RobotsGroup(
            user_agents=tuple(g.get("user_agents", [])),
            rules=tuple(RobotsRule(r["directive"], r["path"]) for r in g.get("rules", [])),
            crawl_delay=g.get("crawl_delay"),
        )
        for g in data.get("groups", [])
    )
    return RobotsDocument(groups=groups, sitemaps=tuple(data.get("sitemaps", [])))


def _group_key(group_data: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(ua.lower() for ua in group_data.get("user_agents", [])))


def diff_structures(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """Field-level diff for `changed_fields_json` — groups matched by their
    (lowercased, sorted) user-agent set, not by list position.
    """
    if old is None:
        return ["groups", "sitemaps"] if new.get("groups") or new.get("sitemaps") else []

    changed: list[str] = []
    old_groups = {_group_key(g): g for g in old.get("groups", [])}
    new_groups = {_group_key(g): g for g in new.get("groups", [])}
    for key in old_groups.keys() | new_groups.keys():
        label = f"group:{'+'.join(key) or '(empty)'}"
        if key not in old_groups or key not in new_groups:
            changed.append(label)
        elif old_groups[key] != new_groups[key]:
            changed.append(label)

    if set(old.get("sitemaps", [])) != set(new.get("sitemaps", [])):
        changed.append("sitemaps")

    return changed
