import json

from conftest import FakeLLM


async def test_extraction_applies_operations(services, fake_llm: FakeLLM):
    store = services.store
    me = store.self_id()
    old = store.add_fact("Alex wohnt in Köln", [me], "lebenssituation", 5)

    def background(payload):
        user_input = payload["messages"][1]["content"]
        assert f"F{old}" in user_input  # bestehender Fakt wird zum Abgleich mitgegeben
        return {
            "content": json.dumps(
                {
                    "entities": [
                        {"ref": "N1", "type": "person", "name": "Max Müller", "aliases": ["Max"],
                         "summary": "CTO bei Acme, auf der Konferenz kennengelernt", "attributes": {"firma": "Acme"}},
                        {"ref": "N2", "type": "organization", "name": "Acme", "summary": "Software-Firma"},
                    ],
                    "facts": [
                        {"op": "supersede", "target": f"F{old}", "content": "Alex wohnt seit November 2026 in Hamburg",
                         "entities": [f"E{me}"], "category": "lebenssituation", "importance": 5},
                        {"op": "add", "content": "Alex hat Max Müller auf der Konferenz kennengelernt",
                         "entities": [f"E{me}", "N1"], "category": "person", "importance": 3},
                    ],
                    "relations": [{"source": "N1", "target": "N2", "type": "arbeitet bei"}],
                }
            )
        }

    fake_llm.background = background
    store.upsert_conversation("c1", "brain")
    turn = store.upsert_turn("c1", 1, "Bin nach Hamburg gezogen und hab Max von Acme getroffen", "Cool!", "gpt-5")
    result = await services.extractor.process_turn(turn)

    assert len(result.created_entities) == 2
    assert store.get_fact(old)["status"] == "superseded"
    max_ent = store.find_entity_by_name("Max")
    assert max_ent["attributes"]["firma"] == "Acme"
    rels = store.relations_for_entity(max_ent["id"])
    assert rels[0]["type"] == "arbeitet_bei" and rels[0]["target_name"] == "Acme"
    assert store.get_turn(turn)["extracted_at"]


async def test_extraction_reuses_existing_entity_by_name(services, fake_llm):
    store = services.store
    existing = store.create_entity("person", "Max Müller", ["Max"])
    fake_llm.background = lambda p: {
        "content": json.dumps(
            {"entities": [{"ref": "N1", "type": "person", "name": "Max", "summary": "Neuer Stand"}],
             "facts": [{"op": "add", "content": "Max Müller plant ein Startup", "entities": ["N1"]}],
             "relations": []}
        )
    }
    store.upsert_conversation("c1", "brain")
    turn = store.upsert_turn("c1", 1, "Max plant ein Startup", "Spannend", "gpt-5")
    result = await services.extractor.process_turn(turn)
    assert result.created_entities == []
    assert store.get_entity(existing)["summary"] == "Neuer Stand"
    assert store.list_facts(entity_id=existing)[0]["content"] == "Max Müller plant ein Startup"


async def test_retrieval_detects_entities_and_facts(services):
    store = services.store
    me = store.self_id()
    proj = store.create_entity("project", "Projekt Phoenix", ["Phoenix"], "Migration der Kundenplattform",
                               {"bereich": "arbeit", "status": "aktiv"})
    store.add_fact("Phoenix soll bis März 2027 live gehen", [proj, me], "projekt", 4)
    store.add_fact("Alex mag Bergwandern", [me], "praeferenz", 2)

    ctx = await services.retriever.build_context([{"role": "user", "content": "Wie stehen wir bei Phoenix?"}])
    assert "Projekt Phoenix" in ctx.text
    assert "März 2027" in ctx.text
    assert "Bergwandern" not in ctx.text
    assert proj in ctx.entity_ids


async def test_worker_runs_profile_and_export(services, fake_llm, settings):
    store = services.store
    me = store.self_id()

    def background(payload):
        system = payload["messages"][0]["content"]
        if "Du pflegst die Memory-Zusammenfassung" in system:
            return {"content": "### Person & Lebenssituation\n- Alex wohnt in Hamburg"}
        if "zusammen, damit" in system:
            return {"content": json.dumps({"title": "Umzug", "summary": "Es ging um den Umzug."})}
        return {"content": json.dumps({"entities": [], "facts": [
            {"op": "add", "content": "Alex wohnt in Hamburg", "entities": [f"E{me}"], "importance": 5}
        ], "relations": []})}

    fake_llm.background = background
    store.upsert_conversation("c1", "brain")
    store.upsert_turn("c1", 1, "Ich wohne jetzt in Hamburg", "Schön!", "gpt-5")
    store.enqueue_job("dream_conversation", {"conversation_id": "c1"})
    await services.worker.drain()

    assert "Hamburg" in store.get_profile()["content"]
    assert store.get_conversation("c1")["title"] == "Umzug"
    from pathlib import Path

    vault = Path(settings.export_dir)
    assert (vault / "Profil.md").exists()
    assert "Alex wohnt in Hamburg" in (vault / "Ich" / "Alex.md").read_text()
    assert list((vault / "Gespräche").glob("*Umzug.md"))


async def test_semantic_search_with_embeddings(settings, fake_llm):
    from app.services import Services

    settings.embedding_model = "fake-embed"
    svc = Services(settings, llm=fake_llm)
    await svc.startup()
    try:
        me = svc.store.self_id()
        svc.store.add_fact("Alex trainiert für einen Halbmarathon im Frühjahr", [me])
        svc.store.add_fact("Alex hat eine Katze namens Luna", [me])
        assert await svc.indexer.index_pending() >= 3  # 2 Fakten + Nutzer-Entität
        hits = await svc.retriever.search_facts("Training?", limit=5)  # kein Volltext-Treffer, nur Vektor
        assert "Halbmarathon" in hits[0][0]["content"]
    finally:
        await svc.shutdown()


async def test_extraction_skips_duplicate_facts(services, fake_llm):
    store = services.store
    me = store.self_id()
    store.add_fact("Alex leitet das Projekt Atlas.", [me])
    fake_llm.background = lambda p: {"content": json.dumps({"entities": [], "relations": [], "facts": [
        {"op": "add", "content": "Alex leitet das Projekt Atlas", "entities": [f"E{me}"]}]})}
    store.upsert_conversation("c1", "brain")
    turn = store.upsert_turn("c1", 1, "Atlas läuft", "Gut", "gpt-5")
    result = await services.extractor.process_turn(turn)
    assert result.added_facts == []
    assert len(store.list_facts()) == 1
