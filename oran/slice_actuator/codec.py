"""E2SM-RC v1.03 Style 2 / Action 6 Control Format 1 tree codec.

The result is a lossless, transport-neutral representation of FlexRIC's
``rc_ctrl_req_data_t`` RAN-parameter tree.  The forked C++ adapter maps these
same nodes onto FlexRIC allocation types before APER encoding.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .model import PlmnIdentity, PrbRatios, SliceIdentity, SliceQuota


class CodecError(ValueError):
    """A request cannot be represented as Style 2 / Action 6."""


def _element(parameter_id: int, name: str, *, integer: int | None = None,
             octets: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "ELEMENT"}
    if integer is not None:
        value["integer"] = integer
    elif octets is not None:
        value["octets"] = octets.lower()
    else:  # pragma: no cover - internal invariant
        raise AssertionError("ELEMENT requires one value")
    return {"id": parameter_id, "name": name, "value": value}


def _structure(parameter_id: int, name: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": parameter_id, "name": name, "value": {"type": "STRUCTURE", "children": children}}


def _list(parameter_id: int, name: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": parameter_id, "name": name, "value": {"type": "LIST", "items": items}}


def _member(identity: SliceIdentity) -> dict[str, Any]:
    snssai_children = [_element(9, "SST", octets=f"{identity.sst:02x}")]
    if identity.sd is not None:
        snssai_children.append(_element(10, "SD", octets=identity.sd))
    return _structure(6, "RRM Policy Member", [
        _element(7, "PLMN Identity", octets=identity.plmn.tbcd_hex()),
        _structure(8, "S-NSSAI", snssai_children),
    ])


def encode_control_request(
    quotas: Iterable[SliceQuota], *, ue_anchor_ref: str,
) -> dict[str, Any]:
    """Build the complete §8.4.3.6 tree, rejecting invalid aggregates early."""
    if not isinstance(ue_anchor_ref, str) or not ue_anchor_ref.strip():
        raise CodecError("Control Header Format 1 requires a non-empty UE anchor reference")
    values = tuple(quotas)
    if not values:
        raise CodecError("RRM Policy Ratio List must contain at least one slice")
    if any(not isinstance(value, SliceQuota) for value in values):
        raise CodecError("every RRM Policy Ratio Group must be a SliceQuota")
    keys = [value.identity.key() for value in values]
    if len(keys) != len(set(keys)):
        raise CodecError("duplicate S-NSSAI member in RRM Policy Ratio List")
    minimum_sum = sum(value.ratios.minimum for value in values)
    dedicated_sum = sum(value.ratios.dedicated for value in values)
    if dedicated_sum > 100:
        raise CodecError(f"aggregate dedicated PRB policy ratio is {dedicated_sum}, above 100")
    if minimum_sum > 100:
        raise CodecError(f"aggregate minimum PRB policy ratio is {minimum_sum}, above 100")

    groups = []
    for quota in values:
        policy = _structure(3, "RRM Policy", [
            _list(5, "RRM Policy Member List", [_member(quota.identity)]),
        ])
        groups.append(_structure(2, "RRM Policy Ratio Group", [
            policy,
            _element(11, "Min PRB Policy Ratio", integer=quota.ratios.minimum),
            _element(12, "Max PRB Policy Ratio", integer=quota.ratios.maximum),
            _element(13, "Dedicated PRB Policy Ratio", integer=quota.ratios.dedicated),
        ]))
    return {
        "header": {
            "format": 1,
            "ricStyleType": 2,
            "ricControlActionId": 6,
            "ueId": {"anchorRef": ue_anchor_ref},
        },
        "message": {"format": 1, "ranParameters": [
            _list(1, "RRM Policy Ratio List", groups),
        ]},
    }


def _require_node(node: Any, parameter_id: int, name: str, value_type: str) -> Mapping[str, Any]:
    if not isinstance(node, Mapping) or node.get("id") != parameter_id or node.get("name") != name:
        raise CodecError(f"expected RAN Parameter {parameter_id} {name}")
    value = node.get("value")
    if not isinstance(value, Mapping) or value.get("type") != value_type:
        raise CodecError(f"RAN Parameter {parameter_id} {name} must be {value_type}")
    return value


def _only_ids(nodes: Any, expected: Sequence[int], context: str) -> list[Mapping[str, Any]]:
    if not isinstance(nodes, list) or [node.get("id") if isinstance(node, Mapping) else None for node in nodes] != list(expected):
        raise CodecError(f"{context} must contain RAN Parameters {list(expected)} in order")
    return nodes


def _integer(node: Mapping[str, Any], parameter_id: int, name: str) -> int:
    value = _require_node(node, parameter_id, name, "ELEMENT")
    integer = value.get("integer")
    if isinstance(integer, bool) or not isinstance(integer, int):
        raise CodecError(f"RAN Parameter {parameter_id} {name} must contain an integer")
    return integer


def _octets(node: Mapping[str, Any], parameter_id: int, name: str) -> str:
    value = _require_node(node, parameter_id, name, "ELEMENT")
    octets = value.get("octets")
    if not isinstance(octets, str):
        raise CodecError(f"RAN Parameter {parameter_id} {name} must contain octets")
    return octets


def decode_control_request(request: Mapping[str, Any]) -> tuple[SliceQuota, ...]:
    """Strictly parse the full tree; unknown, reordered, or partial trees fail."""
    try:
        header = request["header"]
        if not isinstance(header, Mapping) or any(
            header.get(key) != value
            for key, value in (
                ("format", 1), ("ricStyleType", 2), ("ricControlActionId", 6),
            )
        ):
            raise CodecError("Control Header must be Format 1, Style 2, Action 6")
        ue_id = header.get("ueId")
        ue_anchor_ref = ue_id.get("anchorRef") if isinstance(ue_id, Mapping) else None
        if not isinstance(ue_anchor_ref, str) or not ue_anchor_ref.strip():
            raise CodecError("Control Header Format 1 requires a non-empty UE anchor reference")
        message = request["message"]
        if not isinstance(message, Mapping) or message.get("format") != 1:
            raise CodecError("Control Message must be Format 1")
        roots = _only_ids(
            message.get("ranParameters"), [1],
            "Control Message RRM Policy Ratio List",
        )
        root = _require_node(roots[0], 1, "RRM Policy Ratio List", "LIST")
        items = root.get("items")
        if not isinstance(items, list) or not items:
            raise CodecError("RRM Policy Ratio List must not be empty")
        quotas: list[SliceQuota] = []
        for item in items:
            group = _require_node(item, 2, "RRM Policy Ratio Group", "STRUCTURE")
            children = _only_ids(group.get("children"), [3, 11, 12, 13], "RRM Policy Ratio Group")
            policy = _require_node(children[0], 3, "RRM Policy", "STRUCTURE")
            policy_children = _only_ids(policy.get("children"), [5], "RRM Policy")
            member_list = _require_node(policy_children[0], 5, "RRM Policy Member List", "LIST")
            members = member_list.get("items")
            if not isinstance(members, list) or len(members) != 1:
                raise CodecError("RRM Policy Member List must contain exactly one slice member")
            member = _require_node(members[0], 6, "RRM Policy Member", "STRUCTURE")
            member_children = member.get("children")
            if not isinstance(member_children, list) or [node.get("id") for node in member_children] != [7, 8]:
                raise CodecError("RRM Policy Member must contain PLMN Identity and S-NSSAI")
            plmn = PlmnIdentity.from_tbcd_hex(_octets(member_children[0], 7, "PLMN Identity"))
            snssai = _require_node(member_children[1], 8, "S-NSSAI", "STRUCTURE")
            snssai_children = snssai.get("children")
            if not isinstance(snssai_children, list) or [node.get("id") for node in snssai_children] not in ([9], [9, 10]):
                raise CodecError("S-NSSAI must contain SST and optional SD")
            sst_octets = _octets(snssai_children[0], 9, "SST")
            if len(sst_octets) != 2:
                raise CodecError("RAN Parameter 9 SST must contain one octet")
            try:
                sst = int(sst_octets, 16)
            except ValueError as exc:
                raise CodecError("RAN Parameter 9 SST is not hexadecimal") from exc
            sd = _octets(snssai_children[1], 10, "SD").upper() if len(snssai_children) == 2 else None
            ratios = PrbRatios(
                minimum=_integer(children[1], 11, "Min PRB Policy Ratio"),
                maximum=_integer(children[2], 12, "Max PRB Policy Ratio"),
                dedicated=_integer(children[3], 13, "Dedicated PRB Policy Ratio"),
            )
            quotas.append(SliceQuota(SliceIdentity(plmn, sst, sd), ratios))
        # Re-run cross-group validation and reject non-canonical duplicates/aggregates.
        canonical = encode_control_request(quotas, ue_anchor_ref=ue_anchor_ref)
        if canonical != request:
            raise CodecError("request is not the canonical Style 2 / Action 6 tree")
        return tuple(quotas)
    except CodecError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CodecError(f"malformed Style 2 / Action 6 request: {exc}") from exc
