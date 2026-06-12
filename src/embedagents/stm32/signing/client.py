"""``SigningTool`` — STM32_SigningTool_CLI wrapper.

Single-method module per RES-015. Stateless: one subprocess per
``sign_binary()`` call; no probe lock, no ``session_state`` slot.

Validation runs entirely substrate-side before invoking the CLI
(per HIL — loud errors over vendor-tool surprises). The vendor's own
errors surface verbatim via ``signing-cli-failed`` with the captured
log path attached.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from embedagents.stm32.errors import (
    ConfigurationError,
    SigningToolError,
    ToolError,
)
from embedagents.stm32.signing.results import SigningResult
from embedagents.stm32.resolution import coerce_path
from embedagents.stm32.subprocess_runner import run_tool

if TYPE_CHECKING:
    from embedagents.stm32.context import SubstrateContext


_ADDRESS_RE = re.compile(r"^0x[0-9A-Fa-f]+$")
# No substrate-side header-version / image-type allowlists: SigningTool
# reports its own validation errors (RES-015 Q2(c) / RES-018 — the
# vendor-reports stance; an allowlist here would go stale as ST adds
# families). IMP-43 removed the dead constants that implied otherwise.
_ENTRY_POINT_REQUIRED: frozenset[str] = frozenset({"fsbl", "ssbl"})


class SigningTool:
    """Wrapper around STM32_SigningTool_CLI. One per ``SubstrateContext``."""

    def __init__(self, ctx: "SubstrateContext") -> None:
        self.ctx = ctx
        self._cli: Path | None = ctx.tools.stm32_signing_tool_cli
        self._log = ctx.logger.getChild("signing")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _require_cli(self) -> Path:
        if self._cli is None:
            raise ConfigurationError(
                message="STM32_SigningTool_CLI path not configured",
                hint=(
                    "Set signing.stm32_signing_tool_cli in "
                    ".claude/stm32-tools.local.jsonc, or set the "
                    "STM32_SIGNING_TOOL_CLI environment variable. The binary "
                    "is bundled with STM32CubeProgrammer inside STM32CubeCLT "
                    "(e.g. /opt/st/stm32cubeclt_*/STM32CubeProgrammer/bin/"
                    "STM32_SigningTool_CLI)."
                ),
            )
        return self._cli

    def _timeout_s(self) -> float:
        signing_defaults = getattr(self.ctx.defaults, "signing", None)
        if signing_defaults is None:
            return 30.0
        return float(getattr(signing_defaults, "timeout_s", 30))

    # ------------------------------------------------------------------
    # sign_binary — F-013
    # ------------------------------------------------------------------

    def sign_binary(
        self,
        input_path: Path,
        *,
        load_address: str,
        image_type: Literal["ssbl", "fsbl", "teeh", "teed", "teex", "copro"],
        header_version: Literal["1", "2", "2.1", "2.2", "2.3"],
        entry_point: str | None = None,
        option_flags: str | None = None,
        no_key: bool = False,
        align: bool | None = None,
        output_path: Path | None = None,
        device_family: str | None = None,
    ) -> SigningResult:
        """Sign ``input_path``; return ``SigningResult`` on success.

        See ``v1/signing-api.md`` for validation rules + argv mapping
        (UM2543 §2.1). Raises ``ValueError`` on bad input, ``SigningToolError``
        on signing-stage / output-exists / vendor failure.
        """
        # ---- input file checks ----
        input_path = coerce_path(input_path)  # str|Path tolerated (IMP-22)
        if output_path is not None:
            output_path = coerce_path(output_path)
        if not input_path.is_file():
            raise SigningToolError(
                message=f"input file not found: {input_path}",
                signing_marker="input-file-not-found",
                input_path=input_path,
                hint=(
                    "verify the input path exists and is readable; substrate "
                    "doesn't search PATH"
                ),
                recoverable=True,
            )

        # ---- address regex checks ----
        if not _ADDRESS_RE.match(load_address):
            raise ValueError(
                f"invalid load_address {load_address!r}; expected hex "
                "literal like '0x70000000' (regex ^0x[0-9A-Fa-f]+$)"
            )
        if entry_point is not None and entry_point and not _ADDRESS_RE.match(entry_point):
            raise ValueError(
                f"invalid entry_point {entry_point!r}; expected hex literal"
            )
        if option_flags is not None and not _ADDRESS_RE.match(option_flags):
            raise ValueError(
                f"invalid option_flags {option_flags!r}; expected hex literal"
            )

        # ---- conditional entry_point check ----
        if image_type in _ENTRY_POINT_REQUIRED and not entry_point:
            raise ValueError(
                f"entry_point is required for image_type={image_type!r}"
            )

        # ---- header_version + family-aware --align resolution ----
        align_resolved = self._resolve_align(
            align=align,
            header_version=header_version,
            device_family=device_family,
        )

        # ---- output_path default + existence refusal ----
        if output_path is None:
            output_path = input_path.with_name(
                f"{input_path.stem}-trusted{input_path.suffix}"
            )
        if output_path.exists():
            raise SigningToolError(
                message=f"output file already exists: {output_path}",
                signing_marker="output-exists",
                input_path=input_path,
                device_family=device_family,
                header_version=header_version,
                hint=(
                    "delete the existing file or pick a different output_path; "
                    "substrate refuses to overwrite"
                ),
                recoverable=True,
            )

        # ---- no-key WARNING ----
        if no_key:
            self._log.warning(
                "no_key=True: authentication disabled (-nk); dev-only; "
                "do NOT ship this binary"
            )

        # ---- build argv per UM2543 §2.1 ----
        cli = self._require_cli()
        args: list[str] = [
            "-bin", str(input_path),
            "-la", load_address,
            "-t", image_type,
            "-hv", header_version,
        ]
        if entry_point:
            args.extend(["-ep", entry_point])
        if option_flags is not None:
            args.extend(["-of", option_flags])
        if no_key:
            args.append("-nk")
        if align_resolved:
            args.append("--align")
        args.extend(["-o", str(output_path)])

        # ---- invoke + capture ----
        log_path = self._log_path()
        start = time.monotonic()
        try:
            run_tool(
                cli,
                args,
                ctx=self.ctx,
                timeout_s=self._timeout_s(),
                log_path=log_path,
            )
        except ToolError as ex:
            stderr = ex.tool_output or ""
            raise SigningToolError(
                message=f"STM32_SigningTool_CLI failed: {ex.message}",
                signing_marker="signing-cli-failed",
                code=ex.code,
                tool_output=stderr,
                input_path=input_path,
                device_family=device_family,
                header_version=header_version,
                hint=(
                    f"inspect log_path for vendor output: {log_path}"
                ),
                recoverable=False,
            ) from ex
        duration_s = time.monotonic() - start

        bytes_in = _file_size_or_zero(input_path)
        bytes_out = _file_size_or_zero(output_path)
        self._log.info(
            "signed %s → %s (hv=%s type=%s family=%s duration=%.2fs)",
            input_path,
            output_path,
            header_version,
            image_type,
            device_family,
            duration_s,
        )
        return SigningResult(
            input_path=input_path,
            output_path=output_path,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
            load_address=load_address,
            entry_point=entry_point if entry_point else None,
            image_type=image_type,
            header_version=header_version,
            option_flags=option_flags,
            no_auth_flag=no_key,
            align_applied=align_resolved,
            device_family=device_family,
            duration_s=duration_s,
            log_path=log_path,
        )

    def _resolve_align(
        self,
        *,
        align: bool | None,
        header_version: str,
        device_family: str | None,
    ) -> bool:
        """Apply the spec's auto-align rule for ``hv=2.3 + STM32N6``."""
        is_n6_hv23 = (
            header_version == "2.3"
            and device_family is not None
            and device_family.startswith("STM32N6")
        )
        if not is_n6_hv23:
            return bool(align)  # None / False → False; True → True
        if align is False:
            raise SigningToolError(
                message=(
                    "header_version=2.3 on STM32N6 requires --align; explicit "
                    "align=False conflicts with vendor requirement"
                ),
                signing_marker="align-required",
                device_family=device_family,
                header_version=header_version,
                hint=(
                    "pass align=True (or omit align= to let substrate "
                    "auto-set it for STM32N6 hv=2.3)"
                ),
                recoverable=True,
            )
        if align is None:
            self._log.info(
                "--align auto-set for hv=2.3 on %s", device_family
            )
            return True
        return True  # align=True explicit

    def _log_path(self) -> Path:
        """Generate a timestamped log path under cubeide.log_dir
        (signing reuses the same log dir convention as cubeide; substrate
        keeps log files together for easy audit)."""
        cubeide_defaults = getattr(self.ctx.defaults, "cubeide", None)
        raw_dir = (
            getattr(cubeide_defaults, "log_dir", None)
            if cubeide_defaults is not None
            else None
        )
        if raw_dir:
            log_dir = Path(raw_dir)
        else:
            log_dir = self.ctx.cwd / ".stm32-substrate-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return log_dir / f"sign-{ts}.log"


def _file_size_or_zero(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
