"""Structured logging configuration using structlog.

Three log flags, chosen by ``cli.run`` and honoured everywhere:

* **default** — human-friendly colourised output when running in a
  terminal, JSON when piped/redirected (auto-detected via ``isatty``).
  INFO level for our own modules, WARNING+ for ``httpx`` / ``httpcore`` /
  ``urllib3`` so wire-level chatter doesn't drown the pipeline output.
* ``--verbose`` — same renderer choice, but DEBUG for our modules. The
  chatty HTTP libraries stay quiet so operators can actually read the
  tool's own progress.
* ``--debug`` — like ``--verbose`` but *also* enables DEBUG on the HTTP
  libraries. Reserve this for network / auth debugging.
* ``--json`` — force JSON output regardless of whether stderr is a TTY;
  useful when scripting a wrapper around the CLI.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

# Loggers that produce mostly wire-level chatter; silenced under
# --verbose so the tool's own events remain readable. Only --debug
# enables their DEBUG output.
_NOISY_LOGGERS: tuple[str, ...] = ("httpx", "httpcore", "urllib3")

# Context keys already shown once in the startup banner — drop them from
# every subsequent human-readable log line to reduce visual noise.
_HUMAN_HIDDEN_KEYS: frozenset[str] = frozenset({"run_id", "tenant_name"})


def _hide_context_keys(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Drop banner-shown context keys from each pretty-mode line."""
    for key in _HUMAN_HIDDEN_KEYS:
        event_dict.pop(key, None)
    return event_dict


def configure_logging(
    *,
    verbose: bool = False,
    debug: bool = False,
    json_output: bool = False,
    tenant_name: str,
    log_dir: Path | None = None,
) -> structlog.stdlib.BoundLogger:
    """Initialise structlog and return a bound logger for the current run.

    Args:
        verbose: When *True*, our app logs at DEBUG. HTTP libraries stay
            at WARNING+.
        debug: When *True*, everything (including HTTP libraries) logs at
            DEBUG. Implies ``verbose``.
        json_output: When *True*, force JSON output regardless of whether
            stderr is a TTY.
        tenant_name: Anaplan tenant name bound to every log event.
        log_dir: When set, a full-fidelity JSON copy of the run is written
            to ``<log_dir>/run_<UTC-timestamp>_<run_id>.log`` in addition to
            the console. Console verbosity/pretty-mode is unaffected; the
            file always captures the run at the same level the console does,
            so the INFO step/upload/record-count lines survive even when a
            long run scrolls them off the terminal. ``None`` (the default,
            used by tests) writes no file.

    Returns:
        A :class:`structlog.stdlib.BoundLogger` pre-bound with ``run_id``
        and ``tenant_name``.
    """
    run_id = str(uuid.uuid4())
    use_pretty = not json_output and sys.stderr.isatty()
    app_level = logging.DEBUG if verbose or debug else logging.INFO
    third_party_level = logging.DEBUG if debug else logging.WARNING

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    # In pretty mode, hide the always-bound context keys after the banner
    # so each log line is short and scannable.
    pretty_processors: list[structlog.types.Processor] = [
        *shared_processors,
        _hide_context_keys,
    ]

    renderer: structlog.types.Processor
    if use_pretty:
        renderer = structlog.dev.ConsoleRenderer(pad_event=32, sort_keys=False)
        formatter_pre_chain = pretty_processors
    else:
        renderer = structlog.processors.JSONRenderer()
        formatter_pre_chain = shared_processors

    structlog.configure(
        processors=[
            *(pretty_processors if use_pretty else shared_processors),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,  # re-invocation in tests must re-init
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=formatter_pre_chain,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(app_level)

    # Optional durable run log. Always JSON (machine-parseable, full
    # fidelity) regardless of the console renderer. Written via its own
    # ProcessorFormatter so console pretty-mode and the file can coexist.
    log_file_path: Path | None = None
    if log_dir is not None:
        log_file_path = log_dir / f"run_{datetime.now(UTC):%Y%m%d_%H%M%S}_{run_id[:8]}.log"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
            foreign_pre_chain=shared_processors,
        )
        file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

    # Silence the HTTP libraries unless --debug was passed. They are
    # far too chatty to leave at INFO/DEBUG for a normal --verbose run.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(third_party_level)

    logger: structlog.stdlib.BoundLogger = structlog.get_logger()
    logger = logger.bind(run_id=run_id, tenant_name=tenant_name)

    if log_file_path is not None:
        logger.info("run_log_file", path=str(log_file_path))

    if use_pretty:
        # Startup banner shows the once-per-run context that pretty mode
        # then hides from every subsequent line.
        logger.info(
            "run_started",
            run_id=run_id,
            tenant_name=tenant_name,
            mode="debug" if debug else ("verbose" if verbose else "normal"),
        )

    return logger
