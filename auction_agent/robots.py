"""robots.txt matching per RFC 9309, independent of the Python version.

The stdlib ``urllib.robotparser`` only supports wildcards on recent CPython releases,
applies the first matching rule instead of the longest, and drops repeated groups, so
results would differ between the owner's Windows Python and the server. This matcher:
- merges every group that names the same user agent;
- uses the group matching our product token, else the ``*`` group;
- supports ``*`` and ``$`` in paths;
- applies the longest matching rule, with Allow winning ties.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


@dataclass
class RobotsRules:
    groups: dict[str, list[_Rule]] = field(default_factory=dict)

    def rules_for(self, token: str) -> list[_Rule]:
        token = token.lower()
        for agent, rules in self.groups.items():
            if agent != "*" and (agent == token or agent in token):
                return rules
        return self.groups.get("*", [])

    def can_fetch(self, token: str, url: str) -> bool:
        parts = urlsplit(url)
        path = unquote(parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if parts.path == "/robots.txt":
            return True
        best: _Rule | None = None
        for rule in self.rules_for(token):
            if rule.regex.match(path):
                if best is None or len(rule.pattern) > len(best.pattern) or (
                    len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow
                ):
                    best = rule
        return True if best is None else best.allow


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in unquote(body))
    return re.compile(regex + ("$" if anchored else ""))


def parse_robots(text: str) -> RobotsRules:
    rules = RobotsRules()
    current_agents: list[str] = []
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:  # a new group starts
                current_agents, in_rules = [], False
            agent = value.lower()
            current_agents.append(agent)
            rules.groups.setdefault(agent, [])
        elif key in ("allow", "disallow"):
            in_rules = True
            if not current_agents or (key == "disallow" and not value):
                continue  # rules before any group, or an empty Disallow (= allow all)
            if not value.startswith("/") and not value.startswith("*"):
                value = "/" + value
            rule = _Rule(allow=(key == "allow"), pattern=value, regex=_compile(value))
            for agent in current_agents:
                rules.groups[agent].append(rule)
    return rules
