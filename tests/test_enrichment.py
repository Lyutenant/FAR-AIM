"""Phase 9 enrichment layer (plan §36): provider, curation files, rendering."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from far_aim.generate import BuildError
from far_aim.generate.build import collect_units, plan_vault, related_inputs
from far_aim.generate.enrich import RELATED_HEADING, EnrichmentLayer
from far_aim.links import semantic
from far_aim.links.concepts import Concept, ConceptGraph
from far_aim.links.semantic import RelatedIndex, Review, Unit
from far_aim.manifest import SourceManifest, SourceState
from far_aim.models import cfr as cfr_model
from far_aim.parsers import ecfr as parser
from tests.test_generate import SLICE_PATH, SOURCE

# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def test_tokenize_drops_stopwords_and_folds_plurals():
    tokens = semantic.tokenize("The VFR weather minimums shall apply to Class D airspace areas.")
    assert tokens == ["vfr", "weather", "minimum", "apply", "class", "airspace", "area"]
    assert semantic.tokenize("§ 91.155 (a) 2026") == []


@pytest.mark.parametrize(
    "token,expected",
    [("minimums", "minimum"), ("class", "class"), ("radius", "radius"), ("analysis", "analysis"),
     ("categories", "category"), ("approaches", "approach"), ("boxes", "box"), ("gas", "gas")],
)
def test_fold_plural(token, expected):
    assert semantic.fold_plural(token) == expected


def _filler(count: int, start: int = 0) -> list[Unit]:
    """Units with vocabulary unique to each, so a test corpus is large enough
    for the max-df ratio to leave shared terms in the vocabulary."""
    return [
        Unit(f"far-fill-{i}", "far", f"filler{i}alpha filler{i}beta filler{i}gamma")
        for i in range(start, start + count)
    ]


def _corpus() -> list[Unit]:
    weather = "visibility cloud clearance statute mile below feet ceiling weather minimum"
    return [
        Unit("far-a", "far", f"Basic VFR weather minimums. {weather} airspace class"),
        Unit("far-b", "far", f"Special VFR weather minimums. {weather} clearance ATC"),
        Unit("far-c", "far", "Registration marks. nationality mark letters height paint"),
        Unit("far-d", "far", "Registration marks display. nationality mark letters paint fuselage"),
        Unit("aim-a", "aim", f"Basic VFR Weather Minimums. {weather} chart"),
        Unit("aim-b", "aim", "Airport beacons. rotating beacon color light operating hours"),
        Unit("far-e", "far", "[Reserved]"),
        *_filler(13),  # 20 units: terms in up to 3 units survive the 15 % cut
    ]


def test_compute_related_finds_lexical_neighbours_only():
    related = semantic.compute_related(_corpus())
    assert set(related) == {u.id for u in _corpus()}
    far_a = related["far-a"]
    assert [t for t, _ in far_a][:2] in (["aim-a", "far-b"], ["far-b", "aim-a"])
    assert all(t != "far-a" for t, _ in far_a)
    assert "far-c" not in {t for t, _ in far_a}
    assert related["far-e"] == []
    assert related["aim-b"] == []
    assert all(related[f"far-fill-{i}"] == [] for i in range(13))
    assert all(score >= semantic.MIN_SCORE for entries in related.values() for _, score in entries)
    # registration pair is mutual
    assert related["far-c"][0][0] == "far-d" and related["far-d"][0][0] == "far-c"


def test_compute_related_is_deterministic_and_rounded():
    first = semantic.compute_related(_corpus())
    second = semantic.compute_related(list(reversed(_corpus())))
    assert first == second
    for entries in first.values():
        for _, score in entries:
            assert score == round(score, semantic.SCORE_PLACES)


def test_compute_related_per_corpus_quota():
    shared = "shared distinctive vocabulary tokens words"
    units = [Unit(f"far-{i}", "far", shared) for i in range(9)]
    units.append(Unit("aim-x", "aim", shared))
    units.extend(_filler(60))  # 70 units: a term in 10 of them survives the cut
    related = semantic.compute_related(units)
    targets = related["far-0"]
    far_targets = [t for t, _ in targets if t.startswith("far-")]
    assert len(far_targets) == semantic.MAX_PER_CORPUS
    assert [t for t, _ in targets if t.startswith("aim-")] == ["aim-x"]
    # identical scores tie-break on id, deterministically
    assert far_targets == ["far-1", "far-2", "far-3", "far-4", "far-5"]


def test_compute_related_rejects_duplicate_ids():
    with pytest.raises(BuildError, match="unique"):
        semantic.compute_related([Unit("x", "far", "a b"), Unit("x", "far", "c d")])


# ---------------------------------------------------------------------------
# related.json and the review overlay
# ---------------------------------------------------------------------------


def test_related_bytes_round_trip(tmp_path: Path):
    related = semantic.compute_related(_corpus())
    inputs = {"ecfr": "sha256:e", "aim": "sha256:a"}
    data = semantic.related_bytes(inputs, related)
    text = data.decode("utf-8")
    assert text.startswith(
        '{\n"schema": 1,\n"provider": {"id": "lexical-tfidf", "version": 1},\n'
    )
    assert '"inputs": {"aim": "sha256:a", "ecfr": "sha256:e"}' in text
    assert text.endswith("}\n}\n")
    assert len(text.splitlines()) == 5 + len(related) + 2
    path = tmp_path / "related.json"
    path.write_bytes(data)
    index = RelatedIndex.load(path)
    assert index.inputs == inputs
    assert index.units == related
    assert index.targets("far-a") == [t for t, _ in related["far-a"]]
    assert semantic.related_bytes(inputs, index.units) == data


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d.update(schema=2), "unsupported schema"),
        (lambda d: d.update(extra=1), "unknown key"),
        (lambda d: d["provider"].update(version=99), "different provider"),
        (lambda d: d.update(inputs=[1]), "inputs must map"),
        (lambda d: d["units"].__setitem__("u", [{"target": "u", "score": 0.5}]), "self target"),
        (
            lambda d: d["units"].__setitem__(
                "u", [{"target": "v", "score": 0.5}, {"target": "v", "score": 0.4}]
            ),
            "duplicate",
        ),
        (lambda d: d["units"].__setitem__("u", [{"target": "v"}]), "malformed entry"),
    ],
)
def test_related_index_rejects_malformed_files(tmp_path: Path, mutate, message):
    doc = json.loads(semantic.related_bytes({"ecfr": "h"}, {"u": [("v", 0.5)], "v": []}))
    mutate(doc)
    path = tmp_path / "related.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(BuildError, match=message):
        RelatedIndex.load(path)


def test_related_index_verify_ids():
    index = RelatedIndex(inputs={}, units={"u": [("v", 0.5)]})
    index.verify_ids(["u", "v"])
    with pytest.raises(BuildError, match="target 'v'"):
        index.verify_ids(["u"])
    with pytest.raises(BuildError, match="unit 'u'"):
        index.verify_ids(["v"])


def test_review_overlay(tmp_path: Path):
    path = tmp_path / "related-review.json"
    with pytest.raises(BuildError, match="missing"):
        Review.load(path)
    path.write_text(
        json.dumps(
            {
                "_comment": "x",
                "deny": [
                    {"unit": "far-a", "target": "far-b", "reason": "same subject, not related"},
                    {"unit": "far-a", "target": "far-zzz", "reason": "gone"},
                    {"unit": "far-nope", "target": "far-a", "reason": "typo"},
                ],
            }
        ),
        encoding="utf-8",
    )
    review = Review.load(path)
    related = semantic.compute_related(_corpus())
    filtered = review.apply(related)
    assert "far-b" in {t for t, _ in related["far-a"]}
    assert "far-b" not in {t for t, _ in filtered["far-a"]}
    assert filtered["far-b"] == related["far-b"]
    assert review.stale(related) == [("far-a", "far-zzz"), ("far-nope", "far-a")]
    assert review.unknown_ids(u.id for u in _corpus()) == ["far-nope"]


@pytest.mark.parametrize(
    "raw,message",
    [
        ([], "top level must be an object"),
        ({"deny": [], "allow": []}, "unknown section"),
        ({"deny": {}}, "deny must be a list"),
        ({"deny": [{"unit": "a", "target": "b"}]}, "non-empty string"),
        ({"deny": [{"unit": "a", "target": "b", "reason": " "}]}, "non-empty string"),
        (
            {
                "deny": [
                    {"unit": "a", "target": "b", "reason": "r"},
                    {"unit": "a", "target": "b", "reason": "r2"},
                ]
            },
            "duplicate deny",
        ),
    ],
)
def test_review_overlay_rejects_malformed_files(tmp_path: Path, raw, message):
    path = tmp_path / "related-review.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(BuildError, match=message):
        Review.load(path)


# ---------------------------------------------------------------------------
# Concept graph
# ---------------------------------------------------------------------------


def _graph_raw(**overrides) -> dict:
    concepts = [
        {"id": "roots", "title": "Roots", "area": "A", "far": ["91.155"]},
        {"id": "mid", "title": "Middle", "area": "A", "prerequisites": ["roots"]},
        {"id": "leaf-b", "title": "Leaf B", "area": "B", "prerequisites": ["mid", "roots"]},
        {"id": "leaf-a", "title": "Leaf A", "area": "B", "prerequisites": ["mid"]},
    ]
    raw = {"schema": 1, "concepts": concepts}
    raw.update(overrides)
    return raw


def test_concept_graph_derives_order_and_reverse_edges(tmp_path: Path):
    path = tmp_path / "concepts.json"
    path.write_text(json.dumps(_graph_raw(_comment="c")), encoding="utf-8")
    graph = ConceptGraph.load(path)
    assert [c.id for c in graph.roots()] == ["roots"]
    assert graph.dependents["roots"] == ("mid", "leaf-b")
    assert graph.dependents["leaf-a"] == ()
    assert [a for a, _ in graph.areas()] == ["A", "B"]
    assert [c.id for c in graph.study_order()] == ["roots", "mid", "leaf-a", "leaf-b"]
    assert graph.by_id["roots"].far == ("91.155",)
    assert graph.by_id["roots"].description == ""


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda r: r.update(schema=0), "unsupported schema"),
        (lambda r: r.update(extra=[]), "unknown key"),
        (lambda r: r.update(concepts=[]), "non-empty list"),
        (lambda r: r["concepts"].append({"id": "roots", "title": "X", "area": "A"}), "duplicate"),
        (lambda r: r["concepts"].append({"id": "x", "title": "ROOTS", "area": "A"}), "collide"),
        (lambda r: r["concepts"].append({"id": "Bad_Id", "title": "X", "area": "A"}), "slug"),
        (lambda r: r["concepts"].append({"id": "x", "title": "X.", "area": "A"}), "naming policy"),
        (lambda r: r["concepts"].append({"id": "x", "title": "X", "area": ""}), "non-empty string"),
        (
            lambda r: r["concepts"].append({"id": "x", "title": "X", "area": "A", "foo": 1}),
            "unknown key",
        ),
        (
            lambda r: r["concepts"].append(
                {"id": "x", "title": "X", "area": "A", "prerequisites": ["nope"]}
            ),
            "unknown prerequisite",
        ),
        (
            lambda r: r["concepts"].append(
                {"id": "x", "title": "X", "area": "A", "prerequisites": ["x"]}
            ),
            "own prerequisite",
        ),
        (lambda r: r["concepts"][0].update(prerequisites=["leaf-a"]), "cycle among"),
        (lambda r: r["concepts"][0].update(far=["91.155", "91.155"]), "duplicates"),
        (lambda r: r["concepts"][0].update(far=[" 91.155"]), "trimmed"),
        (lambda r: r["concepts"][0].update(description=" x"), "trimmed string"),
    ],
)
def test_concept_graph_rejects_defects(mutate, message):
    raw = _graph_raw()
    mutate(raw)
    with pytest.raises(BuildError, match=message):
        ConceptGraph.from_raw(raw)


# ---------------------------------------------------------------------------
# Rendering on the part-91 slice
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def slice_docs():
    root = ET.parse(SLICE_PATH).getroot()
    docs = parser.build_part_docs(root, SOURCE)
    title_hash = cfr_model.canonical_hash(
        {number: doc["canonical_hash"] for number, doc in docs.items()}
    )
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(
        accepted_version="2026-08-19", canonical_hash=title_hash
    )
    return docs, title_hash, manifest.sources


def _plan(slice_docs, enrichment: EnrichmentLayer | None):
    docs, title_hash, sources = slice_docs
    return plan_vault(docs, "2026-08-19", title_hash, sources, enrichment=enrichment)


def _note(plan, name: str) -> str:
    match = [data for parts, data in plan.items() if parts[-1] == name]
    assert len(match) == 1, name
    return match[0].decode("utf-8")


def _graph(**extra) -> ConceptGraph:
    concepts = (
        Concept(
            id="vfr-minimums",
            title="VFR Visibility Rules",
            area="Weather",
            description="Curator's words about [visibility].",
            far=("91.155", "Part 91"),
            see_also=("Cross-Country Flight",),
            **extra,
        ),
        Concept(
            id="special-vfr",
            title="Special VFR Clearances",
            area="Weather",
            prerequisites=("vfr-minimums",),
            far=("91.157",),
        ),
    )
    return ConceptGraph.from_concepts(concepts, where="test")


def _related(slice_docs) -> RelatedIndex:
    docs, title_hash, _ = slice_docs
    computed = semantic.compute_related(collect_units(docs, None))
    return RelatedIndex(inputs=related_inputs(title_hash, None), units=computed)


def test_collect_units_covers_every_section(slice_docs):
    docs, _, _ = slice_docs
    units = collect_units(docs, None)
    assert len(units) == 11
    assert units[0].corpus == "far"
    assert all(u.id.startswith("cfr-14-") for u in units)
    by_id = {u.id: u for u in units}
    assert by_id["cfr-14-91.155"].text.startswith("Basic VFR weather minimums.\n")


def test_plan_renders_concepts_and_derived_links(slice_docs):
    related = _related(slice_docs)
    assert related.targets("cfr-14-91.155"), "fixture must yield a suggestion for § 91.155"
    layer = EnrichmentLayer(
        concepts=_graph(),
        related=related,
        curated_notes={"Cross-Country Flight": ("Topics", "Cross-Country Flight.md")},
    )
    plan = _plan(slice_docs, layer)
    # 19 slice notes + 2 concepts + Concept Map
    assert len(plan) == 22
    concept = _note(plan, "VFR Visibility Rules.md")
    assert concept.startswith('---\nid: "concept-vfr-minimums"\ntype: "concept"\narea: "Weather"\n')
    assert "> [!note] Curated concept" in concept
    assert "Curator's words about \\[visibility]." in concept
    assert "## Builds on this\n\n- [[Special VFR Clearances]]" in concept
    assert (
        "## Regulations\n\n- [[91.155|§ 91.155 — Basic VFR weather minimums]]\n- [[Part 91]]"
        in concept
    )
    assert "## See also\n\n- [[Concept Map]]\n- [[Cross-Country Flight]]\n" in concept
    special = _note(plan, "Special VFR Clearances.md")
    assert "## Prerequisites\n\n- [[VFR Visibility Rules]]" in special
    concept_map = _note(plan, "Concept Map.md")
    assert (
        "## Start here\n\nConcepts with no prerequisites:\n\n- [[VFR Visibility Rules]]"
        in concept_map
    )
    assert "1. [[VFR Visibility Rules]]\n2. [[Special VFR Clearances]]" in concept_map
    assert "```mermaid\ngraph TD\n" in concept_map
    assert "  c_vfr_minimums --> c_special_vfr\n```" in concept_map
    home = _note(plan, "Home.md")
    assert "[[Concept Map]]" in home

    # § 91.155's only suggestion is § 91.157, which it already cites
    # explicitly: the derived list must not repeat it (plan §32.11), so the
    # section is absent altogether rather than rendered empty.
    section = _note(plan, "91.155.md")
    assert related.targets("cfr-14-91.155") == ["cfr-14-91.157"]
    assert "- [[91.157|§ 91.157]]" in section
    assert RELATED_HEADING not in section
    # § 91.175 ↔ § 91.227 is a pure similarity suggestion.
    other = _note(plan, "91.175.md")
    assert RELATED_HEADING in other
    derived = other.split(RELATED_HEADING, 1)[1]
    assert derived.startswith("\n\n> [!info] Derived links\n> Suggested by lexical similarity")
    assert "\n- [[91.227|§ 91.227 — " in derived
    assert "[[91.227" not in other.split(RELATED_HEADING, 1)[0]
    assert other.endswith("]]\n")


def test_plan_without_enrichment_is_the_same_vault_minus_enrichment(slice_docs):
    related = _related(slice_docs)
    layer = EnrichmentLayer(
        concepts=_graph(),
        related=related,
        curated_notes={"Cross-Country Flight": ("Topics", "Cross-Country Flight.md")},
    )
    with_layer = _plan(slice_docs, layer)
    without = _plan(slice_docs, None)
    assert set(without) < set(with_layer)
    assert {p for p in with_layer if p not in without} == {
        ("Concepts", "VFR Visibility Rules.md"),
        ("Concepts", "Special VFR Clearances.md"),
        ("Concepts", "Concept Map.md"),
    }
    for parts, data in without.items():
        enriched = with_layer[parts].decode("utf-8")
        plain = data.decode("utf-8")
        if parts == ("Home.md",):
            continue
        if RELATED_HEADING in enriched:
            assert enriched.split("\n\n" + RELATED_HEADING, 1)[0] + "\n" == plain
        else:
            assert enriched == plain


def test_plan_rejects_stale_related_inputs(slice_docs):
    related = _related(slice_docs)
    stale = RelatedIndex(inputs={"ecfr": "sha256:other"}, units=related.units)
    with pytest.raises(BuildError, match="stale"):
        _plan(slice_docs, EnrichmentLayer(related=stale))


def test_plan_rejects_dangling_related_target(slice_docs):
    related = _related(slice_docs)
    units = dict(related.units)
    units["cfr-14-91.155"] = [("cfr-14-999.1", 0.9)]
    with pytest.raises(BuildError, match="not in the canonical layers"):
        _plan(slice_docs, EnrichmentLayer(related=RelatedIndex(related.inputs, units)))


def test_plan_rejects_concept_referencing_missing_note(slice_docs):
    graph = ConceptGraph.from_concepts(
        (Concept(id="x", title="Some Concept", area="A", far=("61.109",)),), where="test"
    )
    with pytest.raises(BuildError, match="FAR note '61.109'"):
        _plan(slice_docs, EnrichmentLayer(concepts=graph))
    graph = ConceptGraph.from_concepts(
        (Concept(id="x", title="Some Concept", area="A", see_also=("Nowhere",)),), where="test"
    )
    with pytest.raises(BuildError, match="see_also names 'Nowhere'"):
        _plan(slice_docs, EnrichmentLayer(concepts=graph))


def test_plan_rejects_concept_title_colliding_with_alias(slice_docs):
    graph = ConceptGraph.from_concepts(
        (Concept(id="x", title="Basic VFR weather minimums", area="A"),), where="test"
    )
    with pytest.raises(BuildError, match="collides with the note heading or alias"):
        _plan(slice_docs, EnrichmentLayer(concepts=graph))
    graph = ConceptGraph.from_concepts((Concept(id="x", title="91.155", area="A"),), where="test")
    with pytest.raises(BuildError, match="filename collision"):
        _plan(slice_docs, EnrichmentLayer(concepts=graph))
