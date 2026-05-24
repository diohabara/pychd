"""Python bytecode version detection and per-version dispatch.

A ``.pyc`` file's first four bytes encode the *magic number* of the
CPython release that produced it. The number changes every
minor-release (and sometimes between alpha builds) because the
bytecode specification is *not* stable across versions. pychd uses
the magic number to identify which rule pass to invoke and what
opcode quirks to expect.

pychd supports **every Python 3.x release** for disassembly and
LLM-based decompilation (via the embedded ``xdis`` cross-version
disassembler). The *rule-based* fast path currently targets the two
most recent stable releases, **3.13** and **3.14**, where the
bytecode layout is most relevant to today's users. Older versions
automatically fall through to the LLM pipeline.

What changes between versions
-----------------------------

Every Python minor release deletes opcodes, adds opcodes, and renames
others. The compiler also makes representational decisions
("statement X becomes opcode sequence Y") that look gratuitous in
isolation but ripple through every decompiler. The headline
differences pychd has to navigate, by epoch:

3.0–3.5 — the "stable but simple" era
    Opcode set is close to Python 2. Function definitions emit
    ``MAKE_FUNCTION`` flags as integer bitmasks. List comprehensions
    are still compiled to a separate ``<listcomp>`` code object.
    Annotations live in ``__annotations__`` if used.

3.6 — wordcode
    Every instruction is exactly two bytes (opcode + argument). This
    simplified disassembly forever but doubled `.pyc` size on average.

3.7–3.9 — the "modern but classical" era
    Async/await become first-class. ``CALL_FUNCTION_KW`` carries the
    keyword argument names as a tuple constant. ``MAKE_FUNCTION``
    flags still encode the (defaults, kw-defaults, annotations,
    closure) presence as a 4-bit field.

3.10 — match statements
    PEP 634 adds ``MATCH_CLASS``, ``MATCH_KEYS``, ``MATCH_MAPPING``
    etc. Decompilers have to recognise the structural patterns or
    misrender match bodies as `if/elif` chains.

3.11 — zero-cost exceptions
    PEP 657 replaces ``SETUP_FINALLY`` / ``POP_BLOCK`` blocks with an
    *exception table* mapping instruction ranges to handlers. Try
    bodies no longer have a runtime cost when they don't raise.
    Massive opcode renumbering. ``PRECALL`` + ``CALL`` split.

3.12 — comprehension inlining + PEP 695
    PEP 709 inlines list/set/dict comprehensions into the enclosing
    scope — no more ``<listcomp>`` code object. The compiler
    optimises away the closure but the bytecode shape changes
    completely. PEP 695 introduces ``type Alias = ...`` statements
    and generic ``def f[T]`` syntax.

3.13 — intrinsic call + MAKE_FUNCTION split
    ``CALL_INTRINSIC_1`` consolidates several opcodes (intrinsic 2 =
    ``INTRINSIC_IMPORT_STAR`` replaces the legacy ``IMPORT_STAR``).
    ``MAKE_FUNCTION`` is split into ``MAKE_FUNCTION`` (just the code
    object) + ``SET_FUNCTION_ATTRIBUTE`` (one per defaults /
    kwdefaults / annotations / closure).

3.14 — lazy annotations (PEP 749)
    Every annotated scope gets an ``__annotate__`` closure that
    returns the annotation dict on demand. Class-level annotation
    declarations no longer touch ``__annotations__`` at class body
    execution time. ``LOAD_SMALL_INT``, ``LOAD_FAST_BORROW``, and
    ``LOAD_COMMON_CONSTANT`` added.

pychd's rule pass currently understands 3.13 and 3.14 bytecode. Other
versions are routed to the LLM-only pipeline, which sees an xdis
disassembly of the bytecode and reconstructs the source from there.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class VersionInfo:
    """What pychd knows about one Python release."""

    version: tuple[int, int]
    magic_number: int
    rule_supported: bool  # True if the rule-based pass handles this version
    epoch_label: str
    quirks: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.version[0]}.{self.version[1]}"


# Magic numbers per minor release. Sources:
#   - importlib.util.MAGIC_NUMBER on local 3.10–3.14 interpreters
#     (the canonical answer for those versions)
#   - xdis.magics.canonic_python_version (for everything else)
#
# Every Python 3.x release is listed so that ``detect_version`` can
# always identify a .pyc. The ``rule_supported`` field separates
# "deterministic rule pass available" from "LLM-only fallback only".
KNOWN_VERSIONS: dict[int, VersionInfo] = {
    # ---- 3.0–3.5: stable-but-simple era ----------------------------
    3000: VersionInfo(
        (3, 0), 3000, False, "early 3.x", ["initial 3.0 release; lots of stdlib churn"]
    ),
    3010: VersionInfo((3, 0), 3010, False, "early 3.x", []),
    3020: VersionInfo((3, 1), 3020, False, "early 3.x", []),
    3021: VersionInfo((3, 1), 3021, False, "early 3.x", []),
    3030: VersionInfo((3, 1), 3030, False, "early 3.x", []),
    3031: VersionInfo((3, 1), 3031, False, "early 3.x", []),
    3040: VersionInfo((3, 1), 3040, False, "early 3.x", []),
    3050: VersionInfo((3, 1), 3050, False, "early 3.x", []),
    3060: VersionInfo((3, 1), 3060, False, "early 3.x", []),
    3061: VersionInfo((3, 1), 3061, False, "early 3.x", []),
    3071: VersionInfo((3, 1), 3071, False, "early 3.x", []),
    3081: VersionInfo((3, 1), 3081, False, "early 3.x", []),
    3091: VersionInfo((3, 1), 3091, False, "early 3.x", []),
    3101: VersionInfo((3, 1), 3101, False, "early 3.x", []),
    3103: VersionInfo((3, 1), 3103, False, "early 3.x", []),
    3111: VersionInfo((3, 1), 3111, False, "early 3.x", []),
    3131: VersionInfo((3, 1), 3131, False, "early 3.x", []),
    3141: VersionInfo((3, 2), 3141, False, "early 3.x", []),
    3151: VersionInfo((3, 2), 3151, False, "early 3.x", []),
    3160: VersionInfo((3, 3), 3160, False, "early 3.x", []),
    3170: VersionInfo((3, 3), 3170, False, "early 3.x", []),
    3180: VersionInfo((3, 3), 3180, False, "early 3.x", []),
    3190: VersionInfo((3, 4), 3190, False, "early 3.x", []),
    3200: VersionInfo((3, 4), 3200, False, "early 3.x", []),
    3220: VersionInfo((3, 4), 3220, False, "early 3.x", []),
    3230: VersionInfo((3, 4), 3230, False, "early 3.x", []),
    3250: VersionInfo((3, 5), 3250, False, "early 3.x", []),
    3260: VersionInfo((3, 5), 3260, False, "early 3.x", []),
    3270: VersionInfo((3, 5), 3270, False, "early 3.x", []),
    3280: VersionInfo((3, 5), 3280, False, "early 3.x", []),
    3290: VersionInfo((3, 5), 3290, False, "early 3.x", []),
    3300: VersionInfo((3, 5), 3300, False, "early 3.x", []),
    3310: VersionInfo((3, 5), 3310, False, "early 3.x", []),
    3320: VersionInfo((3, 5), 3320, False, "early 3.x", []),
    3330: VersionInfo((3, 5), 3330, False, "early 3.x", []),
    3340: VersionInfo((3, 5), 3340, False, "early 3.x", []),
    3350: VersionInfo((3, 5), 3350, False, "early 3.x", []),
    3351: VersionInfo((3, 5), 3351, False, "early 3.x", []),
    # ---- 3.6: wordcode -----------------------------------------------
    3360: VersionInfo(
        (3, 6),
        3360,
        False,
        "wordcode",
        ["every instruction is exactly 2 bytes (opcode + arg)"],
    ),
    3361: VersionInfo((3, 6), 3361, False, "wordcode", []),
    3370: VersionInfo((3, 6), 3370, False, "wordcode", []),
    3371: VersionInfo((3, 6), 3371, False, "wordcode", []),
    3372: VersionInfo((3, 6), 3372, False, "wordcode", []),
    3373: VersionInfo((3, 6), 3373, False, "wordcode", []),
    3375: VersionInfo((3, 6), 3375, False, "wordcode", []),
    3376: VersionInfo((3, 6), 3376, False, "wordcode", []),
    3377: VersionInfo((3, 6), 3377, False, "wordcode", []),
    3378: VersionInfo((3, 6), 3378, False, "wordcode", []),
    3379: VersionInfo((3, 6), 3379, False, "wordcode", []),
    # ---- 3.7–3.9: modern-but-classical era ---------------------------
    3390: VersionInfo(
        (3, 7),
        3390,
        False,
        "modern-classical",
        ["async/await first-class", "CALL_FUNCTION_KW carries kw names as tuple const"],
    ),
    3391: VersionInfo((3, 7), 3391, False, "modern-classical", []),
    3392: VersionInfo((3, 7), 3392, False, "modern-classical", []),
    3393: VersionInfo((3, 7), 3393, False, "modern-classical", []),
    3394: VersionInfo((3, 7), 3394, False, "modern-classical", []),
    3400: VersionInfo(
        (3, 8),
        3400,
        False,
        "modern-classical",
        ["walrus operator (PEP 572)", "PEP 570 positional-only parameters"],
    ),
    3401: VersionInfo((3, 8), 3401, False, "modern-classical", []),
    3410: VersionInfo((3, 8), 3410, False, "modern-classical", []),
    3411: VersionInfo((3, 8), 3411, False, "modern-classical", []),
    3412: VersionInfo((3, 8), 3412, False, "modern-classical", []),
    3413: VersionInfo((3, 8), 3413, False, "modern-classical", []),
    3420: VersionInfo(
        (3, 9),
        3420,
        False,
        "modern-classical",
        ["PEP 585 generic types in annotations (list[int])"],
    ),
    3421: VersionInfo((3, 9), 3421, False, "modern-classical", []),
    3422: VersionInfo((3, 9), 3422, False, "modern-classical", []),
    3423: VersionInfo((3, 9), 3423, False, "modern-classical", []),
    3424: VersionInfo((3, 9), 3424, False, "modern-classical", []),
    3425: VersionInfo((3, 9), 3425, False, "modern-classical", []),
    # ---- 3.10: match statement --------------------------------------
    3430: VersionInfo((3, 10), 3430, False, "match", ["match statement (PEP 634)"]),
    3431: VersionInfo((3, 10), 3431, False, "match", []),
    3432: VersionInfo((3, 10), 3432, False, "match", []),
    3433: VersionInfo((3, 10), 3433, False, "match", []),
    3434: VersionInfo((3, 10), 3434, False, "match", []),
    3435: VersionInfo((3, 10), 3435, False, "match", []),
    3436: VersionInfo((3, 10), 3436, False, "match", []),
    3437: VersionInfo((3, 10), 3437, False, "match", []),
    3438: VersionInfo((3, 10), 3438, False, "match", []),
    3439: VersionInfo(
        (3, 10),
        3439,
        False,
        "match",
        ["match statement (PEP 634); MATCH_CLASS/KEYS/MAPPING opcodes"],
    ),
    # ---- 3.11: zero-cost exceptions --------------------------------
    3450: VersionInfo((3, 11), 3450, False, "zero-cost-except", []),
    3451: VersionInfo((3, 11), 3451, False, "zero-cost-except", []),
    3452: VersionInfo((3, 11), 3452, False, "zero-cost-except", []),
    3453: VersionInfo((3, 11), 3453, False, "zero-cost-except", []),
    3454: VersionInfo((3, 11), 3454, False, "zero-cost-except", []),
    3455: VersionInfo((3, 11), 3455, False, "zero-cost-except", []),
    3456: VersionInfo((3, 11), 3456, False, "zero-cost-except", []),
    3457: VersionInfo((3, 11), 3457, False, "zero-cost-except", []),
    3458: VersionInfo((3, 11), 3458, False, "zero-cost-except", []),
    3459: VersionInfo((3, 11), 3459, False, "zero-cost-except", []),
    3460: VersionInfo((3, 11), 3460, False, "zero-cost-except", []),
    3461: VersionInfo((3, 11), 3461, False, "zero-cost-except", []),
    3462: VersionInfo((3, 11), 3462, False, "zero-cost-except", []),
    3463: VersionInfo((3, 11), 3463, False, "zero-cost-except", []),
    3464: VersionInfo((3, 11), 3464, False, "zero-cost-except", []),
    3465: VersionInfo((3, 11), 3465, False, "zero-cost-except", []),
    3466: VersionInfo((3, 11), 3466, False, "zero-cost-except", []),
    3467: VersionInfo((3, 11), 3467, False, "zero-cost-except", []),
    3468: VersionInfo((3, 11), 3468, False, "zero-cost-except", []),
    3469: VersionInfo((3, 11), 3469, False, "zero-cost-except", []),
    3470: VersionInfo((3, 11), 3470, False, "zero-cost-except", []),
    3471: VersionInfo((3, 11), 3471, False, "zero-cost-except", []),
    3472: VersionInfo((3, 11), 3472, False, "zero-cost-except", []),
    3473: VersionInfo((3, 11), 3473, False, "zero-cost-except", []),
    3474: VersionInfo((3, 11), 3474, False, "zero-cost-except", []),
    3475: VersionInfo((3, 11), 3475, False, "zero-cost-except", []),
    3476: VersionInfo((3, 11), 3476, False, "zero-cost-except", []),
    3477: VersionInfo((3, 11), 3477, False, "zero-cost-except", []),
    3478: VersionInfo((3, 11), 3478, False, "zero-cost-except", []),
    3479: VersionInfo((3, 11), 3479, False, "zero-cost-except", []),
    3480: VersionInfo((3, 11), 3480, False, "zero-cost-except", []),
    3481: VersionInfo((3, 11), 3481, False, "zero-cost-except", []),
    3482: VersionInfo((3, 11), 3482, False, "zero-cost-except", []),
    3483: VersionInfo((3, 11), 3483, False, "zero-cost-except", []),
    3484: VersionInfo((3, 11), 3484, False, "zero-cost-except", []),
    3485: VersionInfo((3, 11), 3485, False, "zero-cost-except", []),
    3486: VersionInfo((3, 11), 3486, False, "zero-cost-except", []),
    3487: VersionInfo((3, 11), 3487, False, "zero-cost-except", []),
    3488: VersionInfo((3, 11), 3488, False, "zero-cost-except", []),
    3489: VersionInfo((3, 11), 3489, False, "zero-cost-except", []),
    3490: VersionInfo((3, 11), 3490, False, "zero-cost-except", []),
    3491: VersionInfo((3, 11), 3491, False, "zero-cost-except", []),
    3492: VersionInfo((3, 11), 3492, False, "zero-cost-except", []),
    3493: VersionInfo((3, 11), 3493, False, "zero-cost-except", []),
    3494: VersionInfo((3, 11), 3494, False, "zero-cost-except", []),
    3495: VersionInfo(
        (3, 11),
        3495,
        False,
        "zero-cost-except",
        ["PEP 657 exception table replaces SETUP_FINALLY", "PRECALL + CALL split"],
    ),
    # ---- 3.12: PEP 695 + comp inlining ----------------------------
    3500: VersionInfo((3, 12), 3500, False, "comp-inline", []),
    3501: VersionInfo((3, 12), 3501, False, "comp-inline", []),
    3502: VersionInfo((3, 12), 3502, False, "comp-inline", []),
    3503: VersionInfo((3, 12), 3503, False, "comp-inline", []),
    3504: VersionInfo((3, 12), 3504, False, "comp-inline", []),
    3505: VersionInfo((3, 12), 3505, False, "comp-inline", []),
    3506: VersionInfo((3, 12), 3506, False, "comp-inline", []),
    3507: VersionInfo((3, 12), 3507, False, "comp-inline", []),
    3508: VersionInfo((3, 12), 3508, False, "comp-inline", []),
    3509: VersionInfo((3, 12), 3509, False, "comp-inline", []),
    3510: VersionInfo((3, 12), 3510, False, "comp-inline", []),
    3511: VersionInfo((3, 12), 3511, False, "comp-inline", []),
    3512: VersionInfo((3, 12), 3512, False, "comp-inline", []),
    3513: VersionInfo((3, 12), 3513, False, "comp-inline", []),
    3514: VersionInfo((3, 12), 3514, False, "comp-inline", []),
    3515: VersionInfo((3, 12), 3515, False, "comp-inline", []),
    3516: VersionInfo((3, 12), 3516, False, "comp-inline", []),
    3517: VersionInfo((3, 12), 3517, False, "comp-inline", []),
    3518: VersionInfo((3, 12), 3518, False, "comp-inline", []),
    3519: VersionInfo((3, 12), 3519, False, "comp-inline", []),
    3520: VersionInfo((3, 12), 3520, False, "comp-inline", []),
    3521: VersionInfo((3, 12), 3521, False, "comp-inline", []),
    3522: VersionInfo((3, 12), 3522, False, "comp-inline", []),
    3523: VersionInfo((3, 12), 3523, False, "comp-inline", []),
    3524: VersionInfo((3, 12), 3524, False, "comp-inline", []),
    3525: VersionInfo((3, 12), 3525, False, "comp-inline", []),
    3526: VersionInfo((3, 12), 3526, False, "comp-inline", []),
    3527: VersionInfo((3, 12), 3527, False, "comp-inline", []),
    3528: VersionInfo((3, 12), 3528, False, "comp-inline", []),
    3529: VersionInfo((3, 12), 3529, False, "comp-inline", []),
    3530: VersionInfo((3, 12), 3530, False, "comp-inline", []),
    3531: VersionInfo(
        (3, 12),
        3531,
        False,
        "comp-inline",
        [
            "PEP 709 list/set/dict comprehensions inlined",
            "PEP 695 type alias + generic syntax",
        ],
    ),
    # ---- 3.13: very close to 3.14 (no PEP 749) ---------------------
    # Bytecode shape is ~95% identical to 3.14: same MAKE_FUNCTION +
    # SET_FUNCTION_ATTRIBUTE split, same CALL_INTRINSIC_1, but no
    # `__annotate__` closure and `LOAD_CONST` instead of
    # `LOAD_SMALL_INT` for small ints. v1 routes 3.13 .pyc files
    # through the LLM-only path; adding a 3.13-aware rule pass is the
    # most natural next step and is on the roadmap.
    3550: VersionInfo((3, 13), 3550, False, "near-3.14", []),
    3551: VersionInfo((3, 13), 3551, False, "near-3.14", []),
    3552: VersionInfo((3, 13), 3552, False, "near-3.14", []),
    3553: VersionInfo((3, 13), 3553, False, "near-3.14", []),
    3554: VersionInfo((3, 13), 3554, False, "near-3.14", []),
    3555: VersionInfo((3, 13), 3555, False, "near-3.14", []),
    3556: VersionInfo((3, 13), 3556, False, "near-3.14", []),
    3557: VersionInfo((3, 13), 3557, False, "near-3.14", []),
    3558: VersionInfo((3, 13), 3558, False, "near-3.14", []),
    3559: VersionInfo((3, 13), 3559, False, "near-3.14", []),
    3560: VersionInfo((3, 13), 3560, False, "near-3.14", []),
    3561: VersionInfo((3, 13), 3561, False, "near-3.14", []),
    3562: VersionInfo((3, 13), 3562, False, "near-3.14", []),
    3563: VersionInfo((3, 13), 3563, False, "near-3.14", []),
    3564: VersionInfo((3, 13), 3564, False, "near-3.14", []),
    3565: VersionInfo((3, 13), 3565, False, "near-3.14", []),
    3566: VersionInfo((3, 13), 3566, False, "near-3.14", []),
    3567: VersionInfo((3, 13), 3567, False, "near-3.14", []),
    3568: VersionInfo((3, 13), 3568, False, "near-3.14", []),
    3569: VersionInfo((3, 13), 3569, False, "near-3.14", []),
    3570: VersionInfo((3, 13), 3570, False, "near-3.14", []),
    3571: VersionInfo(
        (3, 13),
        3571,
        False,
        "near-3.14",
        [
            "CALL_INTRINSIC_1 consolidates several opcodes",
            "MAKE_FUNCTION + SET_FUNCTION_ATTRIBUTE split",
            "no PEP 749 — annotations live in __annotations__ at class body time",
        ],
    ),
    # ---- 3.14: rule-supported, PEP 749 ----------------------------
    3600: VersionInfo((3, 14), 3600, True, "lazy-annotations", []),
    3601: VersionInfo((3, 14), 3601, True, "lazy-annotations", []),
    3602: VersionInfo((3, 14), 3602, True, "lazy-annotations", []),
    3603: VersionInfo((3, 14), 3603, True, "lazy-annotations", []),
    3604: VersionInfo((3, 14), 3604, True, "lazy-annotations", []),
    3605: VersionInfo((3, 14), 3605, True, "lazy-annotations", []),
    3610: VersionInfo((3, 14), 3610, True, "lazy-annotations", []),
    3611: VersionInfo((3, 14), 3611, True, "lazy-annotations", []),
    3615: VersionInfo((3, 14), 3615, True, "lazy-annotations", []),
    3620: VersionInfo((3, 14), 3620, True, "lazy-annotations", []),
    3623: VersionInfo((3, 14), 3623, True, "lazy-annotations", []),
    3625: VersionInfo((3, 14), 3625, True, "lazy-annotations", []),
    3627: VersionInfo(
        (3, 14),
        3627,
        True,
        "lazy-annotations",
        [
            "PEP 749 __annotate__ closures",
            "LOAD_SMALL_INT / LOAD_FAST_BORROW / LOAD_COMMON_CONSTANT",
            "EXTENDED_ARG used for large co_consts indices",
            "PEP 758 'except A, B:' parens-free syntax",
        ],
    ),
}


def read_magic(pyc_path: Path) -> int:
    """Extract the magic number from a ``.pyc`` file."""
    with open(pyc_path, "rb") as f:
        header = f.read(4)
    if len(header) < 4:
        raise ValueError(f"{pyc_path}: truncated .pyc header (< 4 bytes)")
    return struct.unpack("<H", header[:2])[0]


def detect_version(pyc_path: Path) -> VersionInfo:
    """Identify the Python version that produced *pyc_path*.

    Raises ``KeyError`` if the magic number is unknown to pychd.
    Callers can choose to fall back to the LLM-only pipeline; xdis
    can disassemble most older versions even when our rule pass
    cannot recognise their bytecode.
    """
    magic = read_magic(pyc_path)
    if magic in KNOWN_VERSIONS:
        return KNOWN_VERSIONS[magic]
    # Best-effort: infer minor version from xdis's broader table.
    try:
        import xdis.magics as xm

        ver = xm.versions.get(magic.to_bytes(2, "little") + b"\r\n")
        if ver:
            ver_tuple = _parse_xdis_version(str(ver))
            return VersionInfo(
                ver_tuple,
                magic_number=magic,
                rule_supported=False,
                epoch_label="from-xdis",
                quirks=[f"xdis identifies this as Python {ver}"],
            )
    except Exception:
        pass
    raise KeyError(
        f"{pyc_path}: unknown magic 0x{magic:04x}. "
        "Add an entry to pychd.versions.KNOWN_VERSIONS to support this "
        "Python version, or fall back to the LLM-only pipeline."
    )


def _parse_xdis_version(label: str) -> tuple[int, int]:
    """Convert an xdis version label like ``'3.10a7'`` to ``(3, 10)``."""
    parts = label.split(".")
    try:
        major = int(parts[0])
        minor_str = "".join(c for c in parts[1] if c.isdigit())
        return (major, int(minor_str))
    except IndexError, ValueError:
        return (0, 0)


def supported(version: tuple[int, int]) -> bool:
    """Return True if pychd's *rule-based* pass handles this version.

    The LLM-only pipeline is always available regardless of version
    (modulo xdis being able to disassemble the bytecode).
    """
    for info in KNOWN_VERSIONS.values():
        if info.version[:2] == version[:2] and info.rule_supported:
            return True
    return False


def compatibility_matrix() -> str:
    """Render the version compatibility table as markdown.

    Used by ``tools/render_paper.py`` to splice an always-up-to-date
    table into the README. One row per minor version (we collapse
    micro-release magic-number variants).
    """
    by_minor: dict[tuple[int, int], VersionInfo] = {}
    for info in KNOWN_VERSIONS.values():
        existing = by_minor.get(info.version)
        if existing is None or info.magic_number > existing.magic_number:
            by_minor[info.version] = info
    lines = [
        "| Python | Latest magic | Rule-based fast path | Notable bytecode change |",
        "|---|---:|:--:|---|",
    ]
    for ver in sorted(by_minor):
        info = by_minor[ver]
        rule = "✅" if info.rule_supported else "⚠️ LLM-only"
        quirk = (info.quirks[0] if info.quirks else "")[:90]
        lines.append(f"| **{info.label}** | {info.magic_number} | {rule} | {quirk} |")
    return "\n".join(lines)
