from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import unquote

_VERBOSE = os.environ.get("WIKI_LINT_VERBOSE") in {"1", "true", "yes"}

ALLOWED_PAGE_TYPES = frozenset(
    {
        "analysis",
        "architecture",
        "capability",
        "concept",
        "decision",
        "domain",
        "entity",
        "glossary",
        "index",
        "interface",
        "journey",
        "log",
        "map",
        "operation",
        "overview",
        "requirement",
        "source",
    }
)
ALLOWED_STATUSES = frozenset(
    {
        "deprecated",
        "historical",
        "implemented",
        "maintained",
        "partially-implemented",
        "proposed",
        "source",
    }
)
REQUIRED_PROPERTIES = frozenset({"type", "title", "status", "audiences", "tags", "last_verified"})
REQUIREMENT_SECTIONS = (
    "Intent",
    "Current behavior",
    "Gap",
    "Acceptance criteria",
    "Implementation evidence",
    "Test evidence",
    "Related",
)

_FRONTMATTER_KEY = re.compile(r"^([a-z][a-z0-9_-]*):(?:\s*(.*))?$")
_HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_KEBAB_FILENAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")


@dataclass(frozen=True, slots=True)
class WikiLintIssue:
    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.code}: {self.message}"


@dataclass(frozen=True, slots=True)
class _Page:
    path: Path
    relative_path: str
    content: str
    body: str
    properties: dict[str, str | list[str]]


def _parse_frontmatter(content: str) -> tuple[dict[str, str | list[str]], str]:
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        return {}, content
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, content

    properties: dict[str, str | list[str]] = {}
    active_list: list[str] | None = None
    for line in lines[1:end]:
        key_match = _FRONTMATTER_KEY.match(line)
        if key_match is not None:
            key, value = key_match.groups()
            if value:
                properties[key] = value.strip().strip("\"'")
                active_list = None
            else:
                active_list = []
                properties[key] = active_list
            continue
        if active_list is not None and line.startswith("  - "):
            active_list.append(line[4:].strip().strip("\"'"))

    return properties, "\n".join(lines[end + 1 :])


def _managed_markdown_paths(root: Path) -> list[Path]:
    t0 = time.perf_counter()
    wiki_dir = root / "wiki"
    paths = list(wiki_dir.rglob("*.md")) if wiki_dir.is_dir() else []
    home = root / "Home.md"
    if home.is_file():
        paths.append(home)
    result = sorted(paths)
    t1 = time.perf_counter()
    if _VERBOSE:
        print(
            f"[lint] _managed_markdown_paths: found {len(result)} files in {t1 - t0:.3f}s",
            flush=True,
        )
    return result


def _load_pages(root: Path) -> list[_Page]:
    t0 = time.perf_counter()
    pages: list[_Page] = []
    file_paths = _managed_markdown_paths(root)
    t1 = time.perf_counter()
    for path in file_paths:
        content = path.read_text(encoding="utf-8")
        properties, body = _parse_frontmatter(content)
        pages.append(
            _Page(
                path=path,
                relative_path=path.relative_to(root).as_posix(),
                content=content,
                body=body,
                properties=properties,
            )
        )
    t2 = time.perf_counter()
    if _VERBOSE:
        print(
            f"[lint] _load_pages: read+parsed {len(pages)} pages in "
            f"{t2 - t1:.3f}s (discovery {t1 - t0:.3f}s)",
            flush=True,
        )
    return pages


