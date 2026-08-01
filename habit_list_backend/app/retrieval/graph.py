"""知识图谱 2-hop 扩散检索（NetworkX DiGraph，从 SQLite graph_nodes / graph_edges 启机加载）。

MVP：Episodic 的 entities_json 里的「人/地点/作品/活动」抽出后在 System2 睡眠巩固写入 graph_nodes/edges；
System1 调用：query 实体抽出来 → 2-hop → 收集 hit 的 episodic_id。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

import networkx as nx  # type: ignore
from sqlalchemy import text

log = logging.getLogger("habit_list.retrieval.graph")

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class GraphHit:
    episodic_id: str
    score: float  # hop=1 → 1.0，hop=2 → 0.6
    path_entities: list[str]


# =========================================================
# 简易实体抽取（没有 NER 模型前，先用规则 + entities_json 已有记录反查）
# =========================================================
# 把 "人物:妈"/"地点:公司"/"作品:人间失格"/"活动:冥想" 反查字典的方式
# 这里 query 里只要命中某个 node 的 norm_name，就算命中节点
_ENTITY_HINT_REGEX = re.compile(r"(妈|爸|姐|哥|弟|妹|老公|老婆|老师|同事|老板|朋友|室友|领导|\
家里|公司|学校|医院|家|咖啡馆|健身房|图书馆|北京|上海|广州|深圳|杭州|成都|苏州|南京|\
冥想|跑步|瑜伽|健身|读书|写日记|看电影|追剧|画画|弹琴|做饭|打扫|睡眠|开会|出差|面试|\
红楼梦|西游记|三体|百年孤独|活着|小王子|人类简史|思考快与慢|被讨厌的勇气|人间失格)")


def extract_query_entities(query: str, known_norm_names: Iterable[str]) -> list[str]:
    if not query:
        return []
    # 1) 正则命中
    found = set(_ENTITY_HINT_REGEX.findall(query))
    # 2) 已知 norm_name 完全匹配（按长度从长到短，避免子串误匹配）
    for name in sorted(set(known_norm_names), key=len, reverse=True):
        if len(name) >= 2 and name in query:
            found.add(name)
    return list(found)


# =========================================================
# 加载 NetworkX 图（内存缓存，每次 System2 刷新后失效重加载）
# =========================================================
_GRAPH_VERSION = 0
_GRAPHS_BY_USER: dict[str, "nx.DiGraph"] = {}
_NODE_NORM_NAMES_BY_USER: dict[str, set[str]] = {}


def bump_graph_version() -> None:
    """System2 改完 graph_nodes/edges 后调一下，让下次 graph.search 重建。"""
    global _GRAPH_VERSION
    _GRAPH_VERSION += 1
    _GRAPHS_BY_USER.clear()
    _NODE_NORM_NAMES_BY_USER.clear()


async def _load_graph_if_needed(session: "AsyncSession", user_id: str) -> "nx.DiGraph":
    if user_id in _GRAPHS_BY_USER:
        return _GRAPHS_BY_USER[user_id]
    G = nx.DiGraph()
    rows = (await session.execute(
        text("SELECT node_id, node_type, node_name, entity_norm_name FROM graph_nodes WHERE user_id=:uid"),
        {"uid": user_id},
    )).all()
    norms: set[str] = set()
    for r in rows:
        G.add_node(r.node_id, ntype=r.node_type, name=r.node_name, norm=r.entity_norm_name)
        norms.add(r.entity_norm_name or r.node_name)
    edges = (await session.execute(
        text("SELECT src, dst, weight FROM graph_edges WHERE user_id=:uid"),
        {"uid": user_id},
    )).all()
    for e in edges:
        G.add_edge(e.src, e.dst, weight=float(e.weight or 1.0))
    _GRAPHS_BY_USER[user_id] = G
    _NODE_NORM_NAMES_BY_USER[user_id] = norms
    return G


async def search(
    session: "AsyncSession",
    user_id: str,
    query: str,
    topk: int = 12,
) -> list[GraphHit]:
    G = await _load_graph_if_needed(session, user_id)
    if not G.number_of_nodes():
        return []
    ents = extract_query_entities(query, _NODE_NORM_NAMES_BY_USER.get(user_id, set()))
    if not ents:
        return []
    # norm → node_id
    q_nodes = {nid for nid, d in G.nodes(data=True) if d.get("norm") in ents or d.get("name") in ents}
    if not q_nodes:
        return []
    # 1-hop / 2-hop
    hit_scores: dict[str, float] = {}
    hit_paths: dict[str, list[str]] = {}
    for n in q_nodes:
        # 1 hop
        for nb in G.neighbors(n):
            s = hit_scores.get(nb, 0.0) + 1.0
            hit_scores[nb] = s
            hit_paths[nb] = [G.nodes[n].get("name", n), G.nodes[nb].get("name", nb)]
        # 2 hops
        for nb in G.neighbors(n):
            for nb2 in G.neighbors(nb):
                if nb2 == n:
                    continue
                s = hit_scores.get(nb2, 0.0) + 0.6
                hit_scores[nb2] = s
                hit_paths[nb2] = [G.nodes[n].get("name", n), G.nodes[nb].get("name", nb), G.nodes[nb2].get("name", nb2)]
    if not hit_scores:
        return []
    # 按得分排序取 topk*2，再从这些节点的 episodic_id 反查：
    # graph_nodes 里我们把「Episodic」当作节点类型，node_id 形如 "ep:<episodic_id>"，
    # 这样 person/location → ep:XXX 的边就能直接连到石子
    cand_nodes = sorted(hit_scores.items(), key=lambda x: x[1], reverse=True)[:topk * 2]
    result: list[GraphHit] = []
    seen: set[str] = set()
    for node_id, score in cand_nodes:
        if not node_id.startswith("ep:"):
            continue
        eid = node_id[3:]
        if eid in seen:
            continue
        seen.add(eid)
        result.append(GraphHit(episodic_id=eid, score=score, path_entities=hit_paths.get(node_id, [])))
        if len(result) >= topk:
            break
    return result
