"""Regression test for tenant-scoped legacy graph caches."""
from __future__ import annotations

from app.db.database import get_db
from app.db.models import GraphEdge, GraphNode
from app.retrieval import graph


async def test_graph_cache_never_reuses_another_users_graph(app_no_scheduler):
    first_user = "01920000-0000-0000-0000-000000000001"
    second_user = "01920000-0000-0000-0000-000000000002"
    async with get_db(read_only=False) as db:
        for user_id, episodic_id in (
            (first_user, "first-private-episode"),
            (second_user, "second-private-episode"),
        ):
            db.add_all(
                [
                    GraphNode(
                        user_id=user_id,
                        node_id="person:妈妈",
                        node_type="person",
                        node_name="妈妈",
                        entity_norm_name="妈妈",
                    ),
                    GraphNode(
                        user_id=user_id,
                        node_id=f"ep:{episodic_id}",
                        node_type="ep",
                        node_name=episodic_id,
                        entity_norm_name=episodic_id,
                    ),
                    GraphEdge(
                        user_id=user_id,
                        src="person:妈妈",
                        dst=f"ep:{episodic_id}",
                        weight=1.0,
                    ),
                ]
            )

    async with get_db(read_only=True) as db:
        first = await graph.search(db, first_user, "妈妈最近怎么样", topk=5)
        second = await graph.search(db, second_user, "妈妈最近怎么样", topk=5)

    assert [hit.episodic_id for hit in first] == ["first-private-episode"]
    assert [hit.episodic_id for hit in second] == ["second-private-episode"]
