"""What an Amiga release needs (chipset, model, Kickstart, CPU, PAL / NTSC) and so which machine to run it on, from what is known of
it: the WHDLoad slave's own flags (``ReqAGA``, ``Req68020``: WHDLoad refuses to start without them), TOSEC's tags in the disk names
("(AGA)", "(OCS-AGA)", "(ECS)", "(A500)", "(CD32)", "(NTSC)" ...) and the OpenRetro release (model, chipset, Kickstart, video).
The strongest of them counts.

    req = requirements(["Pinball Fantasies (1992)(21st Century)(AGA)(Disk 1 of 4).adf", ...], release=..., slave=...)
    req["machine"]            # "A1200": the machine for the floppies; req["whdload"]: for its WHDLoad install
"""

from __future__ import annotations

import re
import struct
from typing import Any, Iterable, Optional

__all__ = ["requirements", "slave_flags"]

_CHIPSETS = ("OCS", "ECS", "AGA")
_TAG_RE = re.compile(r"\(([^()]*)\)")


def slave_flags(slave: bytes) -> Optional[dict[str, Any]]:
    """``{version, needs_aga, needs_68020, chip_memory, exp_memory, exp_optional}`` of a WHDLoad slave (its header: "WHDLOADS" 4 bytes
    into its code; ``ws_ExpMem`` from version 8, negative: optional), or None."""
    try:
        u32 = lambda o: struct.unpack_from(">I", slave, o)[0]
        if u32(0) != 0x3F3:
            return None
        o = 4
        while u32(o):
            o += 4 + 4 * u32(o)
        o += 4
        first, last = u32(o + 4), u32(o + 8)
        o += 12
        for _ in range(last - first + 1):
            o += 8 if u32(o) >> 30 == 3 else 4
        if u32(o) & 0x3FFFFFFF != 0x3E9:
            return None
        code = slave[o + 8:o + 8 + 64]
        if code[4:12] != b"WHDLOADS":
            return None
        version, flags = struct.unpack_from(">HH", code, 12)
        exp = struct.unpack_from(">i", code, 32)[0] if version >= 8 and len(code) >= 36 else 0
        return {"version": version, "needs_aga": bool(flags & 0x20), "needs_68020": bool(flags & 0x10),
                "chip_memory": struct.unpack_from(">I", code, 16)[0], "exp_memory": abs(exp), "exp_optional": exp < 0}
    except (struct.error, IndexError):
        return None


def _rank(chipset: str) -> int:
    return _CHIPSETS.index(chipset) if chipset in _CHIPSETS else -1


def requirements(names: Iterable[str], release: Optional[dict[str, Any]] = None, slave: Optional[bytes] = None) -> dict[str, Any]:
    """``{chipset, model, kickstart, cpu, video, machine, whdload, fast_memory, sources}``: what is known ("" for nothing known);
    ``machine`` the one to run the disks on (CD32 / CDTV, AGA: A1200, ECS or Kickstart 2+: A600, else A500), ``whdload`` the one for
    a WHDLoad install (CD32 for a CD32 game, else an A1200: WHDLoad itself needs Kickstart 2+ and the memory); ``fast_memory``: the
    bytes the slave must have besides chip memory (0: none or optional)."""
    chipset, model, kick, cpu, video = "", "", "", "", ""
    sources: list[str] = []

    def need(c: str, why: str) -> None:
        nonlocal chipset
        if _rank(c) > _rank(chipset):
            chipset = c
        sources.append(why)

    for name in names:
        for tag in _TAG_RE.findall(name or ""):
            t = tag.strip()
            u = t.upper().replace("³²", "32")
            if u in ("AGA",):
                need("AGA", f"TOSEC: ({t})")
            elif u in ("ECS", "ECS-AGA"):
                need("ECS", f"TOSEC: ({t})")
            elif u in ("OCS", "OCS-AGA", "OCS-ECS"):
                need("OCS", f"TOSEC: ({t})")
            elif u in ("CD32", "CDTV"):
                model = u
                sources.append(f"TOSEC: ({t})")
            elif re.fullmatch(r"A(500\+?|600|1000|1200|2000|3000|4000)", u):
                model = model or u
                sources.append(f"TOSEC: ({t})")
            elif u in ("NTSC", "PAL"):
                video = video or u
    if release:
        rm, rc, rk, rv = (release.get(k) or "" for k in ("model", "chipset", "kickstart", "video"))
        if rc.upper() in _CHIPSETS:
            need(rc.upper(), f"OpenRetro: {rc}")
        if rm:
            m = rm.upper().split("/")[0]
            if m in ("CD32", "CDTV") or not model:
                model = m
            sources.append(f"OpenRetro: {rm}")
            if "/0" in rm or "020" in rm:
                cpu = cpu or "68020"
        if rk:
            kick = rk
            sources.append(f"OpenRetro: Kickstart {rk}")
        if rv.upper() in ("NTSC", "PAL"):
            video = video or rv.upper()
    flags = slave_flags(slave) if slave else None
    fast = flags["exp_memory"] if flags and not flags["exp_optional"] else 0
    if fast:
        sources.append(f"WHDLoad slave: {fast // 1024} KB expansion memory")
    if flags:
        if flags["needs_aga"]:
            need("AGA", "WHDLoad slave: needs AGA")
        if flags["needs_68020"]:
            cpu = "68020"
            sources.append("WHDLoad slave: needs a 68020")
    if model in ("CD32",):
        chipset = chipset if chipset == "AGA" else "AGA"
    if chipset == "AGA" and model in ("", "A500", "A500+", "A600", "A1000", "A2000", "A3000"):
        model = "A1200"
    if model in ("CD32", "CDTV"):
        machine = model
    elif chipset == "AGA" or model in ("A1200", "A4000"):
        machine = "A1200"
    elif chipset == "ECS" or model in ("A500+", "A600", "A3000") or re.match(r"[23]", kick):
        machine = "A600"
    else:
        machine = "A500"
    return {"chipset": chipset, "model": model, "kickstart": kick, "cpu": cpu, "video": video, "machine": machine,
            "whdload": "CD32" if model == "CD32" else "A1200", "fast_memory": fast, "sources": sources}
