"""专利家族关系图的纯函数算法。

只依赖内存中的节点与边列表，所有排序键都取申请主键，
保证同一数据库状态下任意进程、任意重启后得到完全一致的拓扑顺序。
"""

from __future__ import annotations

import heapq
from typing import Any


def build_indexes(edges: list[dict[str, Any]]) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    """返回 parent→边 与 child→边 两个索引，边按主键升序。"""
    children_of: dict[int, list[dict[str, Any]]] = {}
    parents_of: dict[int, list[dict[str, Any]]] = {}
    for edge in sorted(edges, key=lambda item: item["id"]):
        children_of.setdefault(edge["parent_application_id"], []).append(edge)
        parents_of.setdefault(edge["child_application_id"], []).append(edge)
    return children_of, parents_of


def topological_order(application_ids: list[int], edges: list[dict[str, Any]]) -> list[int]:
    """Kahn 拓扑排序，堆键为申请主键，结果确定。存在环时抛出 ValueError。"""
    children_of, parents_of = build_indexes(edges)
    indegree = {application_id: len(parents_of.get(application_id, [])) for application_id in application_ids}
    heap = [application_id for application_id, degree in indegree.items() if degree == 0]
    heapq.heapify(heap)
    order: list[int] = []
    while heap:
        current = heapq.heappop(heap)
        order.append(current)
        for edge in children_of.get(current, []):
            child = edge["child_application_id"]
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(heap, child)
    if len(order) != len(application_ids):
        raise ValueError("关系图中存在循环")
    return order


def reachable(start_id: int, edges: list[dict[str, Any]]) -> set[int]:
    """沿 parent→child 方向从 start_id 可达的全部申请集合（不含自身）。"""
    children_of, _ = build_indexes(edges)
    seen: set[int] = set()
    stack = [start_id]
    while stack:
        current = stack.pop()
        for edge in children_of.get(current, []):
            child = edge["child_application_id"]
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def effective_priority_dates(
    applications: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[dict[int, str], dict[int, int], dict[int, int | None]]:
    """按拓扑序计算每件申请的生效优先权日期。

    返回 (生效日期, 贡献来源申请 id, 继承经由的父申请 id)。
    生效日期取自身声明优先权日（缺省为申请日）与全部父申请生效日期的最小值；
    日期相同取来源申请主键较小者，保证结果确定。
    """
    _, parents_of = build_indexes(edges)
    order = topological_order([item["id"] for item in applications], edges)
    base = {
        item["id"]: (item["declared_priority_date"] or item["filing_date"])
        for item in applications
    }
    effective: dict[int, str] = {}
    source: dict[int, int] = {}
    via: dict[int, int | None] = {}
    for application_id in order:
        best_date = base[application_id]
        best_source = application_id
        best_via: int | None = None
        for edge in parents_of.get(application_id, []):
            parent_id = edge["parent_application_id"]
            parent_date = effective[parent_id]
            parent_source = source[parent_id]
            if parent_date < best_date or (parent_date == best_date and parent_source < best_source):
                best_date = parent_date
                best_source = parent_source
                best_via = parent_id
        effective[application_id] = best_date
        source[application_id] = best_source
        via[application_id] = best_via
    return effective, source, via


def connected_components(application_ids: list[int], edges: list[dict[str, Any]]) -> int:
    """无向连通分量数量，用于衡量撤销关系后家族图是否被拆分。"""
    parent = {application_id: application_id for application_id in application_ids}

    def find(value: int) -> int:
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != root:
            parent[value], value = root, parent[value]
        return root

    for edge in edges:
        left, right = find(edge["parent_application_id"]), find(edge["child_application_id"])
        if left != right:
            parent[max(left, right)] = min(left, right)
    return len({find(application_id) for application_id in application_ids})
