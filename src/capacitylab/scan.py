# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scan the project for identities and internal references that must not ship.

Generic rules always run. Two optional files, both git-ignored, add project-specific checks without
putting the terms themselves in the repository: `.local/denylist.json` (HMAC hashes of words) and
`.local/patterns.json` (named regular expressions).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path

TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".json", ".html", ".css", ".js", ".cfg", ".ini",
                 ".sh", ".example", ".svg"}
TEXT_NAMES = {"Makefile", "Dockerfile", ".gitignore", ".env.example"}
EXCLUDED_DIRS = {".venv", ".git", "node_modules", ".local", "runs", "__pycache__", ".pytest_cache", ".ruff_cache", "_frames"}
SCANNED_CATEGORIES = ("company_people_hosts", "infra_identifiers", "schema_names", "db_users")

_DOC_IPS = re.compile(r"^(127\.0\.0\.1|0\.0\.0\.0|192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+)$")
GENERIC_RULES: dict[str, re.Pattern] = {
    "aws_account_id": re.compile(r"(?<![\w.])\d{12}(?![\w.])"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}"),
    "ipv4": re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"),
    "home_path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+"),
    "rds_endpoint": re.compile(r"\.rds\.amazonaws\.com"),
    "aws_arn": re.compile("arn" + r":aws:"),  # split so this file does not match itself
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}
ALLOWED_EMAIL_DOMAINS = re.compile(r"@(example\.(com|org|net)|anthropic\.com)$", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def _is_text(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and not any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in path.relative_to(root).parts):
            if _is_text(path):
                yield path


def load_denylist(path: Path) -> tuple[bytes, dict[str, set[str]]] | None:
    if not path.is_file():
        return None
    doc = json.loads(path.read_text())
    return bytes.fromhex(doc["key"]), {k: set(v) for k, v in doc["categories"].items()}


def hmac_token(key: bytes, token: str) -> str:
    return hmac.new(key, token.lower().encode(), hashlib.sha256).hexdigest()


def tokens(line: str) -> set[str]:
    words = set(re.findall(r"[A-Za-z0-9_]{3,}", line))
    words |= set(re.findall(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)+", line))
    return words


def load_patterns(path: Path | None) -> dict[str, re.Pattern]:
    """Named regular expressions from a local, git-ignored file: {"rule_name": "pattern"}."""
    if path is None or not path.is_file():
        return {}
    return {name: re.compile(pattern, re.IGNORECASE) for name, pattern in json.loads(path.read_text()).items()}


def scan(root: Path, denylist: Path | None = None, patterns: Path | None = None) -> list[Finding]:
    root = root.resolve()
    deny = load_denylist(denylist) if denylist else None
    rules = {**GENERIC_RULES, **load_patterns(patterns)}
    findings: list[Finding] = []
    for path in iter_files(root):
        rel = str(path.relative_to(root))
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, start=1):
            for rule, pattern in rules.items():
                for m in pattern.finditer(line):
                    text = m.group(0)
                    if rule == "email" and ALLOWED_EMAIL_DOMAINS.search(text):
                        continue
                    if rule == "ipv4" and (_DOC_IPS.match(text) or any(int(p) > 255 for p in text.split("."))):
                        continue
                    findings.append(Finding(rel, n, rule))
                    break
            if deny:
                key, categories = deny
                hashed = {hmac_token(key, t) for t in tokens(line)}
                for category in SCANNED_CATEGORIES:
                    if hashed & categories.get(category, set()):
                        findings.append(Finding(rel, n, f"denylist:{category}"))
    return findings
