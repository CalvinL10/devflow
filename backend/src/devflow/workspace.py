from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

from devflow.database import Database
from devflow.errors import RevisionConflict
from devflow.models import MAX_FILE_BYTES, FilePatchSet, validate_file_path
from devflow.staging import StagingFileStore

RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class CandidateBoundaryError(ValueError):
    pass


class ManagedWorkspace:
    """Materializes isolated candidates from managed integer revisions."""

    def __init__(self, root: Path | str, database: Database):
        self.root = Path(os.path.abspath(root))
        self.database = database
        self.revisions_root = self.root / "revisions"
        self.candidates_root = self.root / "candidates"

    def initialize(self) -> None:
        self._reject_root_ancestors()
        self._ensure_managed_directory(self.root, parents=True)
        self._ensure_managed_directory(self.revisions_root)
        self._ensure_managed_directory(self.candidates_root)
        initial = self.revision_path(0)
        self._ensure_managed_directory(initial)

    def revision_path(self, revision: int) -> Path:
        if revision < 0:
            raise ValueError("revision must be non-negative")
        return self.revisions_root / f"{revision:08d}"

    def candidate_path(self, run_id: str) -> Path:
        if RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise CandidateBoundaryError("run_id is not safe for a candidate directory")
        return self.candidates_root / run_id

    def require_candidate_path(self, run_id: str, candidate_dir: Path | str) -> Path:
        expected = self.candidate_path(run_id)
        supplied = Path(os.path.abspath(candidate_dir))
        if os.path.normcase(str(supplied)) != os.path.normcase(str(expected)):
            raise CandidateBoundaryError(
                f"candidate_dir must be the managed candidate path for run {run_id}"
            )
        return expected

    def require_materialized_candidate(
        self, run_id: str, candidate_dir: Path | str
    ) -> Path:
        candidate = self.require_candidate_path(run_id, candidate_dir)
        self._assert_managed_path(candidate, strict=True)
        if not candidate.is_dir():
            raise CandidateBoundaryError(f"managed candidate is not a directory: {candidate}")
        self._reject_links(candidate)
        return candidate

    def read_revision(self, revision: int) -> dict[str, str]:
        source = self.revision_path(revision)
        self._assert_managed_path(source, strict=True)
        return self._read_text_tree(source)

    def _read_text_tree(self, root: Path) -> dict[str, str]:
        self._assert_managed_path(root, strict=True)
        self._reject_links(root)
        files = {}
        total = 0
        for path in root.rglob("*"):
            self._assert_managed_path(path, strict=True)
            validate_file_path(path.relative_to(root).as_posix())
            if path.is_dir():
                continue
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise CandidateBoundaryError("candidate supports bounded regular text files only")
            with path.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > 16 * 1024 * 1024 or len(files) >= 1000:
                raise CandidateBoundaryError("managed revision exceeds materialization limits")
            text = data.decode("utf-8")
            if "\x00" in text:
                raise CandidateBoundaryError("binary files are not supported in this slice")
            files[path.relative_to(root).as_posix()] = text
        return files

    def materialize_candidate(
        self, *, run_id: str, base_revision: int, patch: FilePatchSet | None = None
    ) -> Path:
        run = self.database.get_run(run_id)
        if int(run["base_workspace_revision"]) != base_revision:
            raise RevisionConflict("run base revision does not match the requested revision")
        with self.database.connect() as connection:
            current = connection.execute(
                "SELECT current_revision FROM workspaces WHERE id = ?", (run["workspace_id"],)
            ).fetchone()
        if int(current["current_revision"]) != base_revision:
            raise RevisionConflict("workspace head moved after the run was created")
        if patch is not None and (
            patch.run_id != run_id or patch.base_workspace_revision != base_revision
            or self.database.current_patch(run_id)["patch_revision"] != patch.patch_revision
        ):
            raise RevisionConflict("candidate patch identity does not match the run")

        source_path = self.revision_path(base_revision)
        self._assert_managed_path(source_path, strict=True)
        self._reject_links(source_path)
        self._read_text_tree(source_path)
        source = source_path.resolve(strict=True)
        target = self.candidate_path(run_id)
        temporary = target.with_name(f".{target.name}.materializing")
        self._assert_managed_path(self.candidates_root, strict=True)
        if os.path.lexists(target) or os.path.lexists(temporary):
            raise FileExistsError(f"candidate already exists for run {run_id}")
        materialized = False
        try:
            self._copy_tree_without_links(source, temporary)
            self._assert_managed_path(temporary, strict=True)
            self._reject_links(temporary)
            if patch is not None:
                staged = StagingFileStore(self._read_text_tree(temporary)).stage(patch)
                for file in patch.files:
                    path = temporary / file.path
                    self._assert_managed_path(path, strict=False)
                    if file.modified is None:
                        path.unlink()
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        self._assert_managed_path(path, strict=False)
                        path.write_bytes(staged[file.path].encode("utf-8"))
            self._read_text_tree(temporary)
            temporary.chmod(0o700)
            os.replace(temporary, target)
            materialized = True
        finally:
            if not materialized and os.path.lexists(temporary):
                self._reject_link(temporary)
                shutil.rmtree(temporary)
        self._assert_managed_path(target, strict=True)
        return target

    def publish_patch(self, *, decision_id: str, owner_id: str, run_id: str, patch: FilePatchSet) -> int:
        existing = self.database.publication_for_decision(decision_id)
        if existing is not None:
            return int(existing["revision"])
        base = self.read_revision(patch.base_workspace_revision)
        staged = StagingFileStore(base).stage(patch)
        target = self.revision_path(patch.base_workspace_revision + 1)
        # External idempotency keys are opaque DB values, not filesystem names.
        safe_run_id = self.candidate_path(run_id).name
        temporary = self.revisions_root / f".{target.name}.run-{safe_run_id}.publishing"
        self._assert_managed_path(self.revisions_root, strict=True)
        self.database.require_decision_resume_claim(decision_id=decision_id, owner_id=owner_id)
        staging_paths = [temporary]
        # Recover pre-fix staging only when the old ID is a bounded single
        # component. Never interpret arbitrary persisted IDs as legacy paths.
        if RUN_ID_PATTERN.fullmatch(decision_id):
            staging_paths.append(self.revisions_root / f".{target.name}.{decision_id}.publishing")
        for staging_path in dict.fromkeys(staging_paths):
            if os.path.lexists(staging_path):
                # A process exit cannot run finally. Rebuild under the reclaimed
                # lease, retaining all path, link and text-tree checks.
                self._remove_publication_staging(staging_path, decision_id=decision_id, owner_id=owner_id)
        if os.path.lexists(target):
            if self._read_text_tree(target) != staged:
                raise RevisionConflict("target workspace revision already contains different contents")
            return self.database.finalize_publication(
                decision_id=decision_id, run_id=run_id, patch_revision=patch.patch_revision,
                owner_id=owner_id, revision=patch.base_workspace_revision + 1,
                base_revision=patch.base_workspace_revision, patch_id=self.database.get_patch(run_id, patch.patch_revision)["id"],
            )
        published = False
        try:
            temporary.mkdir(mode=0o700)
            self._write_tree(temporary, staged)
            self._read_text_tree(temporary)
            self.database.require_decision_resume_claim(decision_id=decision_id, owner_id=owner_id)

            def publish() -> None:
                nonlocal published
                os.replace(temporary, target)
                published = True

            return self.database.finalize_publication(
                decision_id=decision_id, run_id=run_id, patch_revision=patch.patch_revision,
                owner_id=owner_id, revision=patch.base_workspace_revision + 1,
                base_revision=patch.base_workspace_revision, patch_id=self.database.get_patch(run_id, patch.patch_revision)["id"],
                publish=publish,
            )
        finally:
            if not published and os.path.lexists(temporary):
                self.database.require_decision_resume_claim(decision_id=decision_id, owner_id=owner_id)
                self._remove_publication_staging(temporary, decision_id=decision_id, owner_id=owner_id)

    def _remove_publication_staging(self, temporary: Path, *, decision_id: str, owner_id: str) -> None:
        self._assert_managed_path(temporary, strict=True)
        if temporary.parent != self.revisions_root or not temporary.is_dir():
            raise CandidateBoundaryError("publication staging must be a managed revision directory")
        # Validate ordinary staging contents before deletion so lease checks still
        # happen after the tree walk. A crash-truncated UTF-8 file is recoverable;
        # the containment/link checks, not decoding, form the deletion boundary.
        try:
            self._read_text_tree(temporary)
        except UnicodeDecodeError:
            self._reject_links(temporary)
        self.database.require_decision_resume_claim(decision_id=decision_id, owner_id=owner_id)
        shutil.rmtree(temporary)

    def _write_tree(self, root: Path, files: dict[str, str]) -> None:
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            self._assert_managed_path(path.parent, strict=True)
            path.write_bytes(text.encode("utf-8"))
            self._assert_managed_path(path, strict=True)

    def _ensure_managed_directory(self, path: Path, *, parents: bool = False) -> None:
        if os.path.lexists(path):
            self._reject_link(path)
            if not path.is_dir():
                raise CandidateBoundaryError(f"managed path is not a directory: {path}")
        else:
            path.mkdir(parents=parents, exist_ok=False)
        self._assert_managed_path(path, strict=True)

    def _assert_managed_path(self, path: Path, *, strict: bool) -> None:
        self._reject_root_ancestors()
        try:
            relative = path.relative_to(self.root)
        except ValueError as error:
            raise CandidateBoundaryError(f"managed path escapes workspace root: {path}") from error

        current = self.root
        if os.path.lexists(current):
            self._reject_link(current)
        for part in relative.parts:
            current = current / part
            if os.path.lexists(current):
                self._reject_link(current)

        resolved_root = self.root.resolve(strict=True)
        resolved_path = path.resolve(strict=strict)
        if not resolved_path.is_relative_to(resolved_root):
            raise CandidateBoundaryError(f"managed path resolves outside workspace root: {path}")

    def _reject_root_ancestors(self) -> None:
        for path in (self.root, *self.root.parents):
            if os.path.lexists(path):
                self._reject_link(path)

    @staticmethod
    def _is_link_or_reparse_point(path: Path) -> bool:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
        except OSError:
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

    @classmethod
    def _reject_link(cls, path: Path) -> None:
        if cls._is_link_or_reparse_point(path):
            raise CandidateBoundaryError(
                f"managed path contains a symbolic link or reparse point: {path}"
            )
        try:
            metadata = path.lstat()
        except OSError:
            return
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise CandidateBoundaryError(f"managed path contains a hard-linked file: {path}")

    @classmethod
    def _reject_links(cls, root: Path) -> None:
        cls._reject_link(root)
        for directory, directory_names, file_names in os.walk(
            root, topdown=True, followlinks=False
        ):
            parent = Path(directory)
            for name in (*directory_names, *file_names):
                cls._reject_link(parent / name)

    @classmethod
    def _copy_tree_without_links(cls, source: Path, target: Path, *, _depth=0, _usage=None) -> None:
        usage = _usage if _usage is not None else [0, 0]
        if _depth > 20:
            raise CandidateBoundaryError("managed revision directory depth exceeded")
        cls._reject_link(source)
        target.mkdir(mode=0o700)
        with os.scandir(source) as entries:
            for entry in entries:
                usage[0] += 1
                if usage[0] > 2000:
                    raise CandidateBoundaryError("managed revision entry count exceeded")
                source_entry = Path(entry.path)
                target_entry = target / entry.name
                cls._reject_link(source_entry)
                if entry.is_dir(follow_symlinks=False):
                    cls._copy_tree_without_links(
                        source_entry, target_entry, _depth=_depth + 1, _usage=usage
                    )
                elif entry.is_file(follow_symlinks=False):
                    # DirEntry.stat() omits stable inode/link metadata on some
                    # Windows Python builds; Path.lstat() retains it.
                    expected = source_entry.lstat()
                    cls._require_single_link_regular_file(expected, source_entry)
                    size = cls._copy_regular_file(source_entry, target_entry, expected)
                    usage[1] += size
                    if usage[1] > 16 * 1024 * 1024:
                        raise CandidateBoundaryError("managed revision size exceeded")
                    cls._reject_link(source_entry)
                    cls._reject_link(target_entry)
                else:
                    raise CandidateBoundaryError(
                        f"managed revision contains an unsupported file: {source_entry}"
                    )
        cls._reject_link(source)

    @staticmethod
    def _require_single_link_regular_file(metadata: os.stat_result, path: Path) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise CandidateBoundaryError(f"managed revision contains an unsupported file: {path}")
        if metadata.st_nlink != 1:
            raise CandidateBoundaryError(f"managed path contains a hard-linked file: {path}")
        if metadata.st_size > MAX_FILE_BYTES:
            raise CandidateBoundaryError("managed revision size exceeded")

    @classmethod
    def _copy_regular_file(
        cls, source: Path, target: Path, expected: os.stat_result
    ) -> int:
        """Copy through checked descriptors without following or replacing a target."""
        source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        source_flags |= getattr(os, "O_NOFOLLOW", 0)
        target_flags |= getattr(os, "O_NOFOLLOW", 0)
        source_fd: int | None = None
        target_fd: int | None = None
        try:
            try:
                source_fd = os.open(source, source_flags)
            except OSError as error:
                raise CandidateBoundaryError(
                    f"managed source could not be opened safely: {source}"
                ) from error
            opened_source = os.fstat(source_fd)
            cls._require_single_link_regular_file(opened_source, source)
            if not os.path.samestat(expected, opened_source):
                raise CandidateBoundaryError(f"managed source changed during copy: {source}")

            try:
                target_fd = os.open(
                    target,
                    target_flags,
                    stat.S_IMODE(opened_source.st_mode) or 0o600,
                )
            except OSError as error:
                raise CandidateBoundaryError(
                    f"materialization target could not be created exclusively: {target}"
                ) from error

            opened_target = os.fstat(target_fd)
            cls._require_single_link_regular_file(opened_target, target)
            copied = 0
            with (
                os.fdopen(source_fd, "rb", closefd=False) as source_stream,
                os.fdopen(target_fd, "wb", closefd=False) as target_stream,
            ):
                while chunk := source_stream.read(64 * 1024):
                    copied += len(chunk)
                    if copied > MAX_FILE_BYTES:
                        raise CandidateBoundaryError("managed revision size exceeded")
                    target_stream.write(chunk)

            final_source = os.fstat(source_fd)
            final_target = os.fstat(target_fd)
            cls._require_single_link_regular_file(final_source, source)
            cls._require_single_link_regular_file(final_target, target)
            try:
                source_path = source.lstat()
                target_path = target.lstat()
            except OSError as error:
                raise CandidateBoundaryError("managed file changed during copy") from error
            if not os.path.samestat(final_source, source_path):
                raise CandidateBoundaryError(f"managed source changed during copy: {source}")
            if not os.path.samestat(final_target, target_path):
                raise CandidateBoundaryError(f"materialization target changed during copy: {target}")
            return copied
        finally:
            if target_fd is not None:
                os.close(target_fd)
            if source_fd is not None:
                os.close(source_fd)
