from __future__ import annotations

from pathlib import Path

import pytest

from talktoharnesses.wiki_lint import lint_wiki


def write_page(
    root: Path,
    relative_path: str,
    *,
    title: str,
    page_type: str = "map",
    status: str = "maintained",
    body: str = "",
) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    commit = "verified_against_commit: abc1234\n" if status == "implemented" else ""
    path.write_text(
        "\n".join(
            (
                "---",
                f"type: {page_type}",
                f"title: {title}",
                f"status: {status}",
                "audiences:",
                "  - developer",
                "tags:",
                f"  - type/{page_type}",
                "last_verified: 2026-07-11",
                commit.rstrip(),
                "---",
                "",
                f"# {title}",
                "",
                body,
            )
        ),
        encoding="utf-8",
    )


def test_lint_wiki_accepts_linked_pages_with_valid_metadata(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "Home.md",
        title="Home",
        body="[Capability](wiki/capabilities/example.md)",
    )
    write_page(
        tmp_path,
        "wiki/capabilities/example.md",
        title="Example Capability",
        page_type="capability",
        status="implemented",
        body="[Home](../../Home.md)",
    )

    assert lint_wiki(tmp_path) == []


@pytest.mark.parametrize("page_type", ("concept", "entity", "source"))
def test_lint_wiki_accepts_all_documented_page_types(tmp_path: Path, page_type: str) -> None:
    write_page(
        tmp_path,
        "Home.md",
        title="Home",
        body=f"[Documented page](wiki/{page_type}.md)",
    )
    write_page(
        tmp_path,
        f"wiki/{page_type}.md",
        title=f"Example {page_type.title()}",
        page_type=page_type,
        body="[Home](../Home.md)",
    )

    assert lint_wiki(tmp_path) == []


def test_lint_wiki_reports_broken_links_and_orphans(tmp_path: Path) -> None:
    write_page(tmp_path, "Home.md", title="Home", body="[Missing](wiki/missing.md)")
    write_page(tmp_path, "wiki/orphan.md", title="Orphan")

    issues = lint_wiki(tmp_path)

    assert {(issue.path, issue.code) for issue in issues} == {
        ("Home.md", "broken-link"),
        ("wiki/orphan.md", "orphan"),
    }


def test_lint_wiki_enforces_requirement_sections(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "Home.md",
        title="Home",
        body="[Requirement](wiki/requirements/example.md)",
    )
    write_page(
        tmp_path,
        "wiki/requirements/example.md",
        title="Example Requirement",
        page_type="requirement",
        status="implemented",
        body="## Intent\n\nDo the thing.\n\n[Home](../../Home.md)",
    )

    issues = lint_wiki(tmp_path)

    missing_sections = {issue.message for issue in issues if issue.code == "requirement-section"}
    assert missing_sections == {
        "missing section: Current behavior",
        "missing section: Gap",
        "missing section: Acceptance criteria",
        "missing section: Implementation evidence",
        "missing section: Test evidence",
        "missing section: Related",
    }
