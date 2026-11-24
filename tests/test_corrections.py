"""Manual corrections applied to a parsed document."""

from __future__ import annotations

import pytest

from paper2ink.corrections import (
    Corrections,
    Region,
    apply_corrections,
    drop_id,
)
from paper2ink.model import Asset, BBox, Block, Document


def block(id_: str, text: str, page: int = 0, y0: float = 0.0, **kwargs) -> Block:
    return Block(
        id=id_,
        text=text,
        page=page,
        bbox=BBox(page, 50, y0, 500, y0 + 20),
        **kwargs,
    )


@pytest.fixture
def doc() -> Document:
    document = Document(page_count=2)
    document.blocks = [
        block("b1", "First paragraph.", page=0, y0=100),
        block("b2", "Second paragraph.", page=0, y0=200),
        block("b3", "On the next page.", page=1, y0=100),
    ]
    document.drop("inside graphic", "Box 1. A highlighted aside.", 0,
                  BBox(0, 60, 300, 400, 360))
    document.drop("running head", "www.example.org", 0, BBox(0, 50, 20, 500, 30))
    return document


# --------------------------------------------------------------------------
# nothing to do
# --------------------------------------------------------------------------


def test_empty_corrections_return_the_same_document(doc):
    assert apply_corrections(doc, Corrections()) is doc


def test_the_original_is_never_mutated(doc):
    before = [b.id for b in doc.blocks]
    apply_corrections(doc, Corrections(removed=["b1"]))
    assert [b.id for b in doc.blocks] == before


def test_applying_twice_gives_the_same_result(doc):
    """Rebuilding twice must not compound the corrections."""
    corrections = Corrections(
        removed=["b2"],
        restored=[drop_id(0)],
        regions=[Region(page=1, bbox=[60, 60, 400, 300], label="Figure 9")],
    )
    once = apply_corrections(doc, corrections)
    twice = apply_corrections(doc, corrections)
    assert [b.id for b in once.blocks] == [b.id for b in twice.blocks]
    assert [a.key for a in once.assets] == [a.key for a in twice.assets]
    assert len(once.dropped) == len(twice.dropped)


# --------------------------------------------------------------------------
# taking blocks out
# --------------------------------------------------------------------------


def test_a_removed_block_leaves_the_flow(doc):
    result = apply_corrections(doc, Corrections(removed=["b1"]))
    assert [b.id for b in result.blocks] == ["b2", "b3"]


def test_a_removed_block_is_recorded_with_a_reason(doc):
    result = apply_corrections(doc, Corrections(removed=["b1"]))
    manual = [d for d in result.dropped if d.text == "First paragraph."]
    assert len(manual) == 1
    assert "hand" in manual[0].reason


def test_removing_an_unknown_id_is_harmless(doc):
    result = apply_corrections(doc, Corrections(removed=["nope"]))
    assert len(result.blocks) == 3


# --------------------------------------------------------------------------
# bringing dropped text back
# --------------------------------------------------------------------------


def test_a_restored_block_returns_to_the_flow(doc):
    """The text-box case: dropped as a graphic, but it is prose."""
    result = apply_corrections(doc, Corrections(restored=[drop_id(0)]))
    texts = [b.text for b in result.blocks]
    assert "Box 1. A highlighted aside." in texts


def test_a_restored_block_lands_at_its_place_on_the_page(doc):
    result = apply_corrections(doc, Corrections(restored=[drop_id(0)]))
    ids = [b.text[:5] for b in result.blocks if b.page == 0]
    # It sat at y=300, below both paragraphs on that page.
    assert ids == ["First", "Secon", "Box 1"]


def test_a_restored_block_is_no_longer_listed_as_dropped(doc):
    result = apply_corrections(doc, Corrections(restored=[drop_id(0)]))
    assert all("Box 1" not in d.text for d in result.dropped)
    assert len(result.dropped) == 1


def test_restoring_an_unknown_drop_is_harmless(doc):
    result = apply_corrections(doc, Corrections(restored=["drop-9999"]))
    assert len(result.blocks) == 3
    assert len(result.dropped) == 2


# --------------------------------------------------------------------------
# drawn regions
# --------------------------------------------------------------------------


def test_a_drawn_region_becomes_an_asset(doc):
    result = apply_corrections(
        doc,
        Corrections(regions=[Region(page=1, bbox=[60, 80, 400, 300],
                                    caption="What it shows.")]),
    )
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.kind == "figure"
    assert asset.caption == "What it shows."
    assert asset.bboxes[0].page == 1
    assert asset.bboxes[0].as_tuple() == (60, 80, 400, 300)


def test_a_drawn_region_takes_the_label_it_was_given(doc):
    result = apply_corrections(
        doc, Corrections(regions=[Region(page=0, bbox=[0, 0, 10, 10],
                                         label="Figure 7")])
    )
    assert result.assets[0].key == "fig-7"
    assert result.assets[0].label == "Figure 7"


