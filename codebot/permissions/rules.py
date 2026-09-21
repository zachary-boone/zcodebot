from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

log = logging.getLogger(__name__)

Effect = Literal["allow", "deny"]

_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")

_CONTENT_FIELDS: dict[str, str] = {
    "Bash": "command",
    "ReadFile": "file_path",
    "WriteFile": "file_path",
    "EditFile": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}
_FILE_TOOLS = frozenset({"ReadFile", "WriteFile", "EditFile"})
_SHELL_METACHARS = (";", "&&", "||", "|", "$(", "`", "\n")


def normalize_content(tool_name: str, content: str) -> str:
    if tool_name in _FILE_TOOLS and content:
        return os.path.normcase(os.path.abspath(content))
    return content


def _is_under(path: str, root: str) -> bool:
    try:
        Path(path).relative_to(Path(root))
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class Rule:
    tool_name: str
    pattern: str
    effect: Effect


    def matches(self, tool_name: str, content: str) -> bool:
        if self.tool_name != tool_name:
            return False
        content = normalize_content(tool_name, content)
        if self.effect == "allow" and tool_name == "Bash":
            if any(marker in content for marker in _SHELL_METACHARS):
                return False
        if self.pattern.startswith("dir:"):
            return _is_under(content, normalize_content(tool_name, self.pattern[4:]))
        return content == normalize_content(tool_name, self.pattern)


def parse_rule(raw: str, effect: Effect) -> Rule:
    m = _RULE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"无效的规则语法: {raw}")
    pattern = m.group(2)
    if pattern.endswith("*") and not pattern.startswith("dir:"):
        log.warning("旧版授权规则 %s 使用通配符，已按精确匹配处理", raw.strip())
        pattern = pattern[:-1]
    tool_name = m.group(1)
    if pattern.startswith("dir:"):
        pattern = "dir:" + normalize_content(tool_name, pattern[4:])
    else:
        pattern = normalize_content(tool_name, pattern)
    return Rule(tool_name=tool_name, pattern=pattern, effect=effect)


def extract_content(tool_name: str, arguments: dict[str, Any]) -> str:
    field = _CONTENT_FIELDS.get(tool_name)
    if field is None:
        return ""
    return str(arguments.get(field, ""))


def _load_rules_file(path: Path) -> list[Rule]:
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rule_str = entry.get("rule", "")
        effect = entry.get("effect", "")
        if effect not in ("allow", "deny"):
            continue
        try:
            rules.append(parse_rule(rule_str, effect))
        except ValueError:
            continue
    return rules


class RuleEngine:


    def __init__(
        self,
        user_rules_path: Path | None = None,
        project_rules_path: Path | None = None,
        local_rules_path: Path | None = None,
    ) -> None:
        self._user_path = user_rules_path
        self._project_path = project_rules_path
        self._local_path = local_rules_path
        if self._local_path and self._local_path.is_file():
            count = len(_load_rules_file(self._local_path))
            if count:
                log.info("已加载 %d 条本地授权规则：%s", count, self._local_path)

    def _load_tiers(self) -> list[list[Rule]]:
        tiers: list[list[Rule]] = []
        for p in (self._local_path, self._project_path, self._user_path):
            tiers.append(_load_rules_file(p) if p else [])
        return tiers


    def evaluate(self, tool_name: str, content: str) -> Effect | None:
        for rules in self._load_tiers():
            for rule in reversed(rules):
                if rule.matches(tool_name, content):
                    return rule.effect
        return None


    def append_local_rule(self, rule: Rule) -> None:
        if self._local_path is None:
            return
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_rules_file(self._local_path)
        if any(
            r.tool_name == rule.tool_name
            and r.pattern == rule.pattern
            and r.effect == rule.effect
            for r in existing
        ):
            return
        existing.append(rule)
        entries = [
            {
                "rule": f"{r.tool_name}({r.pattern})",
                "effect": r.effect,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            for r in existing
        ]
        self._local_path.write_text(yaml.dump(entries, allow_unicode=True), encoding="utf-8")