def _metadata_issues(page: _Page) -> list[WikiLintIssue]:
    issues: list[WikiLintIssue] = []
    missing = sorted(REQUIRED_PROPERTIES - page.properties.keys())
    if missing:
        issues.append(
            WikiLintIssue(
                page.relative_path,
                "metadata",
                f"missing required properties: {', '.join(missing)}",
            )
        )
        return issues

    page_type = page.properties["type"]
    if not isinstance(page_type, str) or page_type not in ALLOWED_PAGE_TYPES:
        issues.append(
            WikiLintIssue(page.relative_path, "type", f"unsupported page type: {page_type!r}")
        )

    status = page.properties["status"]
    if not isinstance(status, str) or status not in ALLOWED_STATUSES:
        issues.append(
            WikiLintIssue(page.relative_path, "status", f"unsupported status: {status!r}")
        )

    for property_name in ("audiences", "tags"):
        value = page.properties[property_name]
        if not isinstance(value, list) or not value:
            issues.append(
                WikiLintIssue(
                    page.relative_path,
                    "metadata",
                    f"{property_name} must be a non-empty YAML list",
                )
            )

    tags = page.properties["tags"]
    expected_tag = f"type/{page_type}"
    if isinstance(tags, list) and expected_tag not in tags:
        issues.append(
            WikiLintIssue(
                page.relative_path,
                "tags",
                f"missing page-type tag {expected_tag!r}",
            )
        )

    verified = page.properties["last_verified"]
    try:
        if not isinstance(verified, str):
            raise ValueError
        date.fromisoformat(verified)
    except ValueError:
        issues.append(
            WikiLintIssue(
                page.relative_path,
                "last-verified",
                "last_verified must be an ISO date",
            )
        )

    if status == "implemented" and not page.properties.get("verified_against_commit"):
        issues.append(
            WikiLintIssue(
                page.relative_path,
                "verification",
                "implemented pages must identify verified_against_commit",
            )
        )

    title = page.properties["title"]
    heading = _HEADING.search(page.body)
    if heading is None:
        issues.append(WikiLintIssue(page.relative_path, "heading", "missing H1 heading"))
    elif not isinstance(title, str) or heading.group(1).strip() != title:
        issues.append(
            WikiLintIssue(
                page.relative_path,
                "heading",
                "H1 heading must exactly match the title property",
            )
        )

    if page.path.name != "Home.md" and not _KEBAB_FILENAME.fullmatch(page.path.name):
        issues.append(
            WikiLintIssue(
                page.relative_path,
                "filename",
                "generated page filenames must use kebab-case",
            )
        )

    if page_type == "requirement":
        headings = set(re.findall(r"^##\s+(.+?)\s*$", page.body, re.MULTILINE))
        for section in REQUIREMENT_SECTIONS:
            if section not in headings:
                issues.append(
                    WikiLintIssue(
                        page.relative_path,
                        "requirement-section",
                        f"missing section: {section}",
                    )
                )

    return issues


def _destination_path(page: _Page, destination: str) -> Path | None:
    destination = destination.strip()
    if destination.startswith("<") and destination.endswith(">"):
        destination = destination[1:-1]
    if not destination or destination.startswith("#"):
        return None
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", destination):
        return None
    destination = unquote(destination.split("#", 1)[0].split("?", 1)[0])
    if not destination:
        return None
    return (page.path.parent / destination).resolve()


def lint_wiki(root: Path) -> list[WikiLintIssue]:
    t_start = time.perf_counter()
    root = root.resolve()
    if _VERBOSE:
        print(f"[lint] lint_wiki starting on root={root}", flush=True)

    pages = _load_pages(root)
    t_loaded = time.perf_counter()
    if _VERBOSE:
        print(
            f"[lint] pages loaded: {len(pages)} in {t_loaded - t_start:.3f}s total so far",
            flush=True,
        )

    issues: list[WikiLintIssue] = []
    known_pages = {page.path.resolve(): page for page in pages}
    inbound_counts = {path: 0 for path in known_pages}
    titles: dict[str, str] = {}

    link_checks = 0
    for page in pages:
        issues.extend(_metadata_issues(page))

        title = page.properties.get("title")
        if isinstance(title, str):
            normalized = title.casefold()
            if normalized in titles:
                issues.append(
                    WikiLintIssue(
                        page.relative_path,
                        "duplicate-title",
                        f"title duplicates {titles[normalized]}",
                    )
                )
            else:
                titles[normalized] = page.relative_path

        for match in _LINK.finditer(page.body):
            link_checks += 1
            target = _destination_path(page, match.group(1))
            if target is None:
                continue
            try:
                target.relative_to(root)
            except ValueError:
                issues.append(
                    WikiLintIssue(
                        page.relative_path,
                        "link-outside-vault",
                        f"link leaves the vault: {match.group(1)}",
                    )
                )
                continue
            if not target.is_file():
                issues.append(
                    WikiLintIssue(
                        page.relative_path,
                        "broken-link",
                        f"target does not exist: {match.group(1)}",
                    )
                )
            elif target in inbound_counts and target != page.path.resolve():
                inbound_counts[target] += 1
    t_checks = time.perf_counter()
    if _VERBOSE:
        print(
            f"[lint] metadata+links processed ({link_checks} links checked) "
            f"in {t_checks - t_loaded:.3f}s",
            flush=True,
        )

    orphan_start = time.perf_counter()
    orphan_exemptions = {root / "Home.md", root / "wiki" / "index.md"}
    for path, count in inbound_counts.items():
        if count == 0 and path not in orphan_exemptions:
            issues.append(
                WikiLintIssue(
                    path.relative_to(root).as_posix(),
                    "orphan",
                    "page has no inbound links from another managed page",
                )
            )
    t_orphans = time.perf_counter()
    if _VERBOSE:
        print(f"[lint] orphan check done in {t_orphans - orphan_start:.3f}s", flush=True)

    result = sorted(issues, key=lambda issue: (issue.path, issue.code, issue.message))
    t_end = time.perf_counter()
    if _VERBOSE:
        print(
            f"[lint] lint_wiki total: {t_end - t_start:.3f}s, found {len(result)} issues",
            flush=True,
        )
    return result