def test_an_unlabelled_region_gets_its_own_number_space(doc):
    """A hand-drawn figure must not collide with a parsed one."""
    doc.assets.append(Asset(key="fig-1", kind="figure", label="Figure 1", number="1"))
    result = apply_corrections(
        doc, Corrections(regions=[Region(page=0, bbox=[0, 0, 10, 10])])
    )
    keys = [a.key for a in result.assets]
    assert keys == ["fig-1", "fig-m1"]


def test_a_region_whose_label_collides_is_given_a_free_key(doc):
    doc.assets.append(Asset(key="fig-3", kind="figure", label="Figure 3", number="3"))
    result = apply_corrections(
        doc, Corrections(regions=[Region(page=0, bbox=[0, 0, 10, 10],
                                         label="Figure 3")])
    )
    assert [a.key for a in result.assets] == ["fig-3", "fig-3-2"]


def test_a_region_can_be_a_table(doc):
    result = apply_corrections(
        doc, Corrections(regions=[Region(page=0, bbox=[0, 0, 10, 10], kind="table")])
    )
    assert result.assets[0].kind == "table"


def test_a_flipped_rectangle_is_normalised(doc):
    """Dragging up and to the left must still give a sane box."""
    result = apply_corrections(
        doc, Corrections(regions=[Region(page=0, bbox=[400, 300, 60, 80])])
    )
    assert result.assets[0].bboxes[0].as_tuple() == (60, 80, 400, 300)


# --------------------------------------------------------------------------
# reading order
# --------------------------------------------------------------------------


def test_reordering_a_page(doc):
    result = apply_corrections(doc, Corrections(order={0: ["b2", "b1"]}))
    assert [b.id for b in result.blocks] == ["b2", "b1", "b3"]


def test_other_pages_are_untouched(doc):
    result = apply_corrections(doc, Corrections(order={0: ["b2", "b1"]}))
    assert result.blocks[2].page == 1


def test_blocks_left_out_of_the_order_keep_their_place(doc):
    doc.blocks.insert(2, block("b1b", "Third on page one.", page=0, y0=250))
    result = apply_corrections(doc, Corrections(order={0: ["b2"]}))
    assert [b.id for b in result.blocks] == ["b2", "b1", "b1b", "b3"]


def test_reordering_an_empty_page_is_harmless(doc):
    result = apply_corrections(doc, Corrections(order={7: ["b1"]}))
    assert [b.id for b in result.blocks] == ["b1", "b2", "b3"]


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------


def test_round_trip_through_json_shaped_dicts():
    corrections = Corrections(
        removed=["b1"],
        restored=["drop-0003"],
        regions=[Region(page=2, bbox=[1, 2, 3, 4], label="Figure 5",
                        caption="c", kind="table")],
        order={3: ["b9", "b8"]},
    )
    restored = Corrections.from_dict(corrections.to_dict())
    assert restored.removed == ["b1"]
    assert restored.restored == ["drop-0003"]
    assert restored.order == {3: ["b9", "b8"]}
    assert restored.regions[0].label == "Figure 5"
    assert restored.regions[0].kind == "table"


def test_from_dict_tolerates_rubbish():
    corrections = Corrections.from_dict(
        {
            "removed": ["b1"],
            "regions": [{"page": 1}, {"page": 1, "bbox": [1, 2]}],
            "order": {"not-a-page": ["b1"]},
        }
    )
    assert corrections.removed == ["b1"]
    assert corrections.regions == []
    assert corrections.order == {}


def test_from_dict_of_nothing():
    assert Corrections.from_dict(None).is_empty


# --------------------------------------------------------------------------
# text picked up from a rectangle
# --------------------------------------------------------------------------


def test_picked_up_text_enters_the_flow(doc):
    from paper2ink.corrections import TextBlock

    result = apply_corrections(
        doc,
        Corrections(
            text_blocks=[
                TextBlock(page=0, bbox=[60, 150, 400, 190],
                          text="A paragraph the parser skipped.")
            ]
        ),
    )
    texts = [b.text for b in result.blocks]
    assert "A paragraph the parser skipped." in texts


def test_picked_up_text_lands_at_its_place_on_the_page(doc):
    from paper2ink.corrections import TextBlock

    result = apply_corrections(
        doc,
        Corrections(
            text_blocks=[
                TextBlock(page=0, bbox=[60, 150, 400, 190], text="In between.")
            ]
        ),
    )
    on_page = [b.text[:8] for b in result.blocks if b.page == 0]
    assert on_page == ["First pa", "In betwe", "Second p"]


def test_a_picked_up_heading_becomes_one(doc):
    from paper2ink.corrections import TextBlock

    result = apply_corrections(
        doc,
        Corrections(
            text_blocks=[
                TextBlock(page=0, bbox=[0, 0, 10, 10], text="3. Results",
                          type="heading")
            ]
        ),
    )
    added = next(b for b in result.blocks if b.text == "3. Results")
    assert added.type == "heading"
    assert added.level == 1


