"""Top-level obfuscation entry point.

Dispatches between the native rewriter (when the .pyc was written by
the *currently-running* interpreter) and the subprocess rewriter
(everything else). Returns an :class:`ObfuscationReport` carrying the
output path, the writer version, and the identifier mapping.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from pychd.versions import VersionInfo

from .header import header_length_for, merge_pyc, split_pyc
from .rewrite_native import ObfuscationMapping, anonymise
from .rewrite_subprocess import run_subprocess_rewrite, uv_run_command


@dataclass
class ObfuscationReport:
    """Result of an obfuscation run."""

    in_path: Path
    out_path: Path
    version: VersionInfo
    used_native: bool
    mapping: ObfuscationMapping

    def total_renames(self) -> int:
        m = self.mapping
        return (
            len(m.names)
            + len(m.varnames)
            + len(m.freevars)
            + len(m.cellvars)
            + len(m.consts)
            + len(m.co_names)
        )


def _current_minor() -> tuple[int, int]:
    return (sys.version_info.major, sys.version_info.minor)


def obfuscate(
    in_pyc: Path,
    out_pyc: Path,
    *,
    force_subprocess: bool = False,
) -> ObfuscationReport:
    """Rewrite *in_pyc* → *out_pyc* and return an audit report.

    Native path: when the writer's minor matches the running
    interpreter we ``marshal.loads`` directly and rewrite in-process.
    Cross-version path: spawn ``uv run --python <writer-minor>`` and
    run the same rewrite inside it (see ``rewrite_subprocess.py``).

    ``force_subprocess=True`` always takes the subprocess path; useful
    for tests that want to verify the cross-version code on the
    currently-running version too.
    """
    in_pyc = Path(in_pyc)
    out_pyc = Path(out_pyc)
    version, header, body = split_pyc(in_pyc)
    hlen = header_length_for(version)
    use_native = (not force_subprocess) and version.version == _current_minor()
    if use_native:
        import marshal

        code = marshal.loads(body)
        new_code, mapping = anonymise(code)
        new_body = marshal.dumps(new_code)
        out_pyc.parent.mkdir(parents=True, exist_ok=True)
        out_pyc.write_bytes(merge_pyc(header, new_body))
        return ObfuscationReport(
            in_path=in_pyc,
            out_path=out_pyc,
            version=version,
            used_native=True,
            mapping=mapping,
        )
    target_cmd = uv_run_command(version.version)
    mapping = run_subprocess_rewrite(target_cmd, in_pyc, out_pyc, hlen)
    return ObfuscationReport(
        in_path=in_pyc,
        out_path=out_pyc,
        version=version,
        used_native=False,
        mapping=mapping,
    )


__all__ = ["ObfuscationReport", "obfuscate"]
