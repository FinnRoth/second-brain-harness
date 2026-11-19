import json

from conftest import FakeLLM, tool_call


async def test_system_prompt_has_summary_saved_memories_and_recent_chats(services):
    store = services.store
    me = store.self_id()
    store.set_profile("### Person & Lebenssituation\n- Alex wohnt in Hamburg")
    saved = store.add_fact("Alex möchte Antworten ohne Emojis", [me], "antwortstil", 5, pinned=True)
    store.add_fact("Alex mag Tee", [me])  # nicht gemerkt → nicht im Systemprompt
    store.upsert_conversation("old", "brain")
    store.upsert_turn("old", 1, "Hilf mir bei der Steuererklärung", "GEHEIME_ASSISTENTENANTWORT", "m")
    store.set_conversation_summary("old", "Steuererklärung 2026", "…", None)

    system, saved_ids = services.prompt_builder.system_prompt("current")
    assert "Alex wohnt in Hamburg" in system
    assert f"#F{saved}" in system and "ohne Emojis" in system
    assert "Alex mag Tee" not in system
    assert "Steuererklärung 2026" in system and "Hilf mir bei der Steuererklärung" in system
    assert "GEHEIME_ASSISTENTENANTWORT" not in system  # wie ChatGPT: nur Nutzernachrichten
    assert saved_ids == {saved}

    # Der aktuelle Chat taucht nicht in „Letzte Gespräche“ auf
    system_old, _ = services.prompt_builder.system_prompt("old")
    assert "Steuererklärung 2026" not in system_old


async def test_saved_memories_budget_moves_rest_to_background(services, settings):
    settings.saved_memories_token_budget = 40
    me = services.store.self_id()
    for i in range(10):
        services.store.add_fact(f"Gemerkter Fakt Nummer {i} mit etwas Text", [me], pinned=True)
    block, shown = services.prompt_builder.saved_memories_block()
    assert 0 < len(shown) < 10
    assert "weitere im Hintergrund" in block


async def test_context_block_excludes_saved_memories(services):
    me = services.store.self_id()
    services.store.add_fact("Alex arbeitet bei Contoso als Data Engineer", [me], pinned=True)
    services.store.add_fact("Contoso hat 500 Mitarbeitende", [me])
    _, saved_ids = services.prompt_builder.system_prompt(None)
    ctx = await services.prompt_builder.turn_context([{"role": "user", "content": "Was gibt es Neues bei Contoso?"}], saved_ids)
    assert "500 Mitarbeitende" in ctx.text
    assert "Data Engineer" not in ctx.text
    assert ctx.text.startswith("Aktuelle Zeit:")


async def test_dreaming_processes_whole_conversation_in_one_call(services, fake_llm: FakeLLM):
    store = services.store
    me = store.self_id()
    calls = []

    def background(payload):
        system = payload["messages"][0]["content"]
        if "zusammen, damit" in system:
            return {"content": json.dumps({"title": "Umzug & Job", "summary": "Umzug und neuer Job."})}
        if "Du pflegst die Memory-Zusammenfassung" in system:
            return {"content": "### Person\n- Alex"}
        calls.append(payload["messages"][1]["content"])
        return {"content": json.dumps({"entities": [], "relations": [], "facts": [
            {"op": "add", "content": "Alex zieht nach Hamburg", "entities": [f"E{me}"], "importance": 5},
            {"op": "add", "content": "Alex mag kurze Antworten (sicher)", "entities": [f"E{me}"],
             "category": "antwortstil", "importance": 4},
        ]})}

    fake_llm.background = background
    store.upsert_conversation("c1", "brain")
    store.upsert_turn("c1", 1, "Ich ziehe nach Hamburg", "Spannend!", "m")
    store.upsert_turn("c1", 2, "Bitte antworte kürzer", "Ok.", "m")
    store.enqueue_job("dream_conversation", {"conversation_id": "c1"}, dedupe_key="dream:c1")
    await services.worker.drain()

    assert len(calls) == 1  # beide Turns in einem Durchgang
    assert "Ich ziehe nach Hamburg" in calls[0] and "Bitte antworte kürzer" in calls[0]
    assert store.unextracted_turns("c1") == []
    conv = store.get_conversation("c1")
    assert conv["title"] == "Umzug & Job" and conv["dreamed_at"]
    assert store.get_profile()["content"]  # Zusammenfassung wurde erstellt