def test_an_unknown_block_type_falls_back_to_paragraph(doc):
    from paper2ink.corrections import TextBlock

    result = apply_corrections(
        doc,
        Corrections(
            text_blocks=[
                TextBlock(page=0, bbox=[0, 0, 10, 10], text="x", type="nonsense")
            ]
        ),
    )
    assert next(b for b in result.blocks if b.text == "x").type == "paragraph"


def test_empty_picked_up_text_is_ignored(doc):
    from paper2ink.corrections import TextBlock

    before = len(doc.blocks)
    result = apply_corrections(
        doc, Corrections(text_blocks=[TextBlock(page=0, bbox=[0, 0, 10, 10],
                                                text="   ")])
    )
    assert len(result.blocks) == before


def test_text_blocks_survive_the_round_trip():
    from paper2ink.corrections import TextBlock

    corrections = Corrections(
        text_blocks=[TextBlock(page=2, bbox=[1, 2, 3, 4], text="hi",
                               type="caption")]
    )
    restored = Corrections.from_dict(corrections.to_dict())
    assert restored.text_blocks[0].text == "hi"
    assert restored.text_blocks[0].type == "caption"
    assert not restored.is_empty


def test_text_blocks_without_text_are_dropped_on_the_way_in():
    corrections = Corrections.from_dict(
        {"text_blocks": [{"page": 1, "bbox": [1, 2, 3, 4]}]}
    )
    assert corrections.text_blocks == []


# --------------------------------------------------------------------------
# correcting the text itself
# --------------------------------------------------------------------------


def test_editing_a_blocks_text(doc):
    """Parsing merges a tinted box with the caption next to it often
    enough that fixing the text by hand is the only sensible repair."""
    result = apply_corrections(doc, Corrections(edited={"b1": "Corrected text."}))
    assert next(b for b in result.blocks if b.id == "b1").text == "Corrected text."


def test_editing_leaves_the_other_blocks_alone(doc):
    result = apply_corrections(doc, Corrections(edited={"b1": "New."}))
    assert next(b for b in result.blocks if b.id == "b2").text == "Second paragraph."


def test_editing_keeps_the_type_and_the_place(doc):
    doc.blocks[1].type = "heading"
    doc.blocks[1].level = 2
    result = apply_corrections(doc, Corrections(edited={"b2": "2. Results"}))
    edited = next(b for b in result.blocks if b.id == "b2")
    assert edited.type == "heading" and edited.level == 2
    assert [b.id for b in result.blocks] == ["b1", "b2", "b3"]


def test_an_empty_edit_is_ignored(doc):
    """Leaving a block out has its own button; a cleared box must not
    silently delete a paragraph."""
    result = apply_corrections(doc, Corrections(edited={"b1": "   "}))
    assert next(b for b in result.blocks if b.id == "b1").text == "First paragraph."


def test_editing_an_unknown_block_is_harmless(doc):
    result = apply_corrections(doc, Corrections(edited={"nope": "x"}))
    assert len(result.blocks) == 3


def test_a_restored_block_can_be_edited_in_the_same_pass(doc):
    """Edits run after restoring, so the ids they name already exist."""
    result = apply_corrections(
        doc,
        Corrections(
            restored=[drop_id(0)],
            edited={"restored-0000": "Box 1. Cleaned up by hand."},
        ),
    )
    texts = [b.text for b in result.blocks]
    assert "Box 1. Cleaned up by hand." in texts


def test_a_picked_up_block_can_be_edited_in_the_same_pass(doc):
    from paper2ink.corrections import TextBlock

    result = apply_corrections(
        doc,
        Corrections(
            text_blocks=[
                TextBlock(page=0, bbox=[0, 0, 10, 10], text="raw scan")
            ],
            edited={"added-0000": "tidied up"},
        ),
    )
    assert any(b.text == "tidied up" for b in result.blocks)


def test_editing_a_removed_block_does_nothing(doc):
    result = apply_corrections(
        doc, Corrections(removed=["b1"], edited={"b1": "too late"})
    )
    assert all(b.text != "too late" for b in result.blocks)


def test_edits_survive_the_round_trip():
    corrections = Corrections(edited={"b4": "fixed"})
    restored = Corrections.from_dict(corrections.to_dict())
    assert restored.edited == {"b4": "fixed"}
    assert not restored.is_empty


def test_empty_edits_are_dropped_on_the_way_in():
    assert Corrections.from_dict({"edited": {"b1": "  "}}).edited == {}


def test_applying_edits_twice_gives_the_same_result(doc):
    corrections = Corrections(edited={"b1": "Once."})
    once = apply_corrections(doc, corrections)
    twice = apply_corrections(doc, corrections)
    assert [b.text for b in once.blocks] == [b.text for b in twice.blocks]
