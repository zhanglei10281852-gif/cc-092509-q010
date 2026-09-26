"""专利家族关系图的纯函数算法。

所有函数只接收普通数据结构，不访问数据库，便于单元测试与在事务外预演。
排序一律使用 (业务键, id) 的全序，保证同一份数据在服务重启后得到
完全一致的拓扑顺序与审计顺序。
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable, Sequence


class CyclicRelationError(ValueError):
    """拟议关系会使申请关系图形成环。"""

    def __init__(self, cycle: Sequence[int]):
        self.cycle = list(cycle)
        super().__init__(f"关系存在循环引用：{' -> '.join(str(item) for item in cycle)}")


def sort_key(node: dict[str, Any]) -> tuple[str, str, int]:
    return (str(node.get("jurisdiction") or ""), str(node.get("application_number") or ""), int(node["id"]))


def ordered_node_ids(nodes: Iterable[dict[str, Any]]) -> list[int]:
    return [int(node["id"]) for node in sorted(nodes, key=sort_key)]


def build_adjacency(
    node_ids: Iterable[int], links: Iterable[dict[str, Any]]
) -> tuple[dict[int, list[int]], dict[int, list[int]], dict[tuple[int, int], dict[str, Any]]]:
    """返回 (父->子, 子->父, 活跃边字典)，邻接表按 id 排序。"""
    nodes = set(node_ids)
    children: dict[int, list[int]] = defaultdict(list)
    parents: dict[int, list[int]] = defaultdict(list)
    edge_map: dict[tuple[int, int], dict[str, Any]] = {}
    for link in links:
        if link.get("status") and link["status"] != "active":
            continue
        child = int(link["child_application_id"])
        parent = int(link["parent_application_id"])
        if child not in nodes or parent not in nodes:
            continue
        children[parent].append(child)
        parents[child].append(parent)
        edge_map[(child, parent)] = link
    for targets in children.values():
        targets.sort()
    for targets in parents.values():
        targets.sort()
    return children, parents, edge_map


def topological_order(
    nodes: Sequence[dict[str, Any]], links: Sequence[dict[str, Any]]
) -> list[int]:
    """Kahn 拓扑排序。入度相同时按 (jurisdiction, application_number, id) 决断。

    只统计活跃边；检测到环时抛出带具体环路径的 CyclicRelationError。
    """
    node_by_id = {int(node["id"]): node for node in nodes}
    children, parents, _ = build_adjacency(node_by_id, links)
    indegree = {node_id: len(parents.get(node_id, [])) for node_id in node_by_id}
    ready: deque[int] = deque(
        sorted(
            (node_id for node_id, degree in indegree.items() if degree == 0),
            key=lambda value: sort_key(node_by_id[value]),
        )
    )
    order: list[int] = []
    while ready:
        node_id = ready.popleft()
        order.append(node_id)
        newly_ready: list[int] = []
        for child in children.get(node_id, []):
            indegree[child] -= 1
            if indegree[child] == 0:
                newly_ready.append(child)
        ready.extend(sorted(newly_ready, key=lambda value: sort_key(node_by_id[value])))
    if len(order) != len(node_by_id):
        cycle = find_cycle(node_by_id, children)
        raise CyclicRelationError(cycle)
    return order


def find_cycle(
    node_by_id: dict[int, dict[str, Any]], children: dict[int, list[int]]
) -> list[int]:
    """在残留子图中用确定性 DFS 找一条环（用于错误提示）。"""
    visiting: set[int] = set()
    visited: set[int] = set()
    stack: list[int] = []

    def visit(node_id: int) -> list[int] | None:
        visiting.add(node_id)
        stack.append(node_id)
        for child in sorted(children.get(node_id, [])):
            if child in visiting:
                start = stack.index(child)
                return stack[start:] + [child]
            if child not in visited:
                found = visit(child)
                if found is not None:
                    return found
        stack.pop()
        visiting.discard(node_id)
        visited.add(node_id)
        return None

    for node_id in sorted(node_by_id, key=lambda value: sort_key(node_by_id[value])):
        if node_id not in visited:
            found = visit(node_id)
            if found is not None:
                return found
    return []


def would_create_cycle(
    nodes: Sequence[dict[str, Any]],
    links: Sequence[dict[str, Any]],
    new_child: int,
    new_parent: int,
) -> list[int]:
    """若新增 child -> parent 边会成环，返回环路径；否则返回空列表。"""
    node_by_id = {int(node["id"]): node for node in nodes}
    if new_child not in node_by_id or new_parent not in node_by_id:
        return []
    pair = (new_child, new_parent)
    existing = {
        (int(link["child_application_id"]), int(link["parent_application_id"]))
        for link in links
        if link.get("status", "active") == "active"
    }
    candidate_links = list(links)
    if pair not in existing:
        candidate_links.append({
            "child_application_id": new_child,
            "parent_application_id": new_parent,
            "status": "active",
        })
    try:
        topological_order(nodes, candidate_links)
    except CyclicRelationError as exc:
        return exc.cycle
    return []


def earliest_date(values: Iterable[str | None]) -> str | None:
    present = sorted(value for value in values if value)
    return present[0] if present else None


def effective_priority_dates(
    nodes: Sequence[dict[str, Any]], links: Sequence[dict[str, Any]]
) -> dict[int, str | None]:
    """沿 DAG 传播最早优先权日。

    节点自身的 priority_claim_date / filing_date 为初始值，
    沿 parent -> child 方向继承，任一祖先的更早日期都会传播到后代。
    """
    order = topological_order(nodes, links)
    _, parents, edge_map = build_adjacency({int(n["id"]) for n in nodes}, links)
    dates: dict[int, str | None] = {}
    for node in nodes:
        node_id = int(node["id"])
        dates[node_id] = node.get("priority_claim_date") or node.get("filing_date")
    for node_id in order:
        incoming = []
        for parent_id in parents.get(node_id, []):
            link = edge_map.get((node_id, parent_id))
            if link and link.get("claimed_priority_date"):
                incoming.append(link["claimed_priority_date"])
            incoming.append(dates.get(parent_id))
        dates[node_id] = earliest_date([dates.get(node_id), *incoming])
    return dates


def reachable_descendants(
    node_ids: Iterable[int], links: Iterable[dict[str, Any]], roots: Iterable[int]
) -> set[int]:
    children, _, _ = build_adjacency(set(node_ids), links)
    seen: set[int] = set()
    queue = deque(roots)
    while queue:
        current = queue.popleft()
        for child in children.get(current, []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen
