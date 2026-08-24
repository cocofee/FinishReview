"""Lifecycle management for external timing receivers."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from .passage_receiver import PassageEventReceiver, PassageEventStore
from .race_metadata import RaceMetadataStore
from .racetiger_source import RaceTigerClient, RaceTigerSource


logger = logging.getLogger("FinishReview")


class ReceiverController:
    """Create, start, and stop the configured timing receiver."""

    def __init__(
        self,
        *,
        receiver_factory: Callable[..., PassageEventReceiver] = PassageEventReceiver,
        racetiger_client_factory: Callable[..., RaceTigerClient] = RaceTigerClient,
        racetiger_source_factory: Callable[..., RaceTigerSource] = RaceTigerSource,
    ):
        self._receiver_factory = receiver_factory
        self._racetiger_client_factory = racetiger_client_factory
        self._racetiger_source_factory = racetiger_source_factory
        self._receiver: PassageEventReceiver | None = None
        self._racetiger_source: RaceTigerSource | None = None
        self._racetiger_generation = 0

    @property
    def receiver(self) -> PassageEventReceiver | None:
        return self._receiver

    @property
    def racetiger_source(self) -> RaceTigerSource | None:
        return self._racetiger_source

    def start_cyclerace(
        self,
        host: str,
        port: int,
        store: PassageEventStore,
        *,
        on_accepted: Callable[[Any], None],
        metadata_store: RaceMetadataStore | None = None,
        on_metadata_accepted: Callable[[Any], None] | None = None,
        on_focus_accepted: Callable[[Any], None] | None = None,
    ) -> PassageEventReceiver:
        receiver = self._receiver
        if receiver is not None and receiver.is_running:
            return receiver

        receiver_kwargs: dict[str, Any] = {"on_accepted": on_accepted}
        parameters = tuple(
            inspect.signature(self._receiver_factory).parameters.values()
        )
        supports_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        parameter_names = {parameter.name for parameter in parameters}
        if supports_kwargs or "metadata_store" in parameter_names:
            receiver_kwargs["metadata_store"] = metadata_store
        if supports_kwargs or "on_metadata_accepted" in parameter_names:
            receiver_kwargs["on_metadata_accepted"] = on_metadata_accepted
        if supports_kwargs or "on_focus_accepted" in parameter_names:
            receiver_kwargs["on_focus_accepted"] = on_focus_accepted

        receiver = self._receiver_factory(
            host,
            port,
            store,
            **receiver_kwargs,
        )
        try:
            receiver.start()
        except Exception:
            try:
                receiver.stop()
            except Exception as exc:  # noqa: BLE001 - rollback is best effort.
                logger.warning("Failed to stop CycleRace receiver: %s", exc)
            raise
        self._receiver = receiver
        return receiver

    def start_racetiger(
        self,
        base_url: str,
        token: str,
        *,
        pc: str,
        rid: str,
        store: PassageEventStore,
        poll_interval_seconds: float,
        on_event: Callable[[Any, int], None],
        on_status: Callable[[Any, int], None],
    ) -> RaceTigerSource:
        source = self._racetiger_source
        if source is not None and source.is_running:
            return source

        missing = [
            label
            for label, value in (
                ("接口地址", base_url),
                ("PC", pc),
                ("RID", rid),
                ("令牌", token),
            )
            if not value
        ]
        if missing:
            raise ValueError("赛虎配置不完整，请填写：" + "、".join(missing))

        client = self._racetiger_client_factory(
            base_url,
            token,
            pc=pc,
            rid=rid,
        )
        self._racetiger_generation += 1
        generation = self._racetiger_generation

        def emit_event(event: Any) -> None:
            if generation == self._racetiger_generation:
                on_event(event, generation)

        def emit_status(status: Any) -> None:
            if generation == self._racetiger_generation:
                on_status(status, generation)

        source = self._racetiger_source_factory(
            client,
            store,
            race_id=rid,
            stage_id="finish",
            poll_interval_seconds=poll_interval_seconds,
            on_event=emit_event,
            on_status=emit_status,
        )
        try:
            source.start()
        except Exception:
            if generation == self._racetiger_generation:
                self._racetiger_generation += 1
            try:
                source.stop()
            except Exception:  # noqa: BLE001 - startup rollback is best effort.
                logger.exception("Failed to stop RaceTiger source after startup error")
            raise
        self._racetiger_source = source
        return source

    def is_racetiger_generation_current(self, generation: int) -> bool:
        return int(generation) == self._racetiger_generation

    def stop(self) -> tuple[str, ...]:
        errors: list[str] = []
        self._racetiger_generation += 1
        receiver = self._receiver
        if receiver is not None:
            error_message = ""
            try:
                receiver.stop()
            except Exception as exc:  # noqa: BLE001 - receiver factories may vary.
                logger.warning("Failed to stop CycleRace receiver: %s", exc)
                error_message = f"CycleRace receiver: {type(exc).__name__}"
            if receiver.is_running:
                if not error_message:
                    error_message = "CycleRace receiver did not stop"
                    logger.warning(error_message)
            else:
                self._receiver = None
            if error_message:
                errors.append(error_message)

        racetiger_source = self._racetiger_source
        if racetiger_source is not None:
            error_message = ""
            try:
                racetiger_source.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort.
                logger.warning("Failed to stop RaceTiger source: %s", exc)
                error_message = f"RaceTiger source: {type(exc).__name__}"
            if racetiger_source.is_running:
                if not error_message:
                    error_message = "RaceTiger source did not stop"
                    logger.warning(error_message)
            else:
                self._racetiger_source = None
            if error_message:
                errors.append(error_message)
        return tuple(errors)
