"""ROADMAP next-item parser against fixture copies shaped like the real project layouts."""

from datetime import datetime, timezone

import pytest

from memorymaster.profile.project_brief import BriefInputs, render_brief
from memorymaster.profile.project_repository import roadmap_next_item

# memorymaster: `## Now` holds `###` blocks; the first unchecked box sits in the second block.
MEMORYMASTER = """\
# MemoryMaster roadmap
# Covers: live Jev decision ledger
# Authority: sole roadmap

## Shipped in v4.6.0
- [ ] a stray unchecked box in a shipped section must never be the answer

## Now

### Engineering-owner plan (operator grant 2026-10-03)
- [x] done thing
Plain prose, no boxes here.

### Shared CLI code navigation (2026-09-28)
- [ ] Review the isolated automatic navigation candidate
  - [ ] nested child
- [ ] Second open item

## Next
- [ ] Research-derived memory sustainability program

## Later
- [ ] Not this
"""

# A tracker that has all three sections: priority is Delivery-first > Lane backlog > Now, not file order.
ALL_THREE = """\
## Now
- [ ] now item

## Lane backlog tracker (auto)
- [x] closed
- [ ] lane item

## Delivery-first program
Intro text.
- [ ] delivery item
"""

LANE_AND_NOW = """\
## Now
- [ ] now item

## Lane backlog tracker
- [ ] lane item
"""


def roadmap(tmp_path, text):
    (tmp_path / "ROADMAP.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_memorymaster_layout_finds_first_open_item_in_a_subsection_with_its_line(tmp_path):
    item, mtime = roadmap_next_item(roadmap(tmp_path, MEMORYMASTER))
    assert item == ("Review the isolated automatic navigation candidate", 15)
    assert mtime is not None


def test_section_priority_beats_file_order(tmp_path):
    assert roadmap_next_item(roadmap(tmp_path, ALL_THREE))[0] == ("delivery item", 10)
    assert roadmap_next_item(roadmap(tmp_path, LANE_AND_NOW))[0] == ("lane item", 5)


def test_priority_falls_through_to_the_next_section_when_the_first_has_no_open_item(tmp_path):
    text = "## Delivery-first program\n- [x] all done\n\n## Now\n- [ ] still open\n"
    assert roadmap_next_item(roadmap(tmp_path, text))[0] == ("still open", 5)


@pytest.mark.parametrize("text", [
    "## Now\n- [x] done\n- [x] done too\n",                 # nothing unchecked
    "## Next\n- [ ] only in Next\n",                        # not a recognised section
    "## Nowhere\n- [ ] heading must not prefix-match a longer word\n",
    "## Now\n```\n- [ ] inside a code fence\n```\n",        # fenced example, not a task
    "## Now\n## Later\n- [ ] after the section ended\n",
    "",
])
def test_no_open_item_in_recognised_sections_is_none_but_roadmap_exists(tmp_path, text):
    item, mtime = roadmap_next_item(roadmap(tmp_path, text))
    assert item is None and mtime is not None


def test_equal_level_heading_ends_the_section_but_deeper_headings_do_not(tmp_path):
    text = "## Now\n### Sub\n### Sub two\n- [ ] deep\n## Other\n"
    assert roadmap_next_item(roadmap(tmp_path, text))[0] == ("deep", 4)


# wezbridge keeps its backlog in TRACKS.md (no ROADMAP.md) and its AGENTS.md has no checkboxes;
# infra and futuraMAX have neither. All three are "no roadmap" -> none.
@pytest.mark.parametrize("project,files", [
    ("wezbridge", {"AGENTS.md": "# wezbridge\nNo checkboxes.\n", "TRACKS.md": "## Defectos medidos\n- [ ] T5 reindex\n"}),
    ("infra", {"AGENTS.md": "# infra\n"}),
    ("futuraMAX", {"AGENTS.md": "# futuraMAX\n", "FUTURAMAX_PROJECT_GUIDE.md": "- [ ] not a roadmap\n"}),
])
def test_projects_without_a_roadmap_file_render_none_no_roadmap(tmp_path, project, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    item, mtime = roadmap_next_item(tmp_path)
    assert (item, mtime) == (None, None)
    brief = render_brief(BriefInputs(project=project, as_of=datetime(2026, 11, 9, tzinfo=timezone.utc),
                                     next_item=item, roadmap_missing=mtime is None))
    assert "NEXT\n- none: no roadmap [ROADMAP.md]" in brief


def test_roadmap_item_renders_with_its_line_tag(tmp_path):
    item, _ = roadmap_next_item(roadmap(tmp_path, MEMORYMASTER))
    brief = render_brief(BriefInputs(project="memorymaster", as_of=datetime(2026, 11, 9, tzinfo=timezone.utc),
                                     next_item=item, roadmap_missing=False))
    assert "- Review the isolated automatic navigation candidate [ROADMAP.md#L15]" in brief


def test_oversized_roadmap_is_treated_as_missing(tmp_path, monkeypatch):
    roadmap(tmp_path, "## Now\n- [ ] x\n")
    monkeypatch.setattr("memorymaster.profile.project_repository.MAX_ROADMAP_BYTES", 4)
    assert roadmap_next_item(tmp_path) == (None, None)
