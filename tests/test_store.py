from app.db import Database
from app.memory.store import MemoryStore, fts_query


def make_store(tmp_path):
    store = MemoryStore(Database(str(tmp_path / "t.db")))
    store.ensure_self("Alex")
    return store


def test_entities_and_alias_lookup(tmp_path):
    s = make_store(tmp_path)
    eid = s.create_entity("person", "Max Müller", ["Max"], "CTO bei Acme", {"firma": "Acme"})
    assert s.find_entity_by_name("max müller")["id"] == eid
    assert s.find_entity_by_name("Max")["id"] == eid
    assert s.resolve_entity("E%d" % eid)["name"] == "Max Müller"
    s.update_entity(eid, attributes={"rolle": "CTO", "firma": ""})
    ent = s.get_entity(eid)
    assert ent["attributes"] == {"rolle": "CTO"}


def test_fact_supersede_keeps_history(tmp_path):
    s = make_store(tmp_path)
    me = s.self_id()
    f1 = s.add_fact("Alex wohnt in Köln", [me], "lebenssituation", 5)
    f2 = s.supersede_fact(f1, "Alex wohnt in Hamburg")
    assert s.get_fact(f1)["status"] == "superseded"
    assert s.get_fact(f1)["superseded_by"] == f2
    assert [e["id"] for e in s.get_fact(f2)["entities"]] == [me]
    active = [f["content"] for f in s.list_facts()]
    assert active == ["Alex wohnt in Hamburg"]


def test_fts_search_handles_compounds(tmp_path):
    s = make_store(tmp_path)
    me = s.self_id()
    s.add_fact("Alex sucht eine Mietwohnung in Hamburg", [me])
    s.add_fact("Alex trinkt gern Kaffee", [me])
    hits = s.search_facts_fts("Wie läuft es mit der Wohnung?")
    assert len(hits) == 1
    assert "Mietwohnung" in s.get_fact(hits[0][0])["content"]


def test_fts_query_filters_stopwords():
    assert fts_query("und der die das") is None
    assert '"projekt"' in fts_query("Was ist mit dem Projekt?")


def test_jobs_dedupe_and_retry(tmp_path):
    s = make_store(tmp_path)
    j1 = s.enqueue_job("summarize_conversation", {"conversation_id": "a"}, dedupe_key="summary:a")
    j2 = s.enqueue_job("summarize_conversation", {"conversation_id": "a"}, dedupe_key="summary:a")
    assert j1 == j2
    job = s.claim_next_job()
    assert job["id"] == j1 and job["attempts"] == 1
    s.fail_job(j1, "boom", retry_in_seconds=0)
    assert s.claim_next_job()["attempts"] == 2


def test_merge_entities(tmp_path):
    s = make_store(tmp_path)
    a = s.create_entity("person", "Max Müller")
    b = s.create_entity("person", "Maximilian Müller")
    f = s.add_fact("Maximilian Müller mag Segeln", [b])
    org = s.create_entity("organization", "Acme")
    s.upsert_relation(b, org, "arbeitet_bei")
    s.merge_entities(a, b)
    assert s.get_entity(b) is None
    assert "Maximilian Müller" in s.get_entity(a)["aliases"]
    assert [e["id"] for e in s.get_fact(f)["entities"]] == [a]
    assert s.relations_for_entity(a)[0]["target_id"] == org


def test_list_facts_filters_and_paging(tmp_path):
    s = make_store(tmp_path)
    me = s.self_id()
    for i in range(5):
        s.add_fact(f"Arbeitsfakt {i}", [me], "arbeit", importance=1 + i)
    s.add_fact("Mag Kaffee", [me], "praeferenz", importance=2)
    assert len(s.list_facts(category="arbeit")) == 5
    assert [f["importance"] for f in s.list_facts(category="arbeit", order="importance", limit=2)] == [5, 4]
    assert [f["importance"] for f in s.list_facts(category="arbeit", order="importance", limit=2, offset=2)] == [3, 2]
    assert len(s.list_facts(min_importance=4)) == 2
    cats = {c["category"]: c["n"] for c in s.fact_categories()}
    assert cats == {"arbeit": 5, "praeferenz": 1}