async def test_new_chat_expedites_pending_dreams(services, client, fake_llm: FakeLLM):
    store = services.store
    store.enqueue_job("dream_conversation", {"conversation_id": "alt"}, delay_seconds=3600, dedupe_key="dream:alt")
    assert store.claim_next_job() is None  # noch nicht fällig
    fake_llm.handler = lambda p: {"content": "Hi"}
    await client.post("/v1/chat/completions", headers={"X-OpenWebUI-Chat-Id": "neu"},
                      json={"model": "brain/gpt-5", "messages": [{"role": "user", "content": "Hallo"}]})
    job = store.claim_next_job()
    assert job and job["payload"] == {"conversation_id": "alt"}


async def test_remember_tool_creates_saved_memory(services, client, fake_llm: FakeLLM):
    calls = {"n": 0}

    def handler(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": None, "tool_calls": [tool_call("remember", {"content": "Alex trinkt keinen Kaffee"})]}
        return {"content": "Gemerkt."}

    fake_llm.handler = handler
    await client.post("/v1/chat/completions", json={
        "model": "brain/gpt-5", "messages": [{"role": "user", "content": "Merk dir: kein Kaffee"}]})
    assert [f["content"] for f in services.store.pinned_facts()] == ["Alex trinkt keinen Kaffee"]
    system, _ = services.prompt_builder.system_prompt(None)
    assert "keinen Kaffee" in system


async def test_entity_consolidation(services, fake_llm: FakeLLM, settings):
    store = services.store
    pid = store.create_entity("project", "Atlas", summary="alt")
    f1 = store.add_fact("Atlas startet im Januar", [pid])
    f2 = store.add_fact("Projekt Atlas beginnt im Januar 2027", [pid])
    f3 = store.add_fact("Atlas hat ein Budget von 50k", [pid])
    f4 = store.add_fact("Atlas wird von Finn geleitet", [pid], pinned=True)

    fake_llm.background = lambda p: {"content": json.dumps({
        "summary": "Datenplattform-Projekt, Start Januar 2027.",
        "attributes": {"status": "geplant"},
        "merge": [{"ids": [f"F{f1}", f"F{f2}"], "content": "Projekt Atlas startet im Januar 2027", "importance": 4}],
        "delete": [f"F{f3}", f"F{f4}"],  # gemerkte Fakten dürfen nicht gelöscht werden
    })}
    stats = await services.consolidator.consolidate(pid)
    assert stats == {"merged": 2, "deleted": 1}
    active = {f["content"] for f in store.list_facts(entity_id=pid)}
    assert active == {"Projekt Atlas startet im Januar 2027", "Atlas wird von Finn geleitet"}
    assert store.get_fact(f1)["status"] == "superseded"
    ent = store.get_entity(pid)
    assert ent["summary"].startswith("Datenplattform") and ent["attributes"]["status"] == "geplant"


async def test_needs_consolidation_threshold(services, settings):
    settings.consolidation_min_facts = 3
    settings.consolidation_growth = 2
    store = services.store
    pid = store.create_entity("project", "Blog")
    for i in range(2):
        store.add_fact(f"Blog Fakt {i}", [pid])
    assert not services.consolidator.needs_consolidation(pid)
    store.add_fact("Blog Fakt 2", [pid])
    assert services.consolidator.needs_consolidation(pid)
    store.set_meta(f"consolidated_count:{pid}", "3")
    assert not services.consolidator.needs_consolidation(pid)


async def test_prompt_preview_endpoint(services, client):
    services.store.add_fact("Alex spielt Cello", [services.store.self_id()], pinned=True)
    r = await client.get("/memory/prompt-preview", params={"message": "Wie war die Probe?"})
    data = r.json()
    assert "Alex spielt Cello" in data["system"] and data["context"].startswith("Aktuelle Zeit")
