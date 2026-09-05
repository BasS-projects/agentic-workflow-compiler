"""Explicit, workspace-confined tools; no shell or dynamic import dispatch.

File tools require POSIX descriptor-relative operations (Linux and macOS).
Symlink components, including symlinks pointing inside the workspace, are
rejected. Atomic replacement makes repeated writes of the same text safe;
this is not a promise of exactly-once external side effects.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any, Iterator, Protocol
import uuid


@dataclass(frozen=True)
class TaskContext:
    workspace: str
    run_id: str
    step_id: str
    idempotency_key: str


class Tool(Protocol):
    def __call__(self, args: dict[str, Any], context: TaskContext) -> dict[str, Any]:
        """Execute an explicitly registered operation."""
        ...


def _string_arg(args: dict[str, Any], key: str) -> str:
    if key not in args or not isinstance(args[key], str):
        raise ValueError(f"{key!r} must be a string")
    return args[key]


def _path_info(path: str, context: TaskContext) -> tuple[Path, tuple[str, ...], str]:
    if not path or "\x00" in path:
        raise ValueError("path must be a nonempty string without null bytes")
    root = Path(context.workspace).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    candidate = Path(path)
    if ".." in candidate.parts:
        raise ValueError("parent traversal ('..') is not allowed")
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("path must be contained in the workspace") from exc
    parts = candidate.parts
    # The descriptor traversal below rejects all symlinks rather than resolving
    # user-supplied components through a potentially escaping symlink.
    return root, parts, candidate.as_posix()


def _lstat(parent_fd: int, name: str) -> os.stat_result:
    result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(result.st_mode):
        raise ValueError("symlink paths are not allowed")
    return result


@contextmanager
def _parent_directory(
    root: Path, parts: tuple[str, ...], *, create: bool = False
) -> Iterator[tuple[int, str]]:
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise RuntimeError("file tools require POSIX dir_fd and O_NOFOLLOW support")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for component in parts[:-1]:
            try:
                _lstat(descriptor, component)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o755, dir_fd=descriptor)
                except FileExistsError:
                    # A concurrent creator is fine only if it made a directory.
                    pass
                _lstat(descriptor, component)
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor, parts[-1] if parts else "."
    finally:
        os.close(descriptor)


def _files_exists(args: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    root, parts, path = _path_info(_string_arg(args, "path"), context)
    try:
        with _parent_directory(root, parts) as (parent, name):
            _lstat(parent, name)
    except (FileNotFoundError, NotADirectoryError):
        return {"exists": False, "path": path}
    return {"exists": True, "path": path}


def _files_require_exists(args: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    result = _files_exists(args, context)
    if not result["exists"]:
        raise FileNotFoundError(f"required workspace path does not exist: {result['path']}")
    return result


def _files_read_text(args: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    root, parts, _ = _path_info(_string_arg(args, "path"), context)
    with _parent_directory(root, parts) as (parent, name):
        _lstat(parent, name)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("read_text requires a regular file")
            stream = os.fdopen(descriptor, "r", encoding="utf-8", newline="")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            return {"text": stream.read()}


def _text_normalize(args: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    lines = [line.strip() for line in _string_arg(args, "text").splitlines()]
    first = 0
    last = len(lines)
    while first < last and not lines[first]:
        first += 1
    while last > first and not lines[last - 1]:
        last -= 1
    return {"text": "\n".join(lines[first:last])}


def _check_write_target(parent: int, name: str) -> None:
    try:
        result = _lstat(parent, name)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(result.st_mode):
        raise IsADirectoryError(name)
    if not stat.S_ISREG(result.st_mode):
        raise ValueError("write_text target must be a regular file")


def _files_write_text(args: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    root, parts, path = _path_info(_string_arg(args, "path"), context)
    value = _string_arg(args, "text")
    # Encode before creating directories or files so invalid Unicode has no effect.
    content = value.encode("utf-8")
    with _parent_directory(root, parts, create=True) as (parent, name):
        _check_write_target(parent, name)
        temporary = f".workflow-{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            try:
                stream = os.fdopen(descriptor, "wb")
            except BaseException:
                os.close(descriptor)
                raise
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _check_write_target(parent, name)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    return {"path": path, "bytes": len(content)}


def _core_value(args: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    if "value" not in args:
        raise ValueError("'value' is required")
    return {"value": args["value"]}


def default_tools() -> dict[str, Tool]:
    """Return a fresh allowlist; callers may register other trusted callables."""
    return {
        "files.exists": _files_exists,
        "files.require_exists": _files_require_exists,
        "files.read_text": _files_read_text,
        "text.normalize": _text_normalize,
        "files.write_text": _files_write_text,
        "core.value": _core_value,
    }
