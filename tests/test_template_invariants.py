"""Static invariants over index.html that no route test would catch.

These read the template *source* rather than rendered output, because the
defect they guard against is structural: a label whose value is interpolated
once at render time and then never updated again while the page stays open.
"""

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).parent.parent / "src" / "ndi_rmtp" / "templates" / "index.html"


def _template_source() -> str:
    return TEMPLATE.read_text()


def _stream_chip_section(html: str) -> str:
    m = re.search(
        r'<section class="[^"]*stream-chip[^"]*".*?</section>', html, re.S
    )
    assert m, "could not locate the .stream-chip section in index.html"
    return m.group(0)


def _script_source(html: str) -> str:
    return "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S))


def _config_bound_labels(section: str) -> list[str]:
    """Classes of chip elements whose text is interpolated from `config`.

    A literal placeholder like `<div class="chip-uptime">00:00:00</div>` is
    driven by the status poll and is deliberately excluded — only labels that
    render a `{{ config... }}` value are subject to the staleness bug.
    """
    labels = []
    for cls, body in re.findall(
        r'<(?:span|div)\s+class="([^"]+)"[^>]*>(.*?)</(?:span|div)>', section, re.S
    ):
        if "{{" in body and "config" in body:
            labels.append(cls.strip().split()[0])
    return labels


def test_chip_section_has_config_bound_labels():
    # Guards the guard: if the chip is restructured so this regex finds
    # nothing, the sync test below would vacuously pass.
    labels = _config_bound_labels(_stream_chip_section(_template_source()))
    assert labels, (
        "No config-bound labels found in the .stream-chip section. If the chip "
        "was restructured, update _config_bound_labels() so the sync invariant "
        "below keeps working instead of silently passing."
    )


@pytest.mark.parametrize(
    "label",
    _config_bound_labels(_stream_chip_section(_template_source())),
)
def test_streaming_chip_label_is_kept_in_sync_by_script(label):
    """Every config-bound chip label needs a client-side updater.

    The streaming chip stays on screen across start/stop without a page
    reload, while the controls beside it (ward select, quality radios) mutate
    config via hx-post. Any chip label rendered once server-side therefore goes
    stale the moment the operator changes that control — showing the *previous*
    ward or quality while the appliance streams the new one.

    This has now shipped twice: `.chip-ward` (fixed in ff10296) and
    `.chip-quality` (fixed in de9ece2). This test fails on the third.
    """
    script = _script_source(_template_source())
    assert f".{label}" in script, (
        f'The streaming chip renders "{label}" from config at page load, but no '
        f"script updates it, so it goes stale as soon as the operator changes "
        f"that control mid-session.\n\n"
        f"Fix: in the <script> block, query the control and the label and sync "
        f"them on 'change', following the existing `.chip-ward` / "
        f"`.chip-quality` handlers.\n\n"
        f"If this label genuinely cannot go stale, exclude it explicitly in "
        f"_config_bound_labels() with a comment saying why."
    )
