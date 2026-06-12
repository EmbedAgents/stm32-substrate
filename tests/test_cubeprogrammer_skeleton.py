"""B1 skeleton tests — package imports, result dataclasses are frozen,
every CubeProgrammer public method raises ``NotImplementedError`` until
its B-phase body lands."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import (
    BankInfo,
    BannerResult,
    BooleanResult,
    Confirmation,
    CoresResult,
    CubeProgrammer,
    CubeProgrammerErrorCode,
    EraseConfirmation,
    FlashConfirmation,
    HardFaultDecode,
    ITMRecord,
    MemoryLayoutResult,
    MemoryReadResult,
    OptionByteDiffEntry,
    OptionBytesDiff,
    OptionBytesResult,
    PairFlashResult,
    ProbeRecord,
    RecoveryAttempt,
    RecoveryResult,
    ResetConfirmation,
    SVDResult,
    is_recoverable,
)
from embedagents.stm32.errors import ConfigurationError


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


_ALL_RESULT_TYPES = [
    BankInfo,
    BannerResult,
    BooleanResult,
    Confirmation,
    CoresResult,
    EraseConfirmation,
    FlashConfirmation,
    HardFaultDecode,
    ITMRecord,
    MemoryLayoutResult,
    MemoryReadResult,
    OptionByteDiffEntry,
    OptionBytesDiff,
    OptionBytesResult,
    PairFlashResult,
    ProbeRecord,
    RecoveryAttempt,
    RecoveryResult,
    ResetConfirmation,
    SVDResult,
]


class TestResultDataclasses:
    @pytest.mark.parametrize("cls", _ALL_RESULT_TYPES)
    def test_is_frozen_dataclass(self, cls: type) -> None:
        assert is_dataclass(cls), f"{cls.__name__} is not a dataclass"
        # Frozen dataclasses set FROZEN=True; construct an instance and
        # verify assignment raises.
        inst = _construct_default(cls)
        first_field = fields(cls)[0]
        with pytest.raises(FrozenInstanceError):
            setattr(inst, first_field.name, _sample_value_for(first_field.type))

    def test_banner_result_shape(self) -> None:
        b = BannerResult(
            stlink_sn="066BFF...",
            stlink_fw="V3J11M3",
            board_name="NUCLEO-L476RG",
            voltage_v=3.28,
            swd_freq_khz=4000,
            device_id="0x415",
            device_name="STM32L4x6",
            device_type="MCU",
            device_cpu="Cortex-M4",
            flash_size_kb=1024,
        )
        assert b.mode_used == "NORMAL"  # default
        assert b.voltage_suspicious is False  # default

    def test_memory_layout_allows_none(self) -> None:
        m = MemoryLayoutResult(
            flash_size_kb=1024, ram_size_kb=None, device_name="STM32L476RG"
        )
        assert m.ram_size_kb is None
        assert m.bank_layout is None  # default

    def test_cores_default_empty(self) -> None:
        c = CoresResult(device_name="STM32L476RG", primary_core="Cortex-M4")
        assert c.secondary_cores == []
        assert c.multi_core is None

    def test_confirmation_data_default_empty(self) -> None:
        c = Confirmation(operation="halt")
        assert c.data == {}

    def test_pair_flash_partial(self) -> None:
        f = FlashConfirmation(bytes_written=1024, address="0x08000000", duration_s=0.5)
        p = PairFlashResult(bootloader=f, application=None, both_succeeded=False)
        assert p.application is None
        assert p.both_succeeded is False


# ---------------------------------------------------------------------------
# Error codes + recoverability
# ---------------------------------------------------------------------------


class TestCubeProgrammerErrorCode:
    def test_canonical_codes_present(self) -> None:
        assert CubeProgrammerErrorCode.TARGET_CONNECT_ERR == 1
        assert CubeProgrammerErrorCode.TARGET_DLL_ERR == 2
        assert CubeProgrammerErrorCode.TARGET_STLINK_SELECT_REQ == 16
        assert CubeProgrammerErrorCode.TARGET_STLINK_SERIAL_NOT_FOUND == 17

    @pytest.mark.parametrize(
        "code,expected",
        [
            (CubeProgrammerErrorCode.TARGET_NO_DEVICE, True),
            (CubeProgrammerErrorCode.TARGET_UNKNOWN_MCU_TARGET, True),
            (CubeProgrammerErrorCode.TARGET_HELD_UNDER_RESET, True),
            (CubeProgrammerErrorCode.TARGET_NOT_HALTED, True),
            (CubeProgrammerErrorCode.TARGET_CONNECT_ERR, False),
            (CubeProgrammerErrorCode.TARGET_DLL_ERR, False),
            (CubeProgrammerErrorCode.TARGET_USB_COMM_ERR, False),
            (CubeProgrammerErrorCode.TARGET_FIRMWARE_OLD, False),
            (CubeProgrammerErrorCode.TARGET_CMD_ERR, False),
            (CubeProgrammerErrorCode.TARGET_HALT_ERR, False),
            (CubeProgrammerErrorCode.TARGET_INTERNAL_ERR, False),
            (CubeProgrammerErrorCode.TARGET_VERSION_ERR, False),
            (CubeProgrammerErrorCode.TARGET_STATUS_ERR, False),
            (CubeProgrammerErrorCode.TARGET_STLINK_SELECT_REQ, False),
            (CubeProgrammerErrorCode.TARGET_STLINK_SERIAL_NOT_FOUND, False),
        ],
    )
    def test_recoverability_matrix(
        self, code: CubeProgrammerErrorCode, expected: bool
    ) -> None:
        assert is_recoverable(code) is expected

    def test_unmapped_int_is_not_recoverable(self) -> None:
        assert is_recoverable(999) is False

    def test_none_is_not_recoverable(self) -> None:
        assert is_recoverable(None) is False

    def test_recoverable_accepts_plain_int(self) -> None:
        assert is_recoverable(4) is True
        assert is_recoverable(1) is False


# ---------------------------------------------------------------------------
# CubeProgrammer client skeleton
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path: Path) -> SubstrateContext:
    return SubstrateContext.from_environment(project_path=tmp_path)


class TestClientSkeleton:
    def test_construct(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        assert client.ctx is ctx
        assert client._log.name == "embedagents.stm32.cubeprogrammer"

    def test_cli_unresolved_in_isolated_ctx(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Isolate from host PATH / env so the built-in fallback cannot
        # resolve STM32_Programmer_CLI.
        monkeypatch.delenv("STM32_PROGRAMMER_CLI", raising=False)
        monkeypatch.setenv("PATH", "")
        isolated = SubstrateContext.from_environment(project_path=tmp_path)
        client = CubeProgrammer(isolated)
        assert client._cli is None

    def test_require_cli_raises_loud_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_PROGRAMMER_CLI", raising=False)
        monkeypatch.setenv("PATH", "")
        isolated = SubstrateContext.from_environment(project_path=tmp_path)
        client = CubeProgrammer(isolated)
        with pytest.raises(ConfigurationError) as excinfo:
            client._require_cli()
        err = excinfo.value
        assert "STM32_Programmer_CLI" in err.message
        assert err.hint is not None
        assert "stm32-tools.local.jsonc" in err.hint
        assert "STM32_PROGRAMMER_CLI" in err.hint

    def test_sn_args_empty_when_unset(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        assert client._sn_args() == []

    def test_sn_args_populated_when_set(self, tmp_path: Path) -> None:
        # Construct a ctx via the public API with a default_probe_sn set
        # via env var.
        import os

        os.environ["STM32_PROGRAMMER_DEFAULT_SN"] = "066BFFTESTSN"
        try:
            ctx = SubstrateContext.from_environment(project_path=tmp_path)
        finally:
            del os.environ["STM32_PROGRAMMER_DEFAULT_SN"]
        client = CubeProgrammer(ctx)
        assert client._sn_args() == ["sn=066BFFTESTSN"]


class TestEveryPublicMethodRaises:
    """B1: every public method raises ``NotImplementedError`` until its
    B-phase body lands. This locks the surface."""

    _PUBLIC_METHODS = [
        # connect() implemented in B3; verified separately in test_cubeprogrammer_connect.py.
        # list_probes() implemented in B4; verified separately in test_cubeprogrammer_list_probes.py.
        # connect_under_reset / board_name / memory_layout / cores implemented in B5a;
        #   verified separately in test_cubeprogrammer_discovery.py.
        # ping_swd implemented in B5b; verified in test_cubeprogrammer_ping_swd.py.
        # read_option_bytes implemented in B5c; verified in test_cubeprogrammer_option_bytes.py.
        # diagnose_micro implemented in B5d; verified in test_cubeprogrammer_diagnose.py.
        # erase_chip / erase_and_reset implemented in B6a; verified in test_cubeprogrammer_erase.py.
        # flash_file / flash_bin / flash_data / flash_signed implemented in B6b;
        #   verified in test_cubeprogrammer_flash_atomic.py.
        # read_memory / read_flash_to_file implemented in B6c;
        #   verified in test_cubeprogrammer_read.py.
        # flash_to_bank / flash_bin_no_address / flash_pair / flash_signed_pair /
        #   download_image implemented in B6d; flash_external implemented in B6e.
        # All verified in test_cubeprogrammer_flash_compound.py / test_cubeprogrammer_flash_external.py.
        # reset / halt / resume implemented in B7; verified in test_cubeprogrammer_atomic_control.py.
        # write_option_bytes / verify_option_bytes implemented in B8;
        #   verified in test_cubeprogrammer_option_bytes_write.py.
        # analyze_hardfault implemented in B9; verified in test_cubeprogrammer_hardfault.py.
        # tail_swo implemented in B10; verified in test_cubeprogrammer_swo.py.
        # svd_for_attached implemented in C4g (unblocked by ctx.svd_db);
        #   verified in test_debug_start_session.py.
    ]
    # Implemented (verified separately):
    #   - connect() / connect_under_reset(): B3 / B5a
    #   - list_probes(): B4 → test_cubeprogrammer_list_probes.py
    #   - All other methods through B10 → corresponding test files.

    @pytest.mark.parametrize("method_name,args", _PUBLIC_METHODS)
    def test_raises_not_implemented(
        self, ctx: SubstrateContext, method_name: str, args: tuple
    ) -> None:
        client = CubeProgrammer(ctx)
        method = getattr(client, method_name)
        with pytest.raises(NotImplementedError):
            method(*args)

    # read_flash_to_file implemented in B6c; verified in test_cubeprogrammer_read.py.

    # tail_swo implemented in B10; verified in test_cubeprogrammer_swo.py.


class TestPublicSurfaceCount:
    """Spec contract: 31 public methods on CubeProgrammer (per
    v1/cubeprogrammer-api.md § "Public methods"). The doc summary says
    "~30 prompts" / "~28 methods" — the actual exact count below the
    summary tables is 31. Lock it down so regressions surface."""

    def test_method_count(self) -> None:
        methods = [
            name
            for name, member in inspect.getmembers(CubeProgrammer, inspect.isfunction)
            if not name.startswith("_")
        ]
        assert len(methods) == 31, f"got {len(methods)} public methods: {sorted(methods)}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _construct_default(cls: type):
    """Build a minimal instance of a result dataclass using sample values
    drawn from each field's declared type."""
    kwargs = {}
    for f in fields(cls):
        if f.default is not _MISSING or f.default_factory is not _MISSING:  # type: ignore[misc]
            continue
        kwargs[f.name] = _sample_value_for(f.type)
    return cls(**kwargs)


def _sample_value_for(annotation) -> object:
    if annotation in (str, "str"):
        return "x"
    if annotation in (int, "int"):
        return 0
    if annotation in (float, "float"):
        return 0.0
    if annotation in (bool, "bool"):
        return False
    return None


_MISSING = object()
try:
    from dataclasses import MISSING as _MISSING  # noqa: F401, E402
except ImportError:
    pass
