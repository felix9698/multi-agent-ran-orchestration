"""Validated domain values shared by the A1 binding and RC codec."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


_SD = re.compile(r"^[0-9A-Fa-f]{6}$")


@dataclass(frozen=True)
class PlmnIdentity:
    mcc: str
    mnc: str

    def __post_init__(self) -> None:
        if not (isinstance(self.mcc, str) and self.mcc.isdigit() and len(self.mcc) == 3):
            raise ValueError("MCC must contain exactly three decimal digits")
        if not (isinstance(self.mnc, str) and self.mnc.isdigit() and len(self.mnc) in (2, 3)):
            raise ValueError("MNC must contain two or three decimal digits")

    def tbcd_hex(self) -> str:
        """Return the 3-octet 3GPP PLMN identity representation."""
        mcc1, mcc2, mcc3 = (int(digit) for digit in self.mcc)
        mnc_digits = [int(digit) for digit in self.mnc]
        mnc1, mnc2 = mnc_digits[:2]
        mnc3 = mnc_digits[2] if len(mnc_digits) == 3 else 0xF
        return bytes((mcc2 << 4 | mcc1, mnc3 << 4 | mcc3, mnc2 << 4 | mnc1)).hex()

    @classmethod
    def from_tbcd_hex(cls, value: str) -> "PlmnIdentity":
        try:
            octet1, octet2, octet3 = bytes.fromhex(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("PLMN identity must be exactly three TBCD octets") from exc
        mcc = f"{octet1 & 0xF}{octet1 >> 4}{octet2 & 0xF}"
        mnc3 = octet2 >> 4
        mnc = f"{octet3 & 0xF}{octet3 >> 4}" + ("" if mnc3 == 0xF else str(mnc3))
        if any(int(digit) > 9 for digit in mcc + mnc):
            raise ValueError("PLMN identity contains a non-decimal TBCD digit")
        return cls(mcc=mcc, mnc=mnc)


@dataclass(frozen=True)
class SliceIdentity:
    plmn: PlmnIdentity
    sst: int
    sd: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sst, bool) or not isinstance(self.sst, int) or not 1 <= self.sst <= 255:
            raise ValueError("S-NSSAI SST must be an integer in 1..255")
        if self.sd is not None:
            if not isinstance(self.sd, str) or _SD.fullmatch(self.sd) is None:
                raise ValueError("S-NSSAI SD must be six hexadecimal digits")
            object.__setattr__(self, "sd", self.sd.upper())

    def key(self) -> str:
        return f"{self.plmn.mcc}-{self.plmn.mnc}/{self.sst}/{self.sd or '-'}"

    def as_scope(self) -> dict[str, Any]:
        snssai: dict[str, Any] = {"sst": self.sst}
        if self.sd is not None:
            snssai["sd"] = self.sd
        return {
            "plmnId": {"mcc": self.plmn.mcc, "mnc": self.plmn.mnc},
            "snssai": snssai,
        }

    @classmethod
    def from_scope(cls, scope: Mapping[str, Any]) -> "SliceIdentity":
        try:
            plmn = scope["plmnId"]
            snssai = scope["snssai"]
            if not isinstance(plmn, Mapping) or not isinstance(snssai, Mapping):
                raise TypeError
            return cls(
                plmn=PlmnIdentity(plmn["mcc"], plmn["mnc"]),
                sst=snssai["sst"],
                sd=snssai.get("sd"),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("scope must contain PLMN identity and S-NSSAI") from exc


@dataclass(frozen=True)
class PrbRatios:
    minimum: int
    maximum: int
    dedicated: int

    def __post_init__(self) -> None:
        for name, value in (
            ("minimum", self.minimum),
            ("maximum", self.maximum),
            ("dedicated", self.dedicated),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                raise ValueError(f"{name} PRB policy ratio must be an integer in 0..100")
        if not self.dedicated <= self.minimum <= self.maximum:
            raise ValueError(
                "quota must satisfy dedicatedPrbPolicyRatio <= "
                "minPrbPolicyRatio <= maxPrbPolicyRatio"
            )

    def as_policy(self) -> dict[str, int]:
        return {
            "minPrbPolicyRatio": self.minimum,
            "maxPrbPolicyRatio": self.maximum,
            "dedicatedPrbPolicyRatio": self.dedicated,
        }

    @classmethod
    def from_policy(cls, value: Mapping[str, Any]) -> "PrbRatios":
        try:
            return cls(
                minimum=value["minPrbPolicyRatio"],
                maximum=value["maxPrbPolicyRatio"],
                dedicated=value["dedicatedPrbPolicyRatio"],
            )
        except KeyError as exc:
            raise ValueError(f"quota is missing {exc.args[0]}") from exc


@dataclass(frozen=True)
class SliceQuota:
    identity: SliceIdentity
    ratios: PrbRatios


def quota_from_policy(policy: Mapping[str, Any]) -> SliceQuota:
    try:
        scope = policy["scope"]
        quota = policy["quota"]
    except KeyError as exc:
        raise ValueError(f"policy is missing {exc.args[0]}") from exc
    if not isinstance(scope, Mapping) or not isinstance(quota, Mapping):
        raise ValueError("scope and quota must be objects")
    return SliceQuota(SliceIdentity.from_scope(scope), PrbRatios.from_policy(quota))
