"""Use only the Engine public financial session API; financial ownership stays there."""

from importlib import import_module
from pathlib import Path


def create(manifest, artifact_root, staging_path, checkpoint=None):
    BackendContext = import_module("quant_backtest_core").BackendContext
    sessions = import_module("quant_backtest_core.sessions")
    open_session, restore_session = sessions.open_session, sessions.restore_session

    context = BackendContext(Path(artifact_root), Path(staging_path))
    if checkpoint is None:
        return open_session(manifest, context=context)
    return restore_session(manifest, checkpoint, context=context)
