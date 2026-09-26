from __future__ import annotations

import pytest

from app.families import topology


def _node(node_id: int, *, jurisdiction: str = "CN", number: str | None = None, **dates):
    return {
        "id": node_id,
        "jurisdiction": jurisdiction,
        "application_number": number or f"A{node_id}",
        "filing_date": dates.get("filing_date", "2025-01-01"),
        "priority_claim_date": dates.get("priority_claim_date"),
    }


def _link(child: int, parent: int, *, status: str = "active", claimed: str | None = None):
    return {
        "child_application_id": child,
        "parent_application_id": parent,
        "status": status,
        "claimed_priority_date": claimed,
    }


def test_topological_order_is_deterministic_on_ties():
    nodes = [_node(1, number="Z"), _node(2, number="A"), _node(3, number="M")]
    assert topology.topological_order(nodes, []) == [2, 3, 1]


def test_topological_order_respects_edges():
    nodes = [_node(1), _node(2), _node(3)]
    links = [_link(2, 1), _link(3, 2)]
    assert topology.topological_order(nodes, links) == [1, 2, 3]


def test_revoked_edges_are_ignored_in_topology():
    nodes = [_node(1), _node(2)]
    links = [_link(2, 1, status="revoked"), _link(1, 2)]
    # 仅剩 1->2 活跃边
    assert topology.topological_order(nodes, links) == [2, 1]


def test_cycle_detection_returns_path():
    nodes = [_node(1), _node(2), _node(3)]
    links = [_link(2, 1), _link(3, 2), _link(1, 3)]
    with pytest.raises(topology.CyclicRelationError) as exc:
        topology.topological_order(nodes, links)
    assert exc.value.cycle[0] == exc.value.cycle[-1]
    assert set(exc.value.cycle) == {1, 2, 3}


def test_would_create_cycle_detects_closing_edge():
    nodes = [_node(1), _node(2), _node(3)]
    links = [_link(2, 1), _link(3, 2)]
    cycle = topology.would_create_cycle(nodes, links, 1, 3)
    assert cycle
    assert topology.would_create_cycle(nodes, links, 3, 1) == []


def test_priority_date_propagates_through_chain():
    nodes = [
        _node(1, priority_claim_date="2020-01-01", filing_date="2020-01-05"),
        _node(2, filing_date="2021-02-01"),
        _node(3, jurisdiction="US", filing_date="2022-03-01"),
    ]
    links = [_link(2, 1), _link(3, 2)]
    dates = topology.effective_priority_dates(nodes, links)
    assert dates == {1: "2020-01-01", 2: "2020-01-01", 3: "2020-01-01"}


def test_priority_date_takes_earliest_across_multiple_parents():
    nodes = [
        _node(1, priority_claim_date="2019-06-01", filing_date="2019-06-01"),
        _node(2, priority_claim_date="2018-01-01", filing_date="2018-01-01"),
        _node(3, filing_date="2020-01-01"),
    ]
    links = [_link(3, 1), _link(3, 2)]
    dates = topology.effective_priority_dates(nodes, links)
    assert dates[3] == "2018-01-01"


def test_link_claimed_priority_date_can_bring_earlier_date():
    nodes = [
        _node(1, priority_claim_date="2020-01-01", filing_date="2020-01-01"),
        _node(2, filing_date="2021-01-01"),
    ]
    links = [_link(2, 1, claimed="2019-01-01")]
    dates = topology.effective_priority_dates(nodes, links)
    assert dates[2] == "2019-01-01"
