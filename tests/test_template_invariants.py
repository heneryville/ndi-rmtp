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


# ---- Shared camera card: one copy, not two ---------------------------------
#
# index.html and camera.html both show the same PTZ controls. They were inlined
# in index.html originally; camera.html was built by extracting them into
# _ptz.html + static/ptz.js rather than copying. These invariants exist because
# a copy would be the easy edit for the next person, and the two copies would
# then drift silently — the controls still render, they just stop matching.

CAMERA_TEMPLATE = Path(__file__).parent.parent / "src" / "ndi_rmtp" / "templates" / "camera.html"
PTZ_PARTIAL = Path(__file__).parent.parent / "src" / "ndi_rmtp" / "templates" / "_ptz.html"
PTZ_SCRIPT = Path(__file__).parent.parent / "src" / "ndi_rmtp" / "static" / "ptz.js"


def test_ptz_partial_and_script_exist():
    assert PTZ_PARTIAL.exists(), "the shared camera card partial is missing"
    assert PTZ_SCRIPT.exists(), "the shared camera-control script is missing"


@pytest.mark.parametrize("template", [TEMPLATE, CAMERA_TEMPLATE], ids=["index", "camera"])
def test_page_includes_shared_ptz_partial(template):
    assert '"_ptz.html"' in template.read_text(), (
        f"{template.name} must render the camera card via {{% include \"_ptz.html\" %}} "
        f"rather than its own copy of the markup."
    )


@pytest.mark.parametrize("template", [TEMPLATE, CAMERA_TEMPLATE], ids=["index", "camera"])
def test_page_loads_shared_ptz_script(template):
    assert "/static/ptz.js" in template.read_text(), (
        f"{template.name} includes the camera card but never loads ptz.js, so "
        f"the preview, presets, and d-pad are inert."
    )


@pytest.mark.parametrize("template", [TEMPLATE, CAMERA_TEMPLATE], ids=["index", "camera"])
def test_page_does_not_inline_ptz_markup(template):
    assert 'id="ptz-card"' not in template.read_text(), (
        f"{template.name} inlines the camera card markup. It belongs in "
        f"_ptz.html so both pages stay identical."
    )


@pytest.mark.parametrize(
    "symbol", ["renderPtzInfo", "refreshPreview", "bindHoldButton"]
)
def test_index_does_not_inline_ptz_script(symbol):
    """These live in static/ptz.js now. A second top-level copy in index.html
    wouldn't just duplicate logic — ptz.js and the inline block would both bind
    the delegated preset click handler, firing every recall twice."""
    assert symbol not in _script_source(_template_source()), (
        f"index.html inlines `{symbol}`, which static/ptz.js also defines."
    )


def test_api_mode_elements_present():
    html = _template_source()
    for el_id in ("yt-api-card", "yt-api-help", "yt-fallback-btn"):
        assert f'id="{el_id}"' in html
    assert "skip_youtube" in _script_source(html)
    assert '_ytMode === "api"' in _script_source(html)


def test_state_change_repolls_youtube():
    # The poll interval is picked from the state at schedule time; without a re-poll on
    # each state change the live pill lags up to a minute behind Start.
    script = _script_source(_template_source())
    m = re.search(r'if \(data\.state !== _prevState\) \{(.*?)\n        \}', script, re.S)
    assert m and "pollYouTube" in m.group(1)
